from __future__ import annotations

import fnmatch
import json
import os
import re
import shutil
import signal
import subprocess
import tempfile
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from threading import Event, Lock, Timer
from typing import Callable, Iterable
from urllib.parse import parse_qsl, urlsplit

from . import __version__

SCHEMA_VERSION = 1
SEMVER_TAG = re.compile(r"^v?\d+\.\d+\.\d+(?:[-+][0-9A-Za-z.-]+)?$")
MAX_TAG_REFS = 100_000
MAX_GIT_OUTPUT_BYTES = 16 * 1024 * 1024
MAX_BASELINE_BYTES = 64 * 1024 * 1024
GIT_TIMEOUT_SECONDS = 60


class TagLedgerError(Exception):
    """Expected user-facing failure with a safe diagnostic."""


@dataclass(frozen=True, slots=True)
class TagRef:
    name: str
    object_oid: str
    target_oid: str
    kind: str


@dataclass(frozen=True, slots=True)
class Drift:
    tag: str
    change: str
    severity: str
    classification: str
    old_object_oid: str | None
    new_object_oid: str | None
    old_target_oid: str | None
    new_target_oid: str | None


def validate_source(source: str) -> str:
    """Accept local repository paths and supported Git transports while rejecting credential URLs."""
    if not isinstance(source, str) or not source.strip():
        raise TagLedgerError("Remote source cannot be empty.")
    try:
        source.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise TagLedgerError("Remote source must contain valid Unicode.") from exc
    if any(ord(char) < 32 or 127 <= ord(char) <= 159 for char in source):
        raise TagLedgerError("Remote source contains unsupported control characters.")
    try:
        local_path = Path(source).expanduser()
        local_directory = local_path.is_dir()
    except (OSError, RuntimeError) as exc:
        raise TagLedgerError(
            "Remote source is not a valid URL or repository path."
        ) from exc
    is_windows_path = os.name == "nt" and bool(re.match(r"^[a-zA-Z]:", source))
    is_unc_path = source.startswith("\\\\")
    is_absolute_path = source.startswith("/")
    is_scp_source = "://" not in source and bool(
        re.match(r"^(?:[^@/:\\\s]+@)?(?:\[[0-9a-fA-F:]+\]|[^/:\\\s]+):(?!:).+$", source)
    )
    if is_windows_path or is_unc_path or is_absolute_path or local_directory:
        return source
    try:
        parsed = urlsplit(source)
        username = parsed.username
        password = parsed.password
    except ValueError as exc:
        raise TagLedgerError(
            "Remote source is not a valid URL or repository path."
        ) from exc
    scheme = parsed.scheme.lower()
    if not scheme and not is_scp_source:
        return source
    if is_scp_source:
        secret_keys = {
            "token",
            "auth",
            "authorization",
            "password",
            "passwd",
            "secret",
            "credential",
            "credentials",
            "apikey",
            "key",
            "signature",
            "sig",
        }
        query_keys = (
            re.split(r"[^a-z0-9]+", key.casefold())
            for key, _ in parse_qsl(parsed.query, keep_blank_values=True)
        )
        if any(secret_keys.intersection(parts) for parts in query_keys):
            raise TagLedgerError(
                "Credential-like source parameters are not allowed; configure Git credential helpers instead."
            )
        return source
    if scheme not in {"http", "https", "ssh", "git"}:
        raise TagLedgerError(
            "Remote source must use HTTP(S), SSH, Git protocol, or a local repository path."
        )
    if password is not None or (
        scheme in {"http", "https", "git"} and username is not None
    ):
        raise TagLedgerError(
            "Credential-bearing URLs are not allowed; configure Git credential helpers instead."
        )
    secret_keys = {
        "token",
        "auth",
        "authorization",
        "password",
        "passwd",
        "secret",
        "credential",
        "credentials",
        "apikey",
        "key",
        "signature",
        "sig",
    }
    query_keys = (
        re.split(r"[^a-z0-9]+", key.casefold())
        for key, _ in parse_qsl(parsed.query, keep_blank_values=True)
    )
    if any(secret_keys.intersection(parts) for parts in query_keys):
        raise TagLedgerError(
            "Credential-like URL query parameters are not allowed; configure Git credential helpers instead."
        )
    if parsed.fragment:
        raise TagLedgerError("URL fragments are not accepted as Git sources.")
    return source


def utc_now() -> str:
    return (
        datetime.now(timezone.utc)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )


def parse_ls_remote(output: str) -> list[TagRef]:
    """Parse `git ls-remote --tags` output, including annotated-tag peel lines."""
    refs: dict[str, str] = {}
    peeled: dict[str, str] = {}
    oid_length: int | None = None
    for number, line in enumerate(output.splitlines(), 1):
        if not line:
            continue
        parts = line.split("\t", 1)
        if len(parts) != 2:
            raise TagLedgerError(f"Malformed git ls-remote output on line {number}.")
        oid, ref = parts
        if not re.fullmatch(r"[0-9a-fA-F]{40}|[0-9a-fA-F]{64}", oid):
            raise TagLedgerError(f"Malformed object ID on git ls-remote line {number}.")
        if oid_length is None:
            oid_length = len(oid)
        elif len(oid) != oid_length:
            raise TagLedgerError(
                "Malformed git ls-remote output: object ID lengths differ."
            )
        if not ref.startswith("refs/tags/"):
            continue
        suffix = "^{}"
        if ref.endswith(suffix):
            name = ref[len("refs/tags/") : -len(suffix)]
            if not _valid_tag_name(name):
                raise TagLedgerError(
                    f"Malformed peeled tag name on git ls-remote line {number}."
                )
            if name in peeled:
                raise TagLedgerError(
                    f"Duplicate peeled tag ref on git ls-remote line {number}."
                )
            peeled[name] = oid.lower()
        else:
            name = ref[len("refs/tags/") :]
            if not _valid_tag_name(name):
                raise TagLedgerError(
                    f"Malformed tag name on git ls-remote line {number}."
                )
            if name in refs:
                raise TagLedgerError(
                    f"Duplicate tag ref on git ls-remote line {number}."
                )
            if len(refs) >= MAX_TAG_REFS:
                raise TagLedgerError(
                    f"Git tag listing exceeds the {MAX_TAG_REFS} tag limit."
                )
            refs[name] = oid.lower()

    orphan_peels = sorted(set(peeled) - set(refs))
    if orphan_peels:
        raise TagLedgerError(
            "Malformed git ls-remote output: peeled tag has no matching ref."
        )

    result = []
    for name, oid in sorted(refs.items()):
        target = peeled.get(name, oid)
        result.append(
            TagRef(
                name=name,
                object_oid=oid,
                target_oid=target,
                kind="annotated" if name in peeled else "lightweight",
            )
        )
    return result


def fetch_remote_tags(source: str) -> list[TagRef]:
    validate_source(source)
    local_path = Path(source).expanduser()
    command = ["git", "-c", "protocol.allow=never", "ls-remote", "--tags", "--", source]
    local_query = local_path.is_dir()
    if local_query:
        command = [
            "git",
            "-C",
            str(local_path),
            "for-each-ref",
            "--format=%(refname)%09%(objectname)%09%(*objectname)",
            "refs/tags",
        ]
    try:
        environment = os.environ.copy()
        # Restrict transports even if a user's Git configuration enables custom remote helpers.
        environment["GIT_ALLOW_PROTOCOL"] = "http:https:ssh:git"
        environment["GIT_PROTOCOL_FROM_USER"] = "0"
        returncode, stdout = _run_git_command(command, environment)
    except FileNotFoundError as exc:
        raise TagLedgerError("Git was not found on PATH.") from exc
    except OSError as exc:
        raise TagLedgerError(
            "Git could not be started or its output could not be read."
        ) from exc
    if returncode != 0:
        # Git's stderr can include remote URLs or credential-helper details. Keep it out of reports.
        raise TagLedgerError(
            f"Git could not read tags from the configured remote (exit {returncode})."
        )
    if local_query:
        ls_remote_lines = []
        for number, line in enumerate(stdout.splitlines(), 1):
            fields = line.split("\t")
            if len(fields) != 3 or not fields[0].startswith("refs/tags/"):
                raise TagLedgerError(f"Malformed local tag listing on line {number}.")
            ref, object_oid, peeled_oid = fields
            ls_remote_lines.append(f"{object_oid}\t{ref}")
            if peeled_oid:
                ls_remote_lines.append(f"{peeled_oid}\t{ref}^{{}}")
        return parse_ls_remote("\n".join(ls_remote_lines))
    return parse_ls_remote(stdout)


def _run_git_command(
    command: list[str], environment: dict[str, str]
) -> tuple[int, str]:
    """Read bounded Git output while enforcing a wall-clock timeout."""
    if os.name == "nt":
        process_group_options = {
            "creationflags": subprocess.CREATE_NEW_PROCESS_GROUP | 0x00000004
        }  # CREATE_SUSPENDED until the job object is assigned
    else:
        process_group_options = {"start_new_session": True}
    process = subprocess.Popen(
        command,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        env=environment,
        **process_group_options,
    )
    if process.stdout is None:
        process.kill()
        process.wait()
        raise TagLedgerError("Could not capture Git tag listing output.")

    job_closer = _create_windows_kill_job(process)
    if os.name == "nt" and job_closer is None:
        process.kill()
        process.wait()
        process.stdout.close()
        raise TagLedgerError("Could not establish safe Git process-tree cleanup.")

    timed_out = Event()
    completed = Event()
    job_closer_holder = [job_closer]
    job_lock = Lock()

    def close_windows_job() -> bool:
        with job_lock:
            closer = job_closer_holder[0]
            job_closer_holder[0] = None
        if closer is None:
            return False
        try:
            return closer()
        except OSError:
            return False

    def kill_process_tree() -> None:
        if os.name == "nt":
            job_closed = close_windows_job()
            taskkill = (
                None
                if job_closed
                else shutil.which("taskkill.exe") or shutil.which("taskkill")
            )
            if taskkill is not None:
                try:
                    subprocess.run(
                        [taskkill, "/PID", str(process.pid), "/T", "/F"],
                        stdin=subprocess.DEVNULL,
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL,
                        timeout=5,
                        check=False,
                    )
                except (OSError, subprocess.TimeoutExpired):
                    pass
        else:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except OSError:
                pass
        try:
            if process.poll() is None:
                process.kill()
        except OSError:
            pass

    def stop_after_timeout() -> None:
        if not completed.is_set():
            timed_out.set()
            kill_process_tree()

    timer = Timer(GIT_TIMEOUT_SECONDS, stop_after_timeout)
    timer.daemon = True
    timer.start()
    output = bytearray()
    try:
        while True:
            read_size = min(65_536, MAX_GIT_OUTPUT_BYTES - len(output) + 1)
            chunk = process.stdout.read(read_size)
            if not chunk:
                break
            if len(output) + len(chunk) > MAX_GIT_OUTPUT_BYTES:
                raise TagLedgerError(
                    f"Git tag listing exceeds the {MAX_GIT_OUTPUT_BYTES // (1024 * 1024)} MiB output limit."
                )
            output.extend(chunk)
        returncode = process.wait()
        completed.set()
    except BaseException:
        kill_process_tree()
        process.wait()
        raise
    finally:
        timer.cancel()
        close_windows_job()
        process.stdout.close()

    if timed_out.is_set():
        raise TagLedgerError(
            f"Remote tag query timed out after {GIT_TIMEOUT_SECONDS:g} seconds."
        )
    return returncode, output.decode("utf-8", errors="replace")


def _create_windows_kill_job(
    process: subprocess.Popen[bytes],
) -> Callable[[], bool] | None:
    """Attach a Windows process to a job that kills descendants when closed."""
    if os.name != "nt":
        return None

    import ctypes
    from ctypes import wintypes

    class JobBasicLimits(ctypes.Structure):
        _fields_ = [
            ("PerProcessUserTimeLimit", ctypes.c_longlong),
            ("PerJobUserTimeLimit", ctypes.c_longlong),
            ("LimitFlags", wintypes.DWORD),
            ("MinimumWorkingSetSize", ctypes.c_size_t),
            ("MaximumWorkingSetSize", ctypes.c_size_t),
            ("ActiveProcessLimit", wintypes.DWORD),
            ("Affinity", ctypes.c_size_t),
            ("PriorityClass", wintypes.DWORD),
            ("SchedulingClass", wintypes.DWORD),
        ]

    class JobIoCounters(ctypes.Structure):
        _fields_ = [
            (field, ctypes.c_ulonglong)
            for field in (
                "ReadOperationCount",
                "WriteOperationCount",
                "OtherOperationCount",
                "ReadTransferCount",
                "WriteTransferCount",
                "OtherTransferCount",
            )
        ]

    class JobExtendedLimits(ctypes.Structure):
        _fields_ = [
            ("BasicLimitInformation", JobBasicLimits),
            ("IoInfo", JobIoCounters),
            ("ProcessMemoryLimit", ctypes.c_size_t),
            ("JobMemoryLimit", ctypes.c_size_t),
            ("PeakProcessMemoryUsed", ctypes.c_size_t),
            ("PeakJobMemoryUsed", ctypes.c_size_t),
        ]

    try:
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    except OSError:
        return None
    kernel32.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
    kernel32.CreateJobObjectW.restype = wintypes.HANDLE
    kernel32.SetInformationJobObject.argtypes = [
        wintypes.HANDLE,
        wintypes.INT,
        ctypes.c_void_p,
        wintypes.DWORD,
    ]
    kernel32.SetInformationJobObject.restype = wintypes.BOOL
    kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
    kernel32.AssignProcessToJobObject.restype = wintypes.BOOL
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.restype = wintypes.BOOL
    kernel32.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
    kernel32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE

    class ThreadEntry32(ctypes.Structure):
        _fields_ = [
            ("dwSize", wintypes.DWORD),
            ("cntUsage", wintypes.DWORD),
            ("th32ThreadID", wintypes.DWORD),
            ("th32OwnerProcessID", wintypes.DWORD),
            ("tpBasePri", wintypes.LONG),
            ("tpDeltaPri", wintypes.LONG),
            ("dwFlags", wintypes.DWORD),
        ]

    kernel32.Thread32First.argtypes = [
        wintypes.HANDLE,
        ctypes.POINTER(ThreadEntry32),
    ]
    kernel32.Thread32First.restype = wintypes.BOOL
    kernel32.Thread32Next.argtypes = [
        wintypes.HANDLE,
        ctypes.POINTER(ThreadEntry32),
    ]
    kernel32.Thread32Next.restype = wintypes.BOOL
    kernel32.OpenThread.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel32.OpenThread.restype = wintypes.HANDLE
    kernel32.ResumeThread.argtypes = [wintypes.HANDLE]
    kernel32.ResumeThread.restype = wintypes.DWORD

    job = kernel32.CreateJobObjectW(None, None)
    if not job:
        return None
    limits = JobExtendedLimits()
    limits.BasicLimitInformation.LimitFlags = 0x2000  # KILL_ON_JOB_CLOSE
    if not kernel32.SetInformationJobObject(
        job, 9, ctypes.byref(limits), ctypes.sizeof(limits)
    ):
        kernel32.CloseHandle(job)
        return None

    process_handle = kernel32.OpenProcess(0x0100 | 0x0001, False, process.pid)
    if not process_handle:
        kernel32.CloseHandle(job)
        return None
    try:
        assigned = kernel32.AssignProcessToJobObject(job, process_handle)
    finally:
        kernel32.CloseHandle(process_handle)
    if not assigned:
        kernel32.CloseHandle(job)
        return None

    snapshot = kernel32.CreateToolhelp32Snapshot(0x00000004, 0)  # SNAPTHREAD
    invalid_handle = ctypes.c_void_p(-1).value
    if snapshot == invalid_handle:
        kernel32.CloseHandle(job)
        return None
    resumed = False
    try:
        entry = ThreadEntry32()
        entry.dwSize = ctypes.sizeof(entry)
        has_thread = kernel32.Thread32First(snapshot, ctypes.byref(entry))
        while has_thread:
            if entry.th32OwnerProcessID == process.pid:
                thread = kernel32.OpenThread(
                    0x0002, False, entry.th32ThreadID
                )  # THREAD_SUSPEND_RESUME
                if thread:
                    try:
                        resumed = kernel32.ResumeThread(thread) != 0xFFFFFFFF
                    finally:
                        kernel32.CloseHandle(thread)
                break
            has_thread = kernel32.Thread32Next(snapshot, ctypes.byref(entry))
    finally:
        kernel32.CloseHandle(snapshot)
    if not resumed:
        kernel32.CloseHandle(job)
        return None

    return lambda: bool(kernel32.CloseHandle(job))


def filter_tags(tags: Iterable[TagRef], patterns: list[str] | None) -> list[TagRef]:
    patterns = ["*"] if patterns is None else patterns
    return [
        tag
        for tag in tags
        if any(fnmatch.fnmatchcase(tag.name, pattern) for pattern in patterns)
    ]


def tag_classification(name: str, mutable_patterns: list[str] | None = None) -> str:
    if mutable_patterns and any(
        fnmatch.fnmatchcase(name, pattern) for pattern in mutable_patterns
    ):
        return "mutable-alias"
    if SEMVER_TAG.fullmatch(name):
        return "version-like-fixed"
    return "unclassified"


def make_baseline(
    source: str,
    tags: Iterable[TagRef],
    include: list[str] | None = None,
    mutable: list[str] | None = None,
    captured_at: str | None = None,
) -> dict:
    validate_source(source)
    include = ["*"] if include is None else include
    mutable = [] if mutable is None else mutable
    _validate_patterns(include, "include_patterns", allow_empty=False)
    _validate_patterns(mutable, "mutable_patterns", allow_empty=True)
    baseline = {
        "schema_version": SCHEMA_VERSION,
        "source": source,
        "captured_at": captured_at or utc_now(),
        "include_patterns": include,
        "mutable_patterns": mutable,
        "tags": [
            asdict(tag)
            for tag in sorted(filter_tags(tags, include), key=lambda tag: tag.name)
        ],
    }
    return _validate_baseline(baseline)


def _validate_patterns(value: object, label: str, *, allow_empty: bool) -> list[str]:
    if not isinstance(value, list) or (not allow_empty and not value):
        expectation = (
            "a list of non-empty strings"
            if allow_empty
            else "a non-empty list of non-empty strings"
        )
        raise TagLedgerError(f"Baseline {label} must be {expectation}.")
    if any(not isinstance(item, str) or not item.strip() for item in value):
        raise TagLedgerError(f"Baseline {label} must contain only non-empty strings.")
    try:
        for item in value:
            item.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise TagLedgerError(f"Baseline {label} must contain valid Unicode.") from exc
    return value


def _valid_tag_name(name: str) -> bool:
    try:
        name.encode("utf-8")
    except UnicodeEncodeError:
        return False
    forbidden = set(" ~^:?*[\\\\")
    if (
        not name
        or name == "@"
        or name.startswith("/")
        or name.endswith(("/", "."))
        or ".." in name
        or "//" in name
        or "@{" in name
    ):
        return False
    if any(ord(char) < 32 or ord(char) == 127 or char in forbidden for char in name):
        return False
    return all(
        component and not component.startswith(".") and not component.endswith(".lock")
        for component in name.split("/")
    )


def _validate_baseline(data: object) -> dict:
    required = {
        "schema_version",
        "source",
        "captured_at",
        "include_patterns",
        "mutable_patterns",
        "tags",
    }
    if (
        not isinstance(data, dict)
        or type(data.get("schema_version")) is not int
        or data.get("schema_version") != SCHEMA_VERSION
        or set(data) != required
    ):
        raise TagLedgerError("Unsupported or malformed baseline schema.")
    if not isinstance(data.get("source"), str) or not isinstance(
        data.get("tags"), list
    ):
        raise TagLedgerError("Baseline must contain a source and a tags list.")
    if len(data["tags"]) > MAX_TAG_REFS:
        raise TagLedgerError(f"Baseline exceeds the {MAX_TAG_REFS} tag limit.")
    validate_source(data["source"])
    timestamp = data.get("captured_at")
    if not isinstance(timestamp, str):
        raise TagLedgerError(
            "Baseline captured_at must be a timezone-aware ISO 8601 timestamp."
        )
    try:
        parsed_time = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
    except ValueError as exc:
        raise TagLedgerError(
            "Baseline captured_at must be a timezone-aware ISO 8601 timestamp."
        ) from exc
    if parsed_time.tzinfo is None or parsed_time.utcoffset() is None:
        raise TagLedgerError(
            "Baseline captured_at must be a timezone-aware ISO 8601 timestamp."
        )
    _validate_patterns(data["include_patterns"], "include_patterns", allow_empty=False)
    _validate_patterns(data["mutable_patterns"], "mutable_patterns", allow_empty=True)
    names: set[str] = set()
    oid_length: int | None = None
    for row in data["tags"]:
        if not isinstance(row, dict) or set(row) != {
            "name",
            "object_oid",
            "target_oid",
            "kind",
        }:
            raise TagLedgerError("Baseline contains a malformed tag entry.")
        name = row["name"]
        if not isinstance(name, str) or not _valid_tag_name(name):
            raise TagLedgerError("Baseline contains an invalid tag name.")
        if name in names:
            raise TagLedgerError("Baseline contains duplicate tag names.")
        names.add(name)
        if not isinstance(row["kind"], str) or row["kind"] not in {
            "annotated",
            "lightweight",
        }:
            raise TagLedgerError("Baseline contains an invalid tag kind.")
        object_oid = row["object_oid"]
        target_oid = row["target_oid"]
        if not isinstance(object_oid, str) or not isinstance(target_oid, str):
            raise TagLedgerError("Baseline contains a malformed object ID.")
        if not re.fullmatch(
            r"[0-9a-f]{40}|[0-9a-f]{64}", object_oid
        ) or not re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", target_oid):
            raise TagLedgerError("Baseline contains a malformed object ID.")
        if len(object_oid) != len(target_oid):
            raise TagLedgerError(
                "Baseline tag object IDs use inconsistent hash lengths."
            )
        if oid_length is None:
            oid_length = len(object_oid)
        elif len(object_oid) != oid_length:
            raise TagLedgerError(
                "Baseline tag object IDs use inconsistent hash lengths."
            )
        if row["kind"] == "lightweight" and object_oid != target_oid:
            raise TagLedgerError(
                "Baseline lightweight tag object and target IDs must match."
            )
        if row["kind"] == "annotated" and object_oid == target_oid:
            raise TagLedgerError(
                "Baseline annotated tag object and target IDs must differ."
            )
    return data


def _unique_json_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise TagLedgerError("Baseline JSON contains a duplicate object key.")
        result[key] = value
    return result


def ensure_outside_git_metadata(path: str | Path) -> Path:
    """Reject output paths that resolve inside Git administrative metadata."""
    target = Path(path)
    try:
        resolved = target.resolve(strict=False)
    except (OSError, RuntimeError) as exc:
        raise TagLedgerError("Output path could not be checked safely.") from exc

    for ancestor in (resolved, *resolved.parents):
        if ancestor.name.casefold() == ".git":
            raise TagLedgerError("Output path cannot be inside Git metadata.")
        try:
            is_bare_git_dir = (
                (ancestor / "HEAD").is_file()
                and (ancestor / "config").is_file()
                and (ancestor / "objects").is_dir()
                and (ancestor / "refs").is_dir()
            )
            is_linked_worktree_git_dir = (ancestor / "gitdir").is_file() and (
                ancestor / "commondir"
            ).is_file()
        except OSError:
            continue
        if is_bare_git_dir or is_linked_worktree_git_dir:
            raise TagLedgerError("Output path cannot be inside Git metadata.")
    return target


def load_baseline(path: str | Path) -> dict:
    target = Path(path)
    try:
        if not target.exists():
            raise TagLedgerError(f"Baseline file does not exist: {target}")
        if not target.is_file():
            raise TagLedgerError(f"Baseline path is not a regular file: {target}")
        with target.open("rb") as stream:
            encoded = stream.read(MAX_BASELINE_BYTES + 1)
    except FileNotFoundError as exc:
        raise TagLedgerError(f"Baseline file does not exist: {target}") from exc
    except OSError as exc:
        raise TagLedgerError("Could not read baseline file.") from exc
    if len(encoded) > MAX_BASELINE_BYTES:
        raise TagLedgerError(
            f"Baseline file exceeds the {MAX_BASELINE_BYTES}-byte input limit."
        )
    try:
        data = json.loads(
            encoded.decode("utf-8"), object_pairs_hook=_unique_json_object
        )
    except (UnicodeError, ValueError, RecursionError) as exc:
        raise TagLedgerError(f"Baseline is not valid UTF-8 JSON: {target}") from exc
    return _validate_baseline(data)


def write_baseline(path: str | Path, baseline: dict) -> None:
    _validate_baseline(baseline)
    target = ensure_outside_git_metadata(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    body = json.dumps(baseline, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    if len(body.encode("utf-8")) > MAX_BASELINE_BYTES:
        raise TagLedgerError(
            f"Baseline exceeds the {MAX_BASELINE_BYTES}-byte size limit."
        )
    temporary_name = None
    try:
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=f".{target.name}.", suffix=".tmp", dir=target.parent
        )
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as stream:
            stream.write(body)
            stream.flush()
        os.replace(temporary_name, target)
    except OSError as exc:
        raise TagLedgerError(f"Could not write baseline file: {target}") from exc
    finally:
        if temporary_name is not None:
            try:
                Path(temporary_name).unlink(missing_ok=True)
            except OSError:
                pass


def compare_baseline(
    baseline: dict, current: Iterable[TagRef], checked_at: str | None = None
) -> dict:
    _validate_baseline(baseline)
    old_rows = {row["name"]: row for row in baseline["tags"]}
    selected = filter_tags(current, baseline.get("include_patterns", ["*"]))
    new_rows = {tag.name: tag for tag in selected}
    mutable = baseline.get("mutable_patterns", [])
    changes: list[Drift] = []

    def make_drift(name: str, kind: str, old: dict | None, new: TagRef | None) -> Drift:
        classification = tag_classification(name, mutable)
        if classification == "mutable-alias":
            severity = "warning"
        elif classification == "version-like-fixed":
            severity = (
                "error"
                if kind in {"deleted", "target-moved", "tag-object-changed"}
                else "info"
            )
        else:
            severity = (
                "warning"
                if kind in {"deleted", "target-moved", "tag-object-changed"}
                else "info"
            )
        return Drift(
            tag=name,
            change=kind,
            severity=severity,
            classification=classification,
            old_object_oid=old.get("object_oid") if old else None,
            new_object_oid=new.object_oid if new else None,
            old_target_oid=old.get("target_oid") if old else None,
            new_target_oid=new.target_oid if new else None,
        )

    for name in sorted(old_rows.keys() | new_rows.keys()):
        old, new = old_rows.get(name), new_rows.get(name)
        if old is None:
            changes.append(make_drift(name, "added", None, new))
        elif new is None:
            changes.append(make_drift(name, "deleted", old, None))
        elif old["target_oid"] != new.target_oid:
            changes.append(make_drift(name, "target-moved", old, new))
        elif old["object_oid"] != new.object_oid:
            changes.append(make_drift(name, "tag-object-changed", old, new))

    return {
        "schema_version": 1,
        "source": baseline["source"],
        "baseline_captured_at": baseline.get("captured_at"),
        "checked_at": checked_at or utc_now(),
        "summary": {
            "tag_count": len(new_rows),
            "change_count": len(changes),
            "error_count": sum(change.severity == "error" for change in changes),
            "warning_count": sum(change.severity == "warning" for change in changes),
            "info_count": sum(change.severity == "info" for change in changes),
        },
        "changes": [asdict(change) for change in changes],
    }


def report_exit_code(report: dict) -> int:
    return (
        1
        if report["summary"]["error_count"] or report["summary"]["warning_count"]
        else 0
    )


def render_sarif(report: dict) -> dict:
    rule_ids = {
        "target-moved": "TAG001",
        "tag-object-changed": "TAG002",
        "deleted": "TAG003",
        "added": "TAG004",
    }
    results = []
    for change in report["changes"]:
        rule_id = rule_ids[change["change"]]
        results.append(
            {
                "ruleId": rule_id,
                "level": {"error": "error", "warning": "warning", "info": "note"}[
                    change["severity"]
                ],
                "message": {
                    "text": f"Tag '{change['tag']}' {change['change']} ({change['classification']})."
                },
                "properties": {
                    "tag": change["tag"],
                    "oldObjectOid": change["old_object_oid"],
                    "newObjectOid": change["new_object_oid"],
                    "oldTargetOid": change["old_target_oid"],
                    "newTargetOid": change["new_target_oid"],
                },
            }
        )
    run = {
        "tool": {
            "driver": {
                "name": "TagLedger",
                "version": __version__,
                "rules": [{"id": key} for key in sorted(set(rule_ids.values()))],
            }
        },
        "results": results,
        "properties": {
            "source": report["source"],
            "baselineCapturedAt": report.get("baseline_captured_at"),
            "checkedAt": report["checked_at"],
        },
    }
    return {
        "$schema": "https://json.schemastore.org/sarif-2.1.0.json",
        "version": "2.1.0",
        "runs": [run],
    }


def render_html(report: dict) -> str:
    import html

    rows = []
    for change in report["changes"]:
        rows.append(
            '<tr data-severity="{severity}"><td><span class="pill {severity}">{severity}</span></td>'
            "<td><code>{tag}</code><small>{classification}</small></td><td>{kind}</td>"
            "<td><code>{old_object}</code></td><td><code>{new_object}</code></td>"
            "<td><code>{old_target}</code></td><td><code>{new_target}</code></td></tr>".format(
                severity=html.escape(change["severity"]),
                tag=html.escape(change["tag"]),
                classification=html.escape(change["classification"]),
                kind=html.escape(change["change"]),
                old_object=html.escape(change["old_object_oid"] or "—"),
                new_object=html.escape(change["new_object_oid"] or "—"),
                old_target=html.escape(change["old_target_oid"] or "—"),
                new_target=html.escape(change["new_target_oid"] or "—"),
            )
        )
    table_body = (
        "\n".join(rows)
        or '<tr><td colspan="7" class="empty">No tag changes found in this check.</td></tr>'
    )
    summary = report["summary"]
    severity = (
        "error"
        if summary["error_count"]
        else "warning"
        if summary["warning_count"]
        else "clean"
    )
    title = "Tag drift report"
    return f"""<!doctype html>
<html lang="en">
<head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>{title} · TagLedger</title>
<style>
:root{{--ink:#14252d;--paper:#edf1ef;--card:#fbfcf9;--muted:#587078;--line:#c7d2cf;--mint:#bde8d7;--amber:#cc741f;--red:#ad3b32;--blue:#1d6574;--mono:ui-monospace,"Cascadia Code",Consolas,monospace;--sans:Inter,"Segoe UI",sans-serif}}
*{{box-sizing:border-box}}body{{margin:0;background:var(--paper);color:var(--ink);font-family:var(--sans);line-height:1.5}}
.shell{{max-width:1280px;margin:0 auto;padding:36px clamp(18px,4vw,56px) 64px}}
header{{display:flex;justify-content:space-between;align-items:flex-start;border-bottom:1px solid var(--ink);padding-bottom:20px;gap:20px}}
.brand{{font:700 14px var(--mono);letter-spacing:.13em;text-transform:uppercase}}.brand:before{{content:"";display:inline-block;width:10px;height:10px;background:var(--blue);margin-right:10px;transform:rotate(45deg)}}
.stamp{{color:var(--muted);font:12px var(--mono);text-align:right}}h1{{font-size:clamp(38px,6vw,72px);letter-spacing:-.055em;line-height:.96;margin:38px 0 14px;max-width:800px}}.lede{{color:var(--muted);max-width:720px;margin:0 0 28px}}
.hero{{display:grid;grid-template-columns:minmax(0,1.55fr) minmax(220px,.8fr);gap:20px;align-items:stretch;margin:28px 0}}
.signal,.metrics{{background:var(--ink);color:var(--card);padding:24px 26px;min-height:180px;position:relative;overflow:hidden}}
.signal:after{{content:"";position:absolute;width:150px;height:150px;border:1px solid #47717b;border-radius:50%;right:-52px;top:-62px;box-shadow:0 0 0 18px #213a43,0 0 0 36px #1a3038}}
.eyebrow{{font:11px var(--mono);letter-spacing:.13em;text-transform:uppercase;color:var(--mint)}}.signal h2{{font-size:27px;letter-spacing:-.03em;margin:18px 0 6px;position:relative;z-index:1}}.signal p{{color:#bdd0d0;margin:0;position:relative;z-index:1}}
.metrics{{background:var(--card);border:1px solid var(--line);color:var(--ink);display:grid;grid-template-columns:repeat(2,1fr);gap:12px}}
.metric{{padding:8px 10px;border-left:2px solid var(--line)}}.metric strong{{display:block;font:600 30px var(--mono)}}.metric span{{font-size:12px;color:var(--muted)}}
.meta{{display:grid;grid-template-columns:130px minmax(0,1fr);gap:8px 14px;padding:18px 0;border-block:1px solid var(--line);font-size:13px}}.meta dt{{color:var(--muted);font:11px var(--mono);text-transform:uppercase}}.meta dd{{margin:0;overflow-wrap:anywhere;font-family:var(--mono)}}
.section-head{{display:flex;justify-content:space-between;align-items:end;gap:18px;margin:36px 0 12px}}h2.section-title{{font-size:20px;margin:0}}label{{font-size:12px;color:var(--muted)}}input{{font:inherit;padding:9px 11px;border:1px solid var(--line);background:var(--card);color:var(--ink);border-radius:0;min-width:min(280px,45vw)}}input:focus-visible{{outline:3px solid #4b9ca4;outline-offset:2px}}
.table-wrap{{overflow:auto;background:var(--card);border:1px solid var(--line)}}table{{border-collapse:collapse;width:100%;min-width:780px;text-align:left}}th{{padding:12px 14px;background:#dce7e3;color:var(--muted);font:11px var(--mono);letter-spacing:.08em;text-transform:uppercase}}td{{padding:14px;border-top:1px solid var(--line);vertical-align:top;font-size:13px}}td code{{font-family:var(--mono);font-size:12px;overflow-wrap:anywhere}}td small{{display:block;color:var(--muted);font:10px var(--mono);margin-top:5px}}.pill{{display:inline-block;padding:3px 7px;text-transform:uppercase;font:10px var(--mono);letter-spacing:.06em}}.pill.error{{background:#f4d3cc;color:#7c241f}}.pill.warning{{background:#f4dfc5;color:#81400f}}.pill.info{{background:#d5e9ea;color:#155663}}.empty{{padding:38px;text-align:center;color:var(--muted)}}
footer{{margin-top:28px;padding-top:14px;border-top:1px solid var(--line);font:11px var(--mono);color:var(--muted);display:flex;justify-content:space-between;gap:12px}}a{{color:inherit}}.sr-only{{position:absolute;width:1px;height:1px;padding:0;margin:-1px;overflow:hidden;clip:rect(0,0,0,0);white-space:nowrap;border:0}}
@media(max-width:760px){{.shell{{padding-top:22px}}header{{flex-direction:column}}.stamp{{text-align:left}}.hero{{grid-template-columns:1fr}}.section-head{{align-items:start;flex-direction:column}}input{{width:100%}}.meta{{grid-template-columns:100px 1fr}}footer{{flex-direction:column}}}}
@media(prefers-color-scheme:dark){{:root{{--ink:#e1ecea;--paper:#0c171b;--card:#142328;--muted:#a2b7b8;--line:#355057;--mint:#9ddbc3;--amber:#e9a15e;--red:#f07868;--blue:#62bac8}}th{{background:#203238}}}}
@media(prefers-reduced-motion:reduce){{*,*::before,*::after{{scroll-behavior:auto!important;transition:none!important}}}}
</style></head>
<body><main class="shell"><header><div class="brand">TagLedger / reference audit</div><div class="stamp">CHECKED {html.escape(report["checked_at"])}</div></header>
<h1>History leaves<br>fingerprints.</h1><p class="lede">A read-only comparison of the tag refs visible now against the baseline you recorded. This report identifies drift; it cannot tell which revision is authoritative.</p>
<section class="hero"><div class="signal"><div class="eyebrow">{severity.upper()} · SNAPSHOT COMPARISON</div><h2>{summary["change_count"]} tag changes</h2><p>{summary["tag_count"]} tags visible in this check · the baseline remains untouched.</p></div>
<div class="metrics"><div class="metric"><strong>{summary["error_count"]}</strong><span>fixed-version findings</span></div><div class="metric"><strong>{summary["warning_count"]}</strong><span>review findings</span></div><div class="metric"><strong>{summary["info_count"]}</strong><span>new refs</span></div><div class="metric"><strong>{summary["tag_count"]}</strong><span>current refs</span></div></div></section>
<dl class="meta"><dt>Remote</dt><dd>{html.escape(report["source"])}</dd><dt>Baseline</dt><dd>{html.escape(report.get("baseline_captured_at") or "unknown")}</dd><dt>Current check</dt><dd>{html.escape(report["checked_at"])}</dd></dl>
<div class="section-head"><h2 class="section-title">Tag ledger</h2><label>Filter by name, change or severity <input id="filter" type="search" placeholder="e.g. v2.4.1" aria-label="Filter tag changes"></label></div>
<div class="table-wrap"><table><caption class="sr-only">Tag object and target identities by tag</caption><thead><tr><th scope="col">Level</th><th scope="col">Tag</th><th scope="col">Change</th><th scope="col">Previous tag object</th><th scope="col">Current tag object</th><th scope="col">Previous target</th><th scope="col">Current target</th></tr></thead><tbody id="rows">{table_body}</tbody></table></div>
<footer><span>TagLedger · local report · no remote changes made</span><span>Object IDs are evidence, not a signature.</span></footer></main>
<script>const input=document.getElementById('filter');input.addEventListener('input',()=>{{const q=input.value.toLowerCase();document.querySelectorAll('#rows tr').forEach(row=>row.hidden=!row.textContent.toLowerCase().includes(q));}});</script>
</body></html>"""
