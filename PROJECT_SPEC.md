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
- **Safety boundary:** `verify` reads source refs and baseline without changing either; it writes local report files only when report destinations are requested. It does not checkout, mutate refs, push, or rewrite the baseline. Supported network sources are HTTP(S), SSH/scp-style SSH, and Git transport; custom helpers and `file://` URLs are rejected. Credential-bearing URLs and credential-like query parameters are rejected; use Git credential helpers or SSH agent/key. The user owns baseline provenance.
- **MVP output:** Text plus optional JSON, SARIF 2.1.0, and self-contained searchable HTML.
- **Non-goals:** Proving first-observation authenticity; artifact signature or provenance validation; release-asset hashing; broad multi-provider APIs; continuous hosted monitoring; automatic acceptance of drift.

## Validation and completion criteria

- Exercise lightweight and annotated tags in a local bare Git repository.
- Verify added, deleted, moved, rewritten-object, mutable-alias, and filtered-tag cases.
- Test malformed remote output, empty-source guard, baseline round trip, explicit advance gates, HTML escaping, and CLI exit codes.
- GitHub Actions run [#2](https://github.com/furuilon/tagledger/actions/runs/44200405601) passed all 12 Ubuntu/macOS/Windows × Python 3.11–3.14 jobs for commit `9895ba58cdd4ea732774af565d1e6b8bcfd512df`, including `pip install .`, CLI smoke checks, `pip check`, and the test step. The hosted images' current Git versions do not validate the declared Git 2.30 minimum.
- Current verification: On Windows/Python 3.11.0, the source suite ran 49 tests: 48 passed and one physical hard-link test was skipped because the environment denied link creation. The run used empty Git author/committer identity variables, and the fixture regression passed. In the earlier Actions [run #1](https://github.com/furuilon/tagledger/actions/runs/44197865606), the Ubuntu/Python 3.11 job failed after reporting 15 failures among 48 tests; the Ubuntu/Python 3.12 job was cancelled after its test step also reported 15 failures among 48. Both logs show temporary-repository commits failing because the committer identity was empty. The fix was validated by [run #2](https://github.com/furuilon/tagledger/actions/runs/44200405601), which passed the configured 12-job matrix for its recorded commit. A standalone sdist inspection and the declared Git 2.30 minimum remain unverified; the package is not published to PyPI.

## Risks

The first baseline can faithfully preserve a bad observation. Version-like tag matching is heuristic. Network/authentication behavior depends on the caller's Git configuration; `git://` transport is unauthenticated and unencrypted. Baselines and reports retain the source identifier, which can expose local usernames, directory names or private repository names; baselines also retain tag names, OIDs and custom include/mutable patterns. Relative local paths resolve from the command's working directory. Baseline JSON is limited to 64 MiB and 100,000 tags. Simultaneous baseline advances are not locked.

## Maintenance

Small standard-library CLI, one JSON schema, and Git's stable ref listing commands keep expected solo maintenance modest. New classification rules should be opt-in or conservative and covered by fixtures.
