from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
from unittest.mock import patch
from pathlib import Path

from tagledger.core import (
    TagLedgerError,
    TagRef,
    _run_git_command,
    compare_baseline,
    ensure_outside_git_metadata,
    fetch_remote_tags,
    load_baseline,
    make_baseline,
    parse_ls_remote,
    render_html,
    render_sarif,
    report_exit_code,
    write_baseline,
    validate_source,
)
from tagledger.cli import main


class ParserTests(unittest.TestCase):
    def test_parses_annotated_and_lightweight_tags_and_peeled_target(self) -> None:
        output = (
            "a" * 40
            + "\trefs/tags/v1.0.0\n"
            + "b" * 40
            + "\trefs/tags/v1.0.0^{}\n"
            + "c" * 40
            + "\trefs/tags/stable\n"
            + "d" * 40
            + "\trefs/heads/main\n"
        )
        tags = parse_ls_remote(output)
        self.assertEqual(
            [(tag.name, tag.kind, tag.target_oid) for tag in tags],
            [
                ("stable", "lightweight", "c" * 40),
                ("v1.0.0", "annotated", "b" * 40),
            ],
        )

    def test_rejects_malformed_and_orphan_peeled_rows(self) -> None:
        with self.assertRaises(TagLedgerError):
            parse_ls_remote("bad row")
        with self.assertRaises(TagLedgerError):
            parse_ls_remote("a" * 40 + "\trefs/tags/missing^{}\n")

    def test_rejects_invalid_tag_names_and_mixed_object_formats(self) -> None:
        with self.assertRaisesRegex(TagLedgerError, "Malformed tag name"):
            parse_ls_remote("a" * 40 + "\trefs/tags/bad name\n")
        with self.assertRaisesRegex(TagLedgerError, "object ID lengths differ"):
            parse_ls_remote(
                "a" * 40 + "\trefs/tags/v1\n" + "b" * 64 + "\trefs/tags/v2\n"
            )

    def test_local_paths_with_hash_and_scp_style_sources_are_supported(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            repository = Path(temporary) / "repo#release"
            repository.mkdir()
            self.assertEqual(validate_source(str(repository)), str(repository))
        self.assertEqual(
            validate_source("github.com:owner/repo.git"), "github.com:owner/repo.git"
        )
        self.assertEqual(
            validate_source("git@github.com:owner/repo.git"),
            "git@github.com:owner/repo.git",
        )

    def test_rejects_control_characters_in_sources(self) -> None:
        for source in ("repo\x00name", "repo\nname", "repo\x1b[31m"):
            with (
                self.subTest(source=repr(source)),
                self.assertRaisesRegex(TagLedgerError, "control characters"),
            ):
                validate_source(source)

    def test_rejects_invalid_unicode_in_source_patterns_and_tag_names(self) -> None:
        invalid_unicode = "repo\ud800"
        with self.assertRaisesRegex(TagLedgerError, "valid Unicode"):
            validate_source(invalid_unicode)
        with self.assertRaisesRegex(TagLedgerError, "valid Unicode"):
            make_baseline("remote", [], include=["tag\ud800"])
        with self.assertRaisesRegex(TagLedgerError, "Malformed tag name"):
            parse_ls_remote("a" * 40 + "\trefs/tags/tag\ud800\n")

    def test_rejects_duplicate_tag_and_peeled_refs(self) -> None:
        with self.assertRaisesRegex(TagLedgerError, "Duplicate tag ref"):
            parse_ls_remote(
                "a" * 40 + "\trefs/tags/v1\n" + "b" * 40 + "\trefs/tags/v1\n"
            )
        with self.assertRaisesRegex(TagLedgerError, "Duplicate peeled tag ref"):
            parse_ls_remote(
                "a" * 40
                + "\trefs/tags/v1\n"
                + "b" * 40
                + "\trefs/tags/v1^{}\n"
                + "c" * 40
                + "\trefs/tags/v1^{}\n"
            )

    def test_rejects_tag_listing_over_ref_limit(self) -> None:
        with patch("tagledger.core.MAX_TAG_REFS", 2):
            output = "".join(
                f"{index:040x}\trefs/tags/v1.2.{index}\n" for index in range(3)
            )
            with self.assertRaisesRegex(TagLedgerError, "2 tag limit"):
                parse_ls_remote(output)

    def test_git_command_stops_when_output_exceeds_byte_limit(self) -> None:
        with patch("tagledger.core.MAX_GIT_OUTPUT_BYTES", 8):
            with self.assertRaisesRegex(TagLedgerError, "output limit"):
                _run_git_command(
                    [sys.executable, "-c", "print('more than eight bytes')"],
                    os.environ.copy(),
                )

    def test_git_command_enforces_wall_clock_timeout(self) -> None:
        with patch("tagledger.core.GIT_TIMEOUT_SECONDS", 0.05):
            with self.assertRaisesRegex(TagLedgerError, "timed out"):
                _run_git_command(
                    [sys.executable, "-c", "import time; time.sleep(10)"],
                    os.environ.copy(),
                )

    def test_timeout_kills_descendant_holding_stdout(self) -> None:
        child_code = "import time; time.sleep(10)"
        parent_code = (
            "import subprocess,sys,time; "
            f"subprocess.Popen([sys.executable, '-c', {child_code!r}]); "
            "print('child started', flush=True); time.sleep(10)"
        )
        started = time.perf_counter()
        with patch("tagledger.core.GIT_TIMEOUT_SECONDS", 1.0):
            with self.assertRaisesRegex(TagLedgerError, "timed out"):
                _run_git_command([sys.executable, "-c", parent_code], os.environ.copy())
        self.assertLess(time.perf_counter() - started, 8.0)

    def test_remote_urls_use_read_only_ls_remote(self) -> None:
        output = "a" * 40 + "\trefs/tags/v1.2.3\n"
        with patch("tagledger.core._run_git_command", return_value=(0, output)) as run:
            tags = fetch_remote_tags("https://example.invalid/org/repo.git")
        self.assertEqual(tags[0].name, "v1.2.3")
        self.assertEqual(
            run.call_args.args[0],
            [
                "git",
                "-c",
                "protocol.allow=never",
                "ls-remote",
                "--tags",
                "--",
                "https://example.invalid/org/repo.git",
            ],
        )
        self.assertEqual(
            run.call_args.args[1]["GIT_ALLOW_PROTOCOL"], "http:https:ssh:git"
        )

    def test_missing_git_uses_safe_diagnostic(self) -> None:
        with patch(
            "tagledger.core._run_git_command",
            side_effect=FileNotFoundError("private executable path"),
        ):
            with self.assertRaisesRegex(
                TagLedgerError, "Git was not found on PATH"
            ) as error:
                fetch_remote_tags("https://example.invalid/org/repo.git")
        self.assertNotIn("private executable path", str(error.exception))

    def test_rejects_credentials_in_remote_urls_before_calling_git(self) -> None:
        with patch("tagledger.core._run_git_command") as run:
            for source in (
                "https://token@example.invalid/org/repo.git",
                "https://user:secret@example.invalid/org/repo.git",
                "https://example.invalid/org/repo.git?access_token=secret",
                "https://example.invalid/org/repo.git?X-Amz-Signature=secret",
                "ssh://user:secret@example.invalid/org/repo.git",
            ):
                with self.subTest(source=source), self.assertRaises(TagLedgerError):
                    fetch_remote_tags(source)
        run.assert_not_called()

    def test_rejects_custom_git_transport_before_calling_git(self) -> None:
        with patch("tagledger.core._run_git_command") as run:
            for source in (
                "ext::sh -c echo",
                "custom-helper://host/repo",
                "file:///tmp/repo.git",
            ):
                with self.subTest(source=source), self.assertRaises(TagLedgerError):
                    fetch_remote_tags(source)
        run.assert_not_called()

    def test_rejects_credential_bearing_source_in_existing_baseline(self) -> None:
        from tagledger.core import load_baseline

        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "credential-baseline.json"
            path.write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "source": "https://token@example.invalid/org/repo.git",
                        "tags": [],
                    }
                ),
                encoding="utf-8",
            )
            with self.assertRaises(TagLedgerError):
                load_baseline(path)

    def test_load_baseline_rejects_oversized_file(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "large.json"
            path.write_bytes(b"0123456789")
            with (
                patch("tagledger.core.MAX_BASELINE_BYTES", 8),
                self.assertRaisesRegex(TagLedgerError, "byte input limit"),
            ):
                load_baseline(path)

    def test_write_baseline_refuses_to_create_oversized_file(self) -> None:
        baseline = make_baseline("remote", [])
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "oversized.json"
            with (
                patch("tagledger.core.MAX_BASELINE_BYTES", 8),
                self.assertRaisesRegex(TagLedgerError, "size limit"),
            ):
                write_baseline(path, baseline)
            self.assertFalse(path.exists())

    def test_output_guard_rejects_linked_worktree_admin_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            git_dir = Path(temporary) / "external-admin" / "worktrees" / "linked"
            git_dir.mkdir(parents=True)
            (git_dir / "gitdir").write_text("../worktree/.git\n", encoding="utf-8")
            (git_dir / "commondir").write_text("../..\n", encoding="utf-8")
            output = git_dir / "refs" / "tags" / "v1.0.0"
            with self.assertRaisesRegex(TagLedgerError, "inside Git metadata"):
                ensure_outside_git_metadata(output)

    def test_load_baseline_rejects_excessive_tag_count(self) -> None:
        baseline = make_baseline(
            "remote",
            [
                TagRef("v1", "a" * 40, "a" * 40, "lightweight"),
                TagRef("v2", "b" * 40, "b" * 40, "lightweight"),
            ],
        )
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "many-tags.json"
            path.write_text(json.dumps(baseline), encoding="utf-8")
            with (
                patch("tagledger.core.MAX_TAG_REFS", 1),
                self.assertRaisesRegex(TagLedgerError, "tag limit"),
            ):
                load_baseline(path)

    def test_load_baseline_wraps_json_recursion_error(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "recursive.json"
            path.write_text("{}", encoding="utf-8")
            with (
                patch("tagledger.core.json.loads", side_effect=RecursionError),
                self.assertRaisesRegex(TagLedgerError, "not valid UTF-8 JSON"),
            ):
                load_baseline(path)

    def test_load_baseline_wraps_json_integer_limit_error(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "integer-limit.json"
            path.write_text('{"schema_version":' + "9" * 5_000 + "}", encoding="utf-8")
            with self.assertRaisesRegex(TagLedgerError, "not valid UTF-8 JSON"):
                load_baseline(path)

    def test_load_baseline_rejects_deep_json_across_python_versions(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "deep.json"
            path.write_text("[" * 2_000 + "0" + "]" * 2_000, encoding="utf-8")
            with self.assertRaises(TagLedgerError):
                load_baseline(path)

    def test_load_baseline_fails_closed_on_patterns_and_tag_schema(self) -> None:
        from tagledger.core import make_baseline, parse_ls_remote

        baseline = make_baseline(
            "remote", parse_ls_remote("a" * 40 + "\trefs/tags/v1.0.0\n")
        )
        mutations = [
            ("include_patterns", []),
            ("include_patterns", 12),
            ("mutable_patterns", [None]),
        ]
        for key, value in mutations:
            malformed = dict(baseline)
            malformed[key] = value
            with tempfile.TemporaryDirectory() as temporary:
                path = Path(temporary) / "baseline.json"
                path.write_text(json.dumps(malformed), encoding="utf-8")
                with (
                    self.subTest(key=key, value=value),
                    self.assertRaises(TagLedgerError),
                ):
                    load_baseline(path)

        for key, value in (
            ("kind", []),
            ("kind", "unknown"),
            ("name", ""),
            ("name", "bad name"),
        ):
            malformed = json.loads(json.dumps(baseline))
            malformed["tags"][0][key] = value
            with tempfile.TemporaryDirectory() as temporary:
                path = Path(temporary) / "baseline.json"
                path.write_text(json.dumps(malformed), encoding="utf-8")
                with (
                    self.subTest(tag_field=key, value=value),
                    self.assertRaises(TagLedgerError),
                ):
                    load_baseline(path)

        for key, value in (("kind", "annotated"), ("target_oid", "b" * 40)):
            malformed = json.loads(json.dumps(baseline))
            malformed["tags"][0][key] = value
            with tempfile.TemporaryDirectory() as temporary:
                path = Path(temporary) / "baseline.json"
                path.write_text(json.dumps(malformed), encoding="utf-8")
                with self.subTest(tag_relation=key), self.assertRaises(TagLedgerError):
                    load_baseline(path)

    def test_loaded_baseline_rejects_mixed_object_formats(self) -> None:
        baseline = make_baseline(
            "remote", [TagRef("v1", "a" * 40, "a" * 40, "lightweight")]
        )
        malformed = json.loads(json.dumps(baseline))
        malformed["tags"].append(
            {
                "name": "v2",
                "object_oid": "b" * 64,
                "target_oid": "b" * 64,
                "kind": "lightweight",
            }
        )
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "baseline.json"
            path.write_text(json.dumps(malformed), encoding="utf-8")
            with self.assertRaisesRegex(TagLedgerError, "inconsistent hash lengths"):
                load_baseline(path)

    def test_load_baseline_rejects_duplicate_json_keys(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "duplicate-keys.json"
            path.write_text(
                '{"schema_version": 1, "schema_version": 1}', encoding="utf-8"
            )
            with self.assertRaisesRegex(TagLedgerError, "duplicate object key"):
                load_baseline(path)

    def test_make_baseline_sorts_tag_records(self) -> None:
        baseline = make_baseline(
            "remote",
            [
                TagRef("z", "b" * 40, "b" * 40, "lightweight"),
                TagRef("a", "a" * 40, "a" * 40, "lightweight"),
            ],
        )
        self.assertEqual([row["name"] for row in baseline["tags"]], ["a", "z"])


class CompareTests(unittest.TestCase):
    def test_classifies_move_delete_metadata_change_new_and_mutable_alias(self) -> None:
        old = parse_ls_remote(
            "1" * 40
            + "\trefs/tags/v1.0.0\n"
            + "2" * 40
            + "\trefs/tags/stable\n"
            + "3" * 40
            + "\trefs/tags/v2.0.0\n"
            + "4" * 40
            + "\trefs/tags/v3.0.0\n"
        )
        baseline = make_baseline("remote", old, mutable=["stable"])
        current = parse_ls_remote(
            "a" * 40
            + "\trefs/tags/v1.0.0\n"
            + "8" * 40
            + "\trefs/tags/stable\n"
            + "5" * 40
            + "\trefs/tags/v3.0.0\n"
            + "4" * 40
            + "\trefs/tags/v3.0.0^{}\n"
            + "7" * 40
            + "\trefs/tags/v4.0.0\n"
        )
        report = compare_baseline(baseline, current, checked_at="now")
        changes = {row["tag"]: row for row in report["changes"]}
        self.assertEqual(changes["v1.0.0"]["change"], "target-moved")
        self.assertEqual(changes["v1.0.0"]["severity"], "error")
        self.assertEqual(changes["stable"]["change"], "target-moved")
        self.assertEqual(changes["stable"]["severity"], "warning")
        self.assertEqual(changes["v2.0.0"]["change"], "deleted")
        self.assertEqual(changes["v3.0.0"]["change"], "tag-object-changed")
        self.assertEqual(changes["v4.0.0"]["change"], "added")
        self.assertEqual(report_exit_code(report), 1)

    def test_pattern_filter_and_empty_clean_report(self) -> None:
        baseline = make_baseline(
            "remote", parse_ls_remote("1" * 40 + "\trefs/tags/v1.0.0\n"), include=["v*"]
        )
        report = compare_baseline(
            baseline, parse_ls_remote("2" * 40 + "\trefs/tags/dev\n")
        )
        self.assertEqual(report["changes"][0]["change"], "deleted")
        clean = compare_baseline(make_baseline("remote", []), [])
        self.assertEqual(clean["summary"]["change_count"], 0)
        self.assertEqual(report_exit_code(clean), 0)

    def test_info_only_added_refs_keep_success_exit_code(self) -> None:
        report = compare_baseline(
            make_baseline("remote", []),
            parse_ls_remote("a" * 40 + "\trefs/tags/new-tag\n"),
        )
        self.assertEqual(report["summary"]["info_count"], 1)
        self.assertEqual(report_exit_code(report), 0)

    def test_html_escapes_untrusted_remote_and_tag_names(self) -> None:
        baseline = make_baseline("<img src=x onerror=alert(1)>", [])
        current = [
            type(
                "Tag",
                (),
                {
                    "name": "<script>alert(1)</script>",
                    "object_oid": "a" * 40,
                    "target_oid": "a" * 40,
                    "kind": "lightweight",
                },
            )()
        ]
        report = compare_baseline(baseline, current, checked_at="now")
        html = render_html(report)
        self.assertNotIn("<script>alert(1)</script>", html)
        self.assertIn("&lt;script&gt;", html)
        self.assertNotIn("<img src=x", html)

    def test_html_shows_tag_object_and_target_oids_for_annotated_tag_rewrite(
        self,
    ) -> None:
        baseline_tags = parse_ls_remote(
            "a" * 40 + "\trefs/tags/v1.0.0\n" + "b" * 40 + "\trefs/tags/v1.0.0^{}\n"
        )
        current_tags = parse_ls_remote(
            "c" * 40 + "\trefs/tags/v1.0.0\n" + "b" * 40 + "\trefs/tags/v1.0.0^{}\n"
        )
        baseline = make_baseline("remote", baseline_tags)
        report = compare_baseline(baseline, current_tags, checked_at="now")

        rendered = render_html(report)
        change = report["changes"][0]
        self.assertEqual(change["change"], "tag-object-changed")
        self.assertIn("Previous tag object", rendered)
        self.assertIn("Current tag object", rendered)
        self.assertIn(
            "<td><code>"
            + "a" * 40
            + "</code></td><td><code>"
            + "c" * 40
            + "</code></td>",
            rendered,
        )
        self.assertIn(
            "<td><code>"
            + "b" * 40
            + "</code></td><td><code>"
            + "b" * 40
            + "</code></td>",
            rendered,
        )

    def test_sarif_serializes_drift_rule_and_object_ids(self) -> None:
        old = make_baseline(
            "remote", parse_ls_remote("1" * 40 + "\trefs/tags/v1.0.0\n")
        )
        report = compare_baseline(
            old, parse_ls_remote("2" * 40 + "\trefs/tags/v1.0.0\n")
        )
        sarif = render_sarif(report)
        result = sarif["runs"][0]["results"][0]
        self.assertEqual(result["ruleId"], "TAG001")
        self.assertEqual(result["properties"]["newTargetOid"], "2" * 40)
        self.assertEqual(sarif["runs"][0]["properties"]["source"], "remote")
        self.assertEqual(
            sarif["runs"][0]["properties"]["checkedAt"], report["checked_at"]
        )


class GitIntegrationTests(unittest.TestCase):
    def git(
        self, *args: str, cwd: Path | None = None, input_text: str | None = None
    ) -> str:
        proc = subprocess.run(
            ["git", *args],
            cwd=cwd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            check=False,
            input=input_text,
        )
        if proc.returncode:
            self.fail(f"git {' '.join(args)} failed: {proc.stderr}")
        return proc.stdout.strip()

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(dir=Path(__file__).parents[1])
        self.root = Path(self.temp.name)
        self.remote = self.root / "remote.git"
        self.git("init", "--bare", str(self.remote))
        self.commit1 = self.make_commit("sample.txt", "first\n")
        self.commit2 = self.make_commit("sample.txt", "second\n", parent=self.commit1)
        self.git("tag", "v1.0.0", self.commit1, cwd=self.remote)
        self.git("tag", "-a", "v2.0.0", "-m", "release", self.commit1, cwd=self.remote)

    def make_commit(
        self, filename: str, contents: str, parent: str | None = None
    ) -> str:
        source = self.root / f"input-{len(list(self.root.glob('input-*')))}.txt"
        source.write_text(contents, encoding="utf-8")
        blob = self.git("hash-object", "-w", str(source), cwd=self.remote)
        tree = self.git(
            "mktree", cwd=self.remote, input_text=f"100644 blob {blob}\t{filename}\n"
        )
        args = [
            "-c",
            "user.name=TagLedger Test",
            "-c",
            "user.email=tagledger@example.invalid",
            "commit-tree",
            tree,
            "-m",
            "fixture",
        ]
        if parent:
            args.extend(["-p", parent])
        return self.git(*args, cwd=self.remote)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_cli_maps_report_path_resolution_loop_to_exit_code_2(self) -> None:
        baseline_path = self.root / "loop-baseline.json"
        output_path = self.root / "loop-report.json"
        write_baseline(
            baseline_path,
            make_baseline(str(self.remote), fetch_remote_tags(str(self.remote))),
        )
        stderr = StringIO()
        arguments = [
            "tagledger",
            "verify",
            str(baseline_path),
            "--json",
            str(output_path),
        ]
        with (
            patch("sys.argv", arguments),
            patch.object(Path, "resolve", side_effect=RuntimeError("symlink loop")),
            redirect_stderr(stderr),
            self.assertRaises(SystemExit) as exit_info,
        ):
            main()
        self.assertEqual(exit_info.exception.code, 2)
        self.assertIn("could not be checked safely", stderr.getvalue())

    def test_cli_rejects_control_characters_in_baseline_source_cleanly(self) -> None:
        baseline = make_baseline(str(self.remote), fetch_remote_tags(str(self.remote)))
        baseline["source"] = "invalid\x00source"
        baseline_path = self.root / "invalid-source.json"
        baseline_path.write_text(json.dumps(baseline), encoding="utf-8")
        stderr = StringIO()
        with (
            patch("sys.argv", ["tagledger", "verify", str(baseline_path)]),
            redirect_stderr(stderr),
            self.assertRaises(SystemExit) as exit_info,
        ):
            main()
        self.assertEqual(exit_info.exception.code, 2)
        self.assertIn("control characters", stderr.getvalue())
        self.assertNotIn("Traceback", stderr.getvalue())

    def test_reads_local_bare_remote_and_keeps_lightweight_and_annotated_shape(
        self,
    ) -> None:
        tags = fetch_remote_tags(str(self.remote))
        self.assertEqual(
            {tag.name: tag.kind for tag in tags},
            {"v1.0.0": "lightweight", "v2.0.0": "annotated"},
        )
        self.assertNotEqual(
            next(tag for tag in tags if tag.name == "v2.0.0").object_oid,
            next(tag for tag in tags if tag.name == "v2.0.0").target_oid,
        )

    def test_moving_a_remote_tag_is_detected_without_mutating_remote(self) -> None:
        baseline = make_baseline(str(self.remote), fetch_remote_tags(str(self.remote)))
        original_ref = self.git("rev-parse", "refs/tags/v1.0.0", cwd=self.remote)
        self.git("update-ref", "refs/tags/v1.0.0", self.commit2, cwd=self.remote)
        report = compare_baseline(baseline, fetch_remote_tags(str(self.remote)))
        self.assertEqual(report["changes"][0]["change"], "target-moved")
        self.assertEqual(report["changes"][0]["severity"], "error")
        self.assertNotEqual(original_ref, report["changes"][0]["new_object_oid"])
        after = fetch_remote_tags(str(self.remote))
        self.assertEqual(after[0].target_oid, self.commit2)

    def test_baseline_round_trip_is_stable_and_atomic(self) -> None:
        baseline = make_baseline(
            str(self.remote),
            fetch_remote_tags(str(self.remote)),
            captured_at="2026-01-01T00:00:00Z",
        )
        path = self.root / "history" / "baseline.json"
        write_baseline(path, baseline)
        loaded = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(loaded, baseline)
        self.assertFalse(list(path.parent.glob(f".{path.name}.*.tmp")))

    def test_baseline_write_does_not_follow_preexisting_temp_symlink(self) -> None:
        baseline = make_baseline(
            str(self.remote),
            fetch_remote_tags(str(self.remote)),
            captured_at="2026-01-01T00:00:00Z",
        )
        path = self.root / "baseline.json"
        target = self.root / "outside.txt"
        target.write_text("preserve me", encoding="utf-8")
        legacy_temp = path.with_name(path.name + ".tmp")
        linked = True
        try:
            legacy_temp.symlink_to(target)
        except OSError:
            linked = False
            legacy_temp.write_text("reserved temp name", encoding="utf-8")

        write_baseline(path, baseline)
        self.assertEqual(target.read_text(encoding="utf-8"), "preserve me")
        if linked:
            self.assertTrue(legacy_temp.is_symlink())
        else:
            self.assertEqual(
                legacy_temp.read_text(encoding="utf-8"), "reserved temp name"
            )

    def test_verify_refuses_report_path_aliases_of_baseline(self) -> None:
        from argparse import Namespace
        from tagledger.cli import run

        baseline_path = self.root / "baseline.json"
        write_baseline(
            baseline_path,
            make_baseline(str(self.remote), fetch_remote_tags(str(self.remote))),
        )
        original = baseline_path.read_bytes()
        with redirect_stdout(StringIO()), redirect_stderr(StringIO()):
            with self.assertRaisesRegex(
                TagLedgerError, "cannot overwrite its baseline"
            ):
                run(
                    Namespace(
                        command="verify",
                        baseline=str(baseline_path),
                        source=None,
                        html=None,
                        sarif=None,
                        json_path=str(baseline_path),
                    )
                )
        self.assertEqual(baseline_path.read_bytes(), original)

    def test_verify_refuses_hardlink_alias_of_baseline(self) -> None:
        from argparse import Namespace
        from tagledger.cli import run

        baseline_path = self.root / "baseline-hardlink.json"
        write_baseline(
            baseline_path,
            make_baseline(str(self.remote), fetch_remote_tags(str(self.remote))),
        )
        hardlink_path = self.root / "baseline-hardlink-alias.json"
        try:
            os.link(baseline_path, hardlink_path)
        except OSError as exc:
            self.skipTest(f"hard links are unavailable in this environment: {exc}")

        original = baseline_path.read_bytes()
        with self.assertRaisesRegex(TagLedgerError, "cannot overwrite its baseline"):
            run(
                Namespace(
                    command="verify",
                    baseline=str(baseline_path),
                    source=None,
                    html=None,
                    sarif=None,
                    json_path=str(hardlink_path),
                )
            )
        self.assertEqual(baseline_path.read_bytes(), original)

    def test_verify_uses_samefile_to_reject_report_aliases(self) -> None:
        from argparse import Namespace
        from tagledger.cli import run

        baseline_path = self.root / "baseline-samefile.json"
        report_path = self.root / "report-samefile.json"
        write_baseline(
            baseline_path,
            make_baseline(str(self.remote), fetch_remote_tags(str(self.remote))),
        )
        report_path.write_text("preserve me", encoding="utf-8")
        with (
            patch("tagledger.cli.os.path.samefile", return_value=True) as samefile,
            self.assertRaisesRegex(TagLedgerError, "cannot overwrite its baseline"),
        ):
            run(
                Namespace(
                    command="verify",
                    baseline=str(baseline_path),
                    source=None,
                    html=None,
                    sarif=None,
                    json_path=str(report_path),
                )
            )
        samefile.assert_called_once_with(report_path, baseline_path)
        self.assertEqual(report_path.read_text(encoding="utf-8"), "preserve me")

    def test_verify_rejects_colliding_report_paths(self) -> None:
        from argparse import Namespace
        from tagledger.cli import run

        baseline_path = self.root / "baseline.json"
        write_baseline(
            baseline_path,
            make_baseline(str(self.remote), fetch_remote_tags(str(self.remote))),
        )
        with self.assertRaisesRegex(TagLedgerError, "paths must be distinct"):
            run(
                Namespace(
                    command="verify",
                    baseline=str(baseline_path),
                    source=None,
                    html=str(self.root / "report.out"),
                    sarif=None,
                    json_path=str(self.root / "report.out"),
                )
            )

    def test_snapshot_and_verify_refuse_git_metadata_output_paths(self) -> None:
        from argparse import Namespace
        from tagledger.cli import run

        ref_path = self.remote / "refs" / "tags" / "v1.0.0"
        self.git("update-ref", "refs/tags/v1.0.0", self.commit1, cwd=self.remote)
        original_ref = ref_path.read_bytes()
        snapshot_args = Namespace(
            command="snapshot",
            source=str(self.remote),
            output=str(ref_path),
            include=None,
            mutable=None,
            allow_empty=False,
        )
        with (
            patch("tagledger.cli.fetch_remote_tags") as fetch_remote,
            self.assertRaisesRegex(TagLedgerError, "inside Git metadata"),
        ):
            run(snapshot_args)
        fetch_remote.assert_not_called()
        self.assertEqual(ref_path.read_bytes(), original_ref)

        baseline_path = self.root / "safe-baseline.json"
        write_baseline(
            baseline_path,
            make_baseline(str(self.remote), fetch_remote_tags(str(self.remote))),
        )
        verify_args = Namespace(
            command="verify",
            baseline=str(baseline_path),
            source=None,
            html=None,
            sarif=None,
            json_path=str(ref_path),
        )
        with self.assertRaisesRegex(TagLedgerError, "inside Git metadata"):
            run(verify_args)
        self.assertEqual(ref_path.read_bytes(), original_ref)

    def test_output_guard_rejects_real_linked_worktree_admin_metadata(self) -> None:
        repository = self.root / "worktree-source"
        linked_worktree = self.root / "linked-worktree"
        repository.mkdir()
        self.git("init", str(repository))
        (repository / "tracked.txt").write_text("fixture\n", encoding="utf-8")
        self.git("add", "tracked.txt", cwd=repository)
        self.git(
            "-c",
            "user.name=TagLedger Test",
            "-c",
            "user.email=tagledger@example.invalid",
            "commit",
            "-m",
            "fixture",
            cwd=repository,
        )
        self.git("worktree", "add", str(linked_worktree), "HEAD", cwd=repository)
        admin_dir = Path(
            self.git("rev-parse", "--absolute-git-dir", cwd=linked_worktree)
        )
        output = admin_dir / "refs" / "tags" / "v1.0.0"
        with self.assertRaisesRegex(TagLedgerError, "inside Git metadata"):
            ensure_outside_git_metadata(output)

    def test_cli_snapshot_verify_and_reports_are_end_to_end(self) -> None:
        from argparse import Namespace
        from tagledger.cli import run

        baseline_path = self.root / "baseline.json"
        with redirect_stdout(StringIO()):
            self.assertEqual(
                run(
                    Namespace(
                        command="snapshot",
                        source=str(self.remote),
                        output=str(baseline_path),
                        include=None,
                        mutable=None,
                        allow_empty=False,
                    )
                ),
                0,
            )
        original_baseline = baseline_path.read_bytes()
        self.git("update-ref", "refs/tags/v1.0.0", self.commit2, cwd=self.remote)

        html_path = self.root / "report.html"
        sarif_path = self.root / "report.sarif"
        json_path = self.root / "report.json"
        output = StringIO()
        with redirect_stdout(output):
            code = run(
                Namespace(
                    command="verify",
                    baseline=str(baseline_path),
                    source=None,
                    html=str(html_path),
                    sarif=str(sarif_path),
                    json_path=str(json_path),
                )
            )

        self.assertEqual(code, 1)
        self.assertIn("target-moved", output.getvalue())
        self.assertEqual(
            baseline_path.read_bytes(),
            original_baseline,
            "verify must not rewrite its baseline",
        )
        sarif = json.loads(sarif_path.read_text(encoding="utf-8"))
        self.assertEqual(sarif["version"], "2.1.0")
        self.assertIn(
            "TAG001", {item["ruleId"] for item in sarif["runs"][0]["results"]}
        )
        report = json.loads(json_path.read_text(encoding="utf-8"))
        self.assertEqual(report["source"], str(self.remote))
        html = html_path.read_text(encoding="utf-8")
        self.assertIn("TagLedger", html)
        self.assertIn("prefers-color-scheme:dark", html)

    def test_cli_rejects_empty_snapshot_unless_explicitly_allowed(self) -> None:
        from argparse import Namespace
        from tagledger.cli import run

        empty_remote = self.root / "empty.git"
        self.git("init", "--bare", str(empty_remote))
        baseline_path = self.root / "empty-baseline.json"
        args = Namespace(
            command="snapshot",
            source=str(empty_remote),
            output=str(baseline_path),
            include=None,
            mutable=None,
            allow_empty=False,
        )
        with self.assertRaises(TagLedgerError):
            run(args)
        self.assertFalse(baseline_path.exists())

        filtered_path = self.root / "filtered-baseline.json"
        filtered_args = Namespace(
            command="snapshot",
            source=str(self.remote),
            output=str(filtered_path),
            include=["v99.*"],
            mutable=None,
            allow_empty=False,
        )
        with self.assertRaises(TagLedgerError):
            run(filtered_args)
        self.assertFalse(
            filtered_path.exists(),
            "a non-matching include pattern must not create an empty baseline",
        )

        args.allow_empty = True
        with redirect_stdout(StringIO()):
            self.assertEqual(run(args), 0)
        self.assertEqual(
            json.loads(baseline_path.read_text(encoding="utf-8"))["tags"], []
        )

    def test_cli_requires_explicit_advance_and_fixed_drift_acceptance(self) -> None:
        from argparse import Namespace
        from tagledger.cli import run

        baseline_path = self.root / "advance-baseline.json"
        write_baseline(
            baseline_path,
            make_baseline(str(self.remote), fetch_remote_tags(str(self.remote))),
        )
        original = baseline_path.read_bytes()
        self.git("update-ref", "refs/tags/v1.0.0", self.commit2, cwd=self.remote)
        args = Namespace(
            command="advance",
            baseline=str(baseline_path),
            yes=False,
            accept_changes=False,
        )

        with self.assertRaises(TagLedgerError):
            run(args)
        args.yes = True
        with redirect_stdout(StringIO()), redirect_stderr(StringIO()):
            with self.assertRaises(TagLedgerError):
                run(args)
        self.assertEqual(baseline_path.read_bytes(), original)

        args.accept_changes = True
        with redirect_stdout(StringIO()):
            self.assertEqual(run(args), 0)
        advanced = json.loads(baseline_path.read_text(encoding="utf-8"))
        self.assertEqual(
            next(tag for tag in advanced["tags"] if tag["name"] == "v1.0.0")[
                "target_oid"
            ],
            self.commit2,
        )


if __name__ == "__main__":
    unittest.main()
