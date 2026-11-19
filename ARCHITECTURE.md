# TagLedger architecture

## Data flow

```text
Git URL ── git ls-remote --tags ─┐
                                 ├─ parse refs ─ compare ─ text / JSON / SARIF / HTML
Local Git repo ─ for-each-ref ──┘                    │
                                                     └─ explicit advance only
```

The baseline is a JSON document with a schema version, source, capture time, include patterns, mutable patterns, and sorted tag entries. Each entry records the ref name, tag object OID, peeled target OID, and whether the ref appears annotated or lightweight.

## Drift model

For each ref name in the union of baseline and current names:

1. only current → `added`;
2. only baseline → `deleted`;
3. both, target OID changed → `target-moved`;
4. same target but object OID changed → `tag-object-changed`;
5. otherwise unchanged.

Exact semantic-version-shaped names are classified as `version-like-fixed`. Other names remain `unclassified` unless they match a user-specified mutable pattern. Classification controls the suggested severity; it never asserts author intent.

## Safety properties

- `verify` reads the remote refs and baseline without changing either. It may write requested report files. URL and SSH scp-style sources use `git ls-remote` with an explicit HTTP/HTTPS/SSH/Git transport allowlist; local directories use `git for-each-ref`. Filesystem paths are recognized before URL fragments so a local directory name containing `#` remains valid.
- Git stderr is intentionally omitted from user reports because remote URLs and credential helpers can be sensitive.
- New baseline and report outputs refuse existing targets by default and publish without clobbering a path that appears during the write. Where hard links are unavailable, an exclusive-create fallback may leave a partial new file if copying is interrupted or fails. `--overwrite` uses a temporary sibling and atomic replacement after the CLI safety checks; it deliberately replaces the requested destination path and does not permit a report to alias its baseline or another report. A pre-existing `.tmp` symlink is not followed.
- Baselines are validated as a strict version-1 schema before comparison or writing, including non-empty include patterns, tag names, object IDs and tag kind.
- Verify report destinations are checked against the baseline and each other, including resolved symlink and hard-link aliases, before any report is written. Existing report files require `--overwrite`.
- `advance` requires `--yes`; fixed-version drift requires `--accept-changes`, and replacing a non-empty baseline with zero matching tags requires `--allow-empty`.
- HTML templates escape external values. JSON/SARIF report paths are caller-selected.

## Trust boundary

The caller owns baseline provenance. An attacker who controls the first observed source can provide a plausible baseline. OIDs are content identifiers, not signatures. For stronger release verification, combine the report with signed tags, immutable releases, artifact digests and provenance verification.

## Modules and interfaces

- `src/tagledger/core.py` owns the data model and deterministic operations: `parse_ls_remote(text) -> list[TagRef]`, `fetch_remote_tags(source) -> list[TagRef]`, `make_baseline(...) -> dict`, `load_baseline(path) -> dict`, `compare_baseline(baseline, refs) -> dict`, and report renderers.
- `src/tagledger/cli.py` owns argument parsing and orchestration. `run(namespace) -> int` is testable without terminating the interpreter; `main()` maps expected failures to exit code 2.
- `src/tagledger/__init__.py` owns the package version. `tests/test_core.py` covers parser, diff, local Git integration, safety gates, and report output.

## Error handling and test boundary

Missing Git, timeout, output over 16 MiB, baseline JSON over 64 MiB, more than 100,000 tag refs, control characters in source identifiers, remote failure, malformed ref output, invalid or duplicate-key schema, credential-bearing source URL, empty filtered snapshot, and file-write errors fail closed with a short diagnostic. Deeply nested or otherwise malformed JSON is converted to a user-facing input error. Git stderr is discarded. `verify` does not call baseline-write functions, though requested reports are written to disk. The test suite uses temporary local bare repositories and mocked remote responses; it does not validate live-host authentication or prove the first baseline trustworthy.
