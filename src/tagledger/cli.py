from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
from pathlib import Path

from . import __version__
from .core import (
    TagLedgerError,
    compare_baseline,
    fetch_remote_tags,
    make_baseline,
    load_baseline,
    render_html,
    render_sarif,
    report_exit_code,
    write_baseline,
    ensure_outside_git_metadata,
)


def _patterns(values: list[str] | None) -> list[str] | None:
    if values is None:
        return None
    cleaned = [value.strip() for value in values if value.strip()]
    if not cleaned:
        raise TagLedgerError("At least one non-empty tag pattern is required.")
    return cleaned


def _text_report(report: dict) -> str:
    summary = report["summary"]
    lines = [
        f"TagLedger: {summary['change_count']} change(s) across {summary['tag_count']} current tag(s)",
        f"Source: {report['source']}",
        f"Baseline: {report.get('baseline_captured_at') or 'unknown'} | Checked: {report['checked_at']}",
    ]
    for row in report["changes"]:
        lines.append(
            f"{row['severity'].upper():7} {row['change']:18} {row['tag']} "
            f"[{row['classification']}] {row['old_target_oid'] or '∅'} -> {row['new_target_oid'] or '∅'}"
        )
    if not report["changes"]:
        lines.append("No tag changes found.")
    lines.append(
        "Object IDs identify Git objects; they do not prove release authenticity."
    )
    return "\n".join(lines)


def _same_path(first: Path, second: Path) -> bool:
    try:
        if first.resolve(strict=False) == second.resolve(strict=False):
            return True
        if first.exists() and second.exists():
            return os.path.samefile(first, second)
    except (OSError, RuntimeError) as exc:
        raise TagLedgerError(
            "A report output path could not be checked safely."
        ) from exc
    return False


def _ensure_report_targets(baseline: str | Path, outputs: list[str | None]) -> None:
    destinations = [Path(output) for output in outputs if output]
    baseline_path = Path(baseline)
    for destination in destinations:
        ensure_outside_git_metadata(destination)
    for index, destination in enumerate(destinations):
        if _same_path(destination, baseline_path):
            raise TagLedgerError("Verify report output cannot overwrite its baseline.")
        if any(_same_path(destination, other) for other in destinations[index + 1 :]):
            raise TagLedgerError("Verify report output paths must be distinct.")


def _write_report(path: str | Path, content: str) -> None:
    destination = ensure_outside_git_metadata(path)
    temporary_name: str | None = None
    try:
        destination.parent.mkdir(parents=True, exist_ok=True)
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=f".{destination.name}.", suffix=".tmp", dir=destination.parent
        )
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as stream:
            stream.write(content)
            stream.flush()
        os.replace(temporary_name, destination)
    except OSError as exc:
        raise TagLedgerError("Could not write report output file.") from exc
    finally:
        if temporary_name is not None:
            try:
                Path(temporary_name).unlink(missing_ok=True)
            except OSError:
                pass


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="tagledger",
        description="Compare Git tag refs to an explicit baseline. Remote checks are read-only.",
    )
    parser.add_argument(
        "--version", action="version", version=f"tagledger {__version__}"
    )
    sub = parser.add_subparsers(dest="command", required=True)

    snapshot = sub.add_parser(
        "snapshot", help="record the visible tag refs as a new baseline"
    )
    snapshot.add_argument("source", help="Git URL or local repository path")
    snapshot.add_argument("-o", "--output", required=True, help="baseline JSON path")
    snapshot.add_argument(
        "--include", action="append", help="tag glob to include; repeatable"
    )
    snapshot.add_argument(
        "--mutable", action="append", help="tag glob that may move, e.g. v1 or stable"
    )
    snapshot.add_argument(
        "--allow-empty",
        action="store_true",
        help="allow a baseline with no matching tags",
    )

    verify = sub.add_parser(
        "verify", help="compare a baseline with the source's current tags"
    )
    verify.add_argument("baseline", help="baseline JSON path")
    verify.add_argument("--source", help="override the source stored in the baseline")
    verify.add_argument("--html", help="write a self-contained HTML report")
    verify.add_argument("--sarif", help="write a SARIF 2.1.0 report")
    verify.add_argument("--json", dest="json_path", help="write a JSON report")

    advance = sub.add_parser(
        "advance", help="replace a baseline after an explicit review"
    )
    advance.add_argument("baseline", help="baseline JSON path")
    advance.add_argument(
        "--yes",
        action="store_true",
        help="confirm that you intend to replace the baseline",
    )
    advance.add_argument(
        "--accept-changes",
        action="store_true",
        help="allow advancement despite fixed-version drift",
    )

    return parser


def run(args: argparse.Namespace) -> int:
    if args.command == "snapshot":
        ensure_outside_git_metadata(args.output)
        include = _patterns(args.include)
        mutable = _patterns(args.mutable)
        tags = fetch_remote_tags(args.source)
        baseline = make_baseline(args.source, tags, include=include, mutable=mutable)
        if not baseline["tags"] and not args.allow_empty:
            raise TagLedgerError(
                "No tags matched the source and include patterns. Check the source/filter or pass --allow-empty if this is intentional."
            )
        write_baseline(args.output, baseline)
        print(
            f"Recorded {len(baseline['tags'])} tag(s) from {args.source} to {args.output}."
        )
        print("Review and commit this baseline through your normal change process.")
        return 0

    if args.command == "advance":
        ensure_outside_git_metadata(args.baseline)
    baseline = load_baseline(args.baseline)
    if args.command == "verify":
        _ensure_report_targets(args.baseline, [args.html, args.sarif, args.json_path])
    source = (
        args.source if args.command == "verify" and args.source else baseline["source"]
    )
    current = fetch_remote_tags(source)
    report = compare_baseline(baseline, current)
    report["source"] = source

    if args.command == "advance":
        if not args.yes:
            raise TagLedgerError(
                "Baseline replacement requires --yes after you review the current baseline and remote."
            )
        if report["summary"]["error_count"] and not args.accept_changes:
            print(_text_report(report), file=sys.stderr)
            raise TagLedgerError(
                "Fixed-version drift exists. Review it and pass --accept-changes only if you intend to reset history."
            )
        replacement = make_baseline(
            source,
            current,
            baseline.get("include_patterns"),
            baseline.get("mutable_patterns"),
        )
        write_baseline(args.baseline, replacement)
        print(f"Advanced baseline to {len(replacement['tags'])} current tag(s).")
        return 0

    if args.html:
        _write_report(args.html, render_html(report))
    if args.sarif:
        _write_report(args.sarif, json.dumps(render_sarif(report), indent=2) + "\n")
    if args.json_path:
        _write_report(
            args.json_path, json.dumps(report, ensure_ascii=False, indent=2) + "\n"
        )
    print(_text_report(report))
    if args.html:
        print(f"HTML report: {args.html}")
    return report_exit_code(report)


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()
    try:
        code = run(args)
    except TagLedgerError as exc:
        print(f"tagledger: {exc}", file=sys.stderr)
        code = 2
    except OSError as exc:
        print(f"tagledger: Could not write report: {exc}", file=sys.stderr)
        code = 2
    raise SystemExit(code)


if __name__ == "__main__":
    main()
