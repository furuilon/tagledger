from __future__ import annotations

import json
import subprocess
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
from unittest.mock import patch
from pathlib import Path

from tagledger.core import (
    TagLedgerError,
    TagRef,
    compare_baseline,
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


class ParserTests(unittest.TestCase):
    def test_parses_annotated_and_lightweight_tags_and_peeled_target(self) -> None:
        output = (
            "a" * 40 + "\trefs/tags/v1.0.0\n"
            + "b" * 40 + "\trefs/tags/v1.0.0^{}\n"
            + "c" * 40 + "\trefs/tags/stable\n"
            + "d" * 40 + "\trefs/heads/main\n"
        )
        tags = parse_ls_remote(output)
        self.assertEqual([(tag.name, tag.kind, tag.target_oid) for tag in tags], [
            ("stable", "lightweight", "c" * 40),
            ("v1.0.0", "annotated", "b" * 40),
        ])

    def test_rejects_malformed_and_orphan_peeled_rows(self) -> None:
        with self.assertRaises(TagLedgerError):
            parse_ls_remote("bad row")
        with self.assertRaises(TagLedgerError):
            parse_ls_remote("a" * 40 + "\trefs/tags/missing^{}\n")

    def test_rejects_invalid_tag_names_and_mixed_object_formats(self) -> None:
        with self.assertRaisesRegex(TagLedgerError, "Malformed tag name"):
            parse_ls_remote("a" * 40 + "\trefs/tags/bad name\n")
        with self.assertRaisesRegex(TagLedgerError, "object ID lengths differ"):
            parse_ls_remote("a" * 40 + "\trefs/tags/v1\n" + "b" * 64 + "\trefs/tags/v2\n")

    def test_local_paths_with_hash_and_scp_style_sources_are_supported(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            repository = Path(temporary) / "repo#release"
            repository.mkdir()
            self.assertEqual(validate_source(str(repository)), str(repository))
        self.assertEqual(validate_source("github.com:owner/repo.git"), "github.com:owner/repo.git")
        self.assertEqual(validate_source("git@github.com:owner/repo.git"), "git@github.com:owner/repo.git")

    def test_rejects_duplicate_tag_and_peeled_refs(self) -> None:
        with self.assertRaisesRegex(TagLedgerError, "Duplicate tag ref"):
            parse_ls_remote("a" * 40 + "\trefs/tags/v1\n" + "b" * 40 + "\trefs/tags/v1\n")
        with self.assertRaisesRegex(TagLedgerError, "Duplicate peeled tag ref"):
            parse_ls_remote(
                "a" * 40 + "\trefs/tags/v1\n"
                + "b" * 40 + "\trefs/tags/v1^{}\n"
                + "c" * 40 + "\trefs/tags/v1^{}\n"
            )

    def test_remote_urls_use_read_only_ls_remote(self) -> None:
        from types import SimpleNamespace

        output = "a" * 40 + "\trefs/tags/v1.2.3\n"
        with patch("tagledger.core.subprocess.run", return_value=SimpleNamespace(returncode=0, stdout=output, stderr="")) as run:
            tags = fetch_remote_tags("https://example.invalid/org/repo.git")
        self.assertEqual(tags[0].name, "v1.2.3")
        self.assertEqual(run.call_args.args[0], ["git", "-c", "protocol.allow=never", "ls-remote", "--tags", "--", "https://example.invalid/org/repo.git"])
        self.assertEqual(run.call_args.kwargs["env"]["GIT_ALLOW_PROTOCOL"], "http:https:ssh:git")

    def test_rejects_credentials_in_remote_urls_before_calling_git(self) -> None:
        with patch("tagledger.core.subprocess.run") as run:
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
        with patch("tagledger.core.subprocess.run") as run:
            for source in ("ext::sh -c echo", "custom-helper://host/repo", "file:///tmp/repo.git"):
                with self.subTest(source=source), self.assertRaises(TagLedgerError):
                    fetch_remote_tags(source)
        run.assert_not_called()

    def test_rejects_credential_bearing_source_in_existing_baseline(self) -> None:
        from tagledger.core import load_baseline

        path = Path(__file__).parent / "credential-baseline.json"
        path.write_text(json.dumps({"schema_version": 1, "source": "https://token@example.invalid/org/repo.git", "tags": []}), encoding="utf-8")
        self.addCleanup(lambda: path.unlink(missing_ok=True))
        with self.assertRaises(TagLedgerError):
            load_baseline(path)

    def test_load_baseline_fails_closed_on_patterns_and_tag_schema(self) -> None:
        from tagledger.core import make_baseline, parse_ls_remote

        baseline = make_baseline("remote", parse_ls_remote("a" * 40 + "\trefs/tags/v1.0.0\n"))
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
                with self.subTest(key=key, value=value), self.assertRaises(TagLedgerError):
                    load_baseline(path)

        for key, value in (("kind", []), ("kind", "unknown"), ("name", ""), ("name", "bad name")):
            malformed = json.loads(json.dumps(baseline))
            malformed["tags"][0][key] = value
            with tempfile.TemporaryDirectory() as temporary:
                path = Path(temporary) / "baseline.json"
                path.write_text(json.dumps(malformed), encoding="utf-8")
                with self.subTest(tag_field=key, value=value), self.assertRaises(TagLedgerError):
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
        baseline = make_baseline("remote", [TagRef("v1", "a" * 40, "a" * 40, "lightweight")])
        malformed = json.loads(json.dumps(baseline))
        malformed["tags"].append({
            "name": "v2", "object_oid": "b" * 64, "target_oid": "b" * 64, "kind": "lightweight",
        })
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "baseline.json"
            path.write_text(json.dumps(malformed), encoding="utf-8")
            with self.assertRaisesRegex(TagLedgerError, "inconsistent hash lengths"):
                load_baseline(path)

    def test_load_baseline_rejects_duplicate_json_keys(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "duplicate-keys.json"
            path.write_text('{"schema_version": 1, "schema_version": 1}', encoding="utf-8")
            with self.assertRaisesRegex(TagLedgerError, "duplicate object key"):
                load_baseline(path)

    def test_make_baseline_sorts_tag_records(self) -> None:
        baseline = make_baseline("remote", [
            TagRef("z", "b" * 40, "b" * 40, "lightweight"),
            TagRef("a", "a" * 40, "a" * 40, "lightweight"),
        ])
        self.assertEqual([row["name"] for row in baseline["tags"]], ["a", "z"])


class CompareTests(unittest.TestCase):
    def test_classifies_move_delete_metadata_change_new_and_mutable_alias(self) -> None:
        old = parse_ls_remote(
            "1" * 40 + "\trefs/tags/v1.0.0\n"
            + "2" * 40 + "\trefs/tags/stable\n"
            + "3" * 40 + "\trefs/tags/v2.0.0\n"
            + "4" * 40 + "\trefs/tags/v3.0.0\n"
        )
        baseline = make_baseline("remote", old, mutable=["stable"])
        current = parse_ls_remote(
            "a" * 40 + "\trefs/tags/v1.0.0\n"
            + "8" * 40 + "\trefs/tags/stable\n"
            + "5" * 40 + "\trefs/tags/v3.0.0\n"
            + "4" * 40 + "\trefs/tags/v3.0.0^{}\n"
            + "7" * 40 + "\trefs/tags/v4.0.0\n"
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
        baseline = make_baseline("remote", parse_ls_remote("1" * 40 + "\trefs/tags/v1.0.0\n"), include=["v*"])
        report = compare_baseline(baseline, parse_ls_remote("2" * 40 + "\trefs/tags/dev\n"))
        self.assertEqual(report["changes"][0]["change"], "deleted")
        clean = compare_baseline(make_baseline("remote", []), [])
        self.assertEqual(clean["summary"]["change_count"], 0)
        self.assertEqual(report_exit_code(clean), 0)

    def test_html_escapes_untrusted_remote_and_tag_names(self) -> None:
        baseline = make_baseline("<img src=x onerror=alert(1)>", [])
        current = [type("Tag", (), {"name": "<script>alert(1)</script>", "object_oid": "a" * 40, "target_oid": "a" * 40, "kind": "lightweight"})()]
        report = compare_baseline(baseline, current, checked_at="now")
        html = render_html(report)
        self.assertNotIn("<script>alert(1)</script>", html)
        self.assertIn("&lt;script&gt;", html)
        self.assertNotIn("<img src=x", html)

    def test_sarif_serializes_drift_rule_and_object_ids(self) -> None:
        old = make_baseline("remote", parse_ls_remote("1" * 40 + "\trefs/tags/v1.0.0\n"))
        report = compare_baseline(old, parse_ls_remote("2" * 40 + "\trefs/tags/v1.0.0\n"))
        sarif = render_sarif(report)
        result = sarif["runs"][0]["results"][0]
        self.assertEqual(result["ruleId"], "TAG001")
        self.assertEqual(result["properties"]["newTargetOid"], "2" * 40)
        self.assertEqual(sarif["runs"][0]["properties"]["source"], "remote")
        self.assertEqual(sarif["runs"][0]["properties"]["checkedAt"], report["checked_at"])


class GitIntegrationTests(unittest.TestCase):
    def git(self, *args: str, cwd: Path | None = None, input_text: str | None = None) -> str:
        proc = subprocess.run(
            ["git", *args], cwd=cwd, capture_output=True, text=True,
            encoding="utf-8", check=False, input=input_text,
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

    def make_commit(self, filename: str, contents: str, parent: str | None = None) -> str:
        source = self.root / f"input-{len(list(self.root.glob('input-*')))}.txt"
        source.write_text(contents, encoding="utf-8")
        blob = self.git("hash-object", "-w", str(source), cwd=self.remote)
        tree = self.git("mktree", cwd=self.remote, input_text=f"100644 blob {blob}\t{filename}\n")
        args = ["-c", "user.name=TagLedger Test", "-c", "user.email=tagledger@example.invalid", "commit-tree", tree, "-m", "fixture"]
        if parent:
            args.extend(["-p", parent])
        return self.git(*args, cwd=self.remote)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_reads_local_bare_remote_and_keeps_lightweight_and_annotated_shape(self) -> None:
        tags = fetch_remote_tags(str(self.remote))
        self.assertEqual({tag.name: tag.kind for tag in tags}, {"v1.0.0": "lightweight", "v2.0.0": "annotated"})
        self.assertNotEqual(next(tag for tag in tags if tag.name == "v2.0.0").object_oid,
                            next(tag for tag in tags if tag.name == "v2.0.0").target_oid)

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
        baseline = make_baseline(str(self.remote), fetch_remote_tags(str(self.remote)), captured_at="2026-01-01T00:00:00Z")
        path = self.root / "history" / "baseline.json"
        write_baseline(path, baseline)
        loaded = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(loaded, baseline)
        self.assertFalse(list(path.parent.glob(f".{path.name}.*.tmp")))

    def test_baseline_write_does_not_follow_preexisting_temp_symlink(self) -> None:
        baseline = make_baseline(str(self.remote), fetch_remote_tags(str(self.remote)), captured_at="2026-01-01T00:00:00Z")
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
            self.assertEqual(legacy_temp.read_text(encoding="utf-8"), "reserved temp name")

    def test_verify_refuses_report_path_aliases_of_baseline(self) -> None:
        from argparse import Namespace
        from tagledger.cli import run

        baseline_path = self.root / "baseline.json"
        write_baseline(baseline_path, make_baseline(str(self.remote), fetch_remote_tags(str(self.remote))))
        original = baseline_path.read_bytes()
        with redirect_stdout(StringIO()), redirect_stderr(StringIO()):
            with self.assertRaisesRegex(TagLedgerError, "cannot overwrite its baseline"):
                run(Namespace(
                    command="verify", baseline=str(baseline_path), source=None,
                    html=None, sarif=None, json_path=str(baseline_path),
                ))
        self.assertEqual(baseline_path.read_bytes(), original)

    def test_verify_rejects_colliding_report_paths(self) -> None:
        from argparse import Namespace
        from tagledger.cli import run

        baseline_path = self.root / "baseline.json"
        write_baseline(baseline_path, make_baseline(str(self.remote), fetch_remote_tags(str(self.remote))))
        with self.assertRaisesRegex(TagLedgerError, "paths must be distinct"):
            run(Namespace(
                command="verify", baseline=str(baseline_path), source=None,
                html=str(self.root / "report.out"), sarif=None, json_path=str(self.root / "report.out"),
            ))

    def test_cli_snapshot_verify_and_reports_are_end_to_end(self) -> None:
        from argparse import Namespace
        from tagledger.cli import run

        baseline_path = self.root / "baseline.json"
        with redirect_stdout(StringIO()):
            self.assertEqual(run(Namespace(
                command="snapshot", source=str(self.remote), output=str(baseline_path),
                include=None, mutable=None, allow_empty=False,
            )), 0)
        original_baseline = baseline_path.read_bytes()
        self.git("update-ref", "refs/tags/v1.0.0", self.commit2, cwd=self.remote)

        html_path = self.root / "report.html"
        sarif_path = self.root / "report.sarif"
        json_path = self.root / "report.json"
        output = StringIO()
        with redirect_stdout(output):
            code = run(Namespace(
                command="verify", baseline=str(baseline_path), source=None,
                html=str(html_path), sarif=str(sarif_path), json_path=str(json_path),
            ))

        self.assertEqual(code, 1)
        self.assertIn("target-moved", output.getvalue())
        self.assertEqual(baseline_path.read_bytes(), original_baseline, "verify must not rewrite its baseline")
        sarif = json.loads(sarif_path.read_text(encoding="utf-8"))
        self.assertEqual(sarif["version"], "2.1.0")
        self.assertIn("TAG001", {item["ruleId"] for item in sarif["runs"][0]["results"]})
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
            command="snapshot", source=str(empty_remote), output=str(baseline_path),
            include=None, mutable=None, allow_empty=False,
        )
        with self.assertRaises(TagLedgerError):
            run(args)
        self.assertFalse(baseline_path.exists())

        filtered_path = self.root / "filtered-baseline.json"
        filtered_args = Namespace(
            command="snapshot", source=str(self.remote), output=str(filtered_path),
            include=["v99.*"], mutable=None, allow_empty=False,
        )
        with self.assertRaises(TagLedgerError):
            run(filtered_args)
        self.assertFalse(filtered_path.exists(), "a non-matching include pattern must not create an empty baseline")

        args.allow_empty = True
        with redirect_stdout(StringIO()):
            self.assertEqual(run(args), 0)
        self.assertEqual(json.loads(baseline_path.read_text(encoding="utf-8"))["tags"], [])

    def test_cli_requires_explicit_advance_and_fixed_drift_acceptance(self) -> None:
        from argparse import Namespace
        from tagledger.cli import run

        baseline_path = self.root / "advance-baseline.json"
        write_baseline(baseline_path, make_baseline(str(self.remote), fetch_remote_tags(str(self.remote))))
        original = baseline_path.read_bytes()
        self.git("update-ref", "refs/tags/v1.0.0", self.commit2, cwd=self.remote)
        args = Namespace(command="advance", baseline=str(baseline_path), yes=False, accept_changes=False)

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
        self.assertEqual(next(tag for tag in advanced["tags"] if tag["name"] == "v1.0.0")["target_oid"], self.commit2)


if __name__ == "__main__":
    unittest.main()
