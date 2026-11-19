# TagLedger project specification

## Product definition

- **One sentence:** Keep a reviewable, committed record of Git tag object identities and report when a remote later exposes different refs.
- **Target users:** Maintainers who pin releases, dependencies, GitHub Actions, or build inputs by tag and want drift visible in code review.
- **Problem:** A tag name can be moved or deleted after a consumer has observed it; consumers may keep different local copies without a shared history.
- **Use cases:** Establish a checked baseline for a dependency; run read-only CI drift checks; review added, missing, moved, or rewritten tags before explicitly advancing history.
- **Existing approaches and prior art:** Manual `git ls-remote`, provider immutable release controls, broad monitors such as Sentinel, GitHub Actions lock tools such as SecureCI/GitHub workflow dependency lockfiles, and CertiSigma Census's local `ref-snapshot`/`tag-drift` workflow. These cover related but different scopes; tag drift detection itself is established prior art.
- **Differentiation:** TagLedger inspects Git remotes using supported transports as well as local repositories, records both the tag object OID and peeled target OID for each visible tag, and compares that committed, versioned JSON evidence later. This is a scope and data-model distinction, not a claim of a novel detection principle. No service account or server is required.

## MVP and implementation

- **Core mechanism:** Parse Git tag refs, pair annotated-tag object IDs with peeled target IDs, compare the union of names, classify drift, and require explicit confirmation before baseline replacement.
- **Stack:** Python 3.11+ standard library and the Git executable. The package is pure Python and has no C/C++ extension or Python runtime package dependency. Build backend: setuptools 77.0.3 or newer.
- **Data:** Versioned JSON baseline with source, capture time, include/mutable patterns, and sorted tag records. Reports include source, time, classification, severity, and OIDs.
- **Safety boundary:** `verify` reads source refs and baseline without changing either; it writes local report files only when report destinations are requested. Existing outputs are refused by default; `--overwrite` is explicit, and report paths still cannot alias the baseline or each other. It does not checkout, mutate refs, push, or rewrite the baseline. Supported network sources are HTTP(S), SSH/scp-style SSH, and Git transport; custom helpers and `file://` URLs are rejected. Credential-bearing URLs and credential-like query parameters are rejected; use Git credential helpers or SSH agent/key. The user owns baseline provenance.
- **MVP output:** Text plus optional JSON, SARIF 2.1.0, and self-contained searchable HTML.
- **Non-goals:** Proving first-observation authenticity; artifact signature or provenance validation; release-asset hashing; broad multi-provider APIs; continuous hosted monitoring; automatic acceptance of drift.

## Test coverage and verification status

The test suite uses local Git fixtures to cover lightweight and annotated tags, additions/deletions/moves, malformed ref output, baseline validation, no-clobber output safety, report escaping, exit codes, and the explicit `advance` gates. Regression cases verify that a pre-existing or concurrently created output remains intact unless replacement was explicitly requested. The hosted workflow targets Ubuntu, macOS and Windows with Python 3.11–3.14; see the [Actions page](https://github.com/furuilon/tagledger/actions/workflows/tests.yml) for results associated with each commit. The declared Git 2.30 minimum and a standalone current sdist inspection remain unverified. The package is not published to PyPI.

## Risks

The first baseline can faithfully preserve a bad observation. Version-like tag matching is heuristic. Network/authentication behavior depends on the caller's Git configuration; `git://` transport is unauthenticated and unencrypted. Baselines and reports retain the source identifier, which can expose local usernames, directory names or private repository names; baselines also retain tag names, OIDs and custom include/mutable patterns. Relative local paths resolve from the command's working directory. Baseline JSON is limited to 64 MiB and 100,000 tags. Simultaneous baseline advances are not locked.

## Maintenance

Small standard-library CLI, one JSON schema, and Git's stable ref listing commands keep expected solo maintenance modest. New classification rules should be opt-in or conservative and covered by fixtures.
