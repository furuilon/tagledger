# TagLedger project specification

## Product definition

- **One sentence:** Keep a reviewable, committed record of Git tag object identities and report when a remote later exposes different refs.
- **Target users:** Maintainers who pin releases, dependencies, GitHub Actions, or build inputs by tag and want drift visible in code review.
- **Problem:** A tag name can be moved or deleted after a consumer has observed it; consumers may keep different local copies without a shared history.
- **Use cases:** Establish a checked baseline for a dependency; run read-only CI drift checks; review added, missing, moved, or rewritten tags before explicitly advancing history.
- **Existing approaches:** Manual `git ls-remote`, provider immutable release controls, and general repository monitoring. They either require manual comparison, cover only eligible releases, or center on notification rather than a committed object-level baseline.
- **Differentiation:** Persist both the tag object OID and peeled target OID in a simple versioned JSON file. No service account or server is required.

## MVP and implementation

- **Core mechanism:** Parse Git tag refs, pair annotated-tag object IDs with peeled target IDs, compare the union of names, classify drift, and require explicit confirmation before baseline replacement.
- **Stack:** Python 3.11 standard library and the Git executable. No Python runtime package dependency.
- **Data:** Versioned JSON baseline with source, capture time, include/mutable patterns, and sorted tag records. Reports include source, time, classification, severity, and OIDs.
- **Safety boundary:** `verify` reads source refs and baseline without changing either; it writes local report files only when report destinations are requested. It does not checkout, mutate refs, push, or rewrite the baseline. Supported network sources are HTTP(S), SSH/scp-style SSH, and Git transport; custom helpers and `file://` URLs are rejected. Credential-bearing URLs and credential-like query parameters are rejected; use Git credential helpers or SSH agent/key. The user owns baseline provenance.
- **MVP output:** Text plus optional JSON, SARIF 2.1.0, and self-contained searchable HTML.
- **Non-goals:** Proving first-observation authenticity; artifact signature or provenance validation; release-asset hashing; broad multi-provider APIs; continuous hosted monitoring; automatic acceptance of drift.

## Validation and completion criteria

- Exercise lightweight and annotated tags in a local bare Git repository.
- Verify added, deleted, moved, rewritten-object, mutable-alias, and filtered-tag cases.
- Test malformed remote output, empty-source guard, baseline round trip, explicit advance gates, HTML escaping, and CLI exit codes.
- Run Windows and Ubuntu CI for Python 3.11–3.13 when available.
- Keep the project `PARTIAL` until core tests, local installation/entry-point checks, and required docs pass. It is now `READY FOR USER ACCEPTANCE`; the user's review against a repository they trust is a separate acceptance step.

## Risks

The first baseline can faithfully preserve a bad observation. Version-like tag matching is heuristic. Network/authentication behavior depends on the caller's Git configuration. Simultaneous baseline advances are not locked.

## Maintenance

Small standard-library CLI, one JSON schema, and Git's stable ref listing commands keep expected solo maintenance modest. New classification rules should be opt-in or conservative and covered by fixtures.
