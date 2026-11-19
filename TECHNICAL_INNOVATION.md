# TagLedger technical mechanism

## Technical problem

Git tag names are refs, and a ref can later resolve to another object. A list of tag names alone cannot show whether an annotated tag object or the target commit changed between two observations.

## Usual approach

A maintainer can query `git ls-remote --tags` and compare text manually, or rely on a host feature that protects some release tags. Manual comparison is easy to forget and does not create a project-owned history record.

## TagLedger method

1. Read remote tags with `git ls-remote --tags`; for a local repository use `git for-each-ref`.
2. Join each annotated ref row to its `^{}` peeled row. Store `name`, tag object OID, target OID, and tag kind.
3. Apply the same include patterns to baseline and current refs.
4. Compare names over the union of both sets. Distinguish add/delete, target move, and tag-object-only change.
5. Classify likely fixed versions versus user-declared mutable aliases and produce stable JSON, SARIF, text, and escaped HTML.
6. Keep remote refs and the baseline unchanged during verification; requested reports are written as local files. Existing outputs are refused unless `--overwrite` is explicit, and a concurrent new target is not replaced. Baseline advancement is a separate command with explicit consent flags; clearing all previously recorded tags also requires `--allow-empty`.

## Hard parts and code locations

- Ref/peeled-row pairing and malformed row rejection: `src/tagledger/core.py` (`parse_ls_remote`).
- Stable drift classification and severity policy: `compare_baseline` and its classification helpers in the same module.
- Local/remote read-only source selection: `fetch_remote_tags`.
- Atomic baseline replacement and safety gates: `write_baseline` and `src/tagledger/cli.py`.
- Report escaping and export schemas: `render_html` and `render_sarif`.

The mechanism deliberately does not claim a cryptographic trust anchor. OIDs identify Git objects; they do not prove who published a ref or whether a release artifact matches it.

## Correctness evidence

`tests/test_core.py` builds a local bare repository with a lightweight tag and an annotated tag, moves a tag, round-trips a baseline, and runs the snapshot/verify CLI flow. Unit tests also cover malformed and orphan peeled rows, additions/deletions, mutable aliases, OID changes, escaping, SARIF fields, baseline immutability during verification, existing output preservation and a destination created during the write.

## Performance

Parsing and comparison are linear in the number of visible refs plus sorting for deterministic output. Network latency is external to the tool. Previously recorded synthetic timings and their limits are summarized in [PERFORMANCE_NOTES.md](PERFORMANCE_NOTES.md); they are not a production-scale guarantee.

## Limitations and next technical work

- The baseline is not signed or independently authenticated; a malicious or mistaken first observation remains possible.
- Tag names are classified by a version-name heuristic, and simultaneous `advance` processes are not locked.
- Credentials embedded in HTTP(S) URLs and credential-like query parameters are rejected; use Git credential helpers. SSH agent/key authentication remains supported.
- The current 100,000-row parser timing is recorded in `PERFORMANCE_NOTES.md`. Next validation should exercise public and private remotes through user-owned Git credential helpers, add fixtures for additional Git hosting output shapes, and measure cold startup plus full-pipeline memory. Reassess the explicit 16 MiB/100,000-tag caps only if real workloads require larger inputs. Do not add provider-specific trust claims without separate evidence.
