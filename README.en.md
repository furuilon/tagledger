# TagLedger

<p align="center"><img src="assets/tagledger-icon.svg" alt="TagLedger project icon" width="112"></p>

The project mark is an original SVG; see [assets/README.md](assets/README.md) for its source note.

![Python 3.11+](https://img.shields.io/badge/Python-3.11%2B-3776AB) ![Git 2.30+](https://img.shields.io/badge/Git-2.30%2B-F05032) ![MIT](https://img.shields.io/badge/License-MIT-green) ![No Python runtime dependencies](https://img.shields.io/badge/runtime%20dependencies-none-brightgreen)

TagLedger keeps an explicit record of the Git tags a dependency exposed at a point in time, then checks whether those names still point to the same Git objects. It is designed for maintainers who want a reviewable history file they can commit beside their dependency configuration.

It is a local CLI. Remote checks read tag refs only. TagLedger does not checkout code, rewrite a ref, push, or update a baseline during `verify`.

**This tool identifies drift; it cannot tell which revision is authentic.** Verify the first baseline through a trusted source before committing it.

简体中文主页：[README.md](README.md) · [架构](ARCHITECTURE.md) · [项目规格](PROJECT_SPEC.md) · [技术机制](TECHNICAL_INNOVATION.md)

## Stack and support scope

| Area | Current declaration |
|---|---|
| Implementation | Python 3.11+; pure Python with no C/C++ extension |
| Git | Minimum declared version is 2.30; CI does not yet pin and validate that minimum |
| Runtime | Python standard library and the system Git executable; no third-party Python runtime dependencies |
| Build | setuptools 77.0.3+ (build time only) |
| CI targets | Ubuntu, macOS and Windows latest GitHub-hosted images × Python 3.11, 3.12, 3.13 and 3.14; configured targets are not passing results until hosted CI runs |
| Package version | 0.1.0 development snapshot; not published to PyPI |
| License | MIT; see [LICENSE](LICENSE) |

Local verification used Windows build 26200 and Git 2.54.0. Source tests passed on Python 3.11.0 and 3.14.2; wheel/sdist builds, isolated Python 3.14.2 wheel installation and the CLI entry point also passed. Python 3.12–3.13, macOS/Linux, the declared Git 2.30 minimum, and the hosted GitHub Actions matrix remain unverified.

## Install

Requirements: Python 3.11+ and Git 2.30+.

```powershell
python -m pip install .
tagledger --version
```

From a source checkout without installing:

```powershell
$env:PYTHONPATH = "src"
python -m tagledger.cli --help
```

## Quick start

Record a baseline after checking that the source URL and version tags are correct:

```powershell
tagledger snapshot https://github.com/OWNER/REPOSITORY.git `
  --output .tagledger/repository.json `
  --mutable v1 --mutable v2 --mutable stable
```

If an intentionally empty repository is expected, pass `--allow-empty`; otherwise an empty tag list is treated as a likely source or filter mistake.

Commit `.tagledger/repository.json` with the dependency change. Later, compare the visible tags with that file:

```powershell
tagledger verify .tagledger/repository.json `
  --html .tagledger/latest-report.html `
  --sarif .tagledger/latest-report.sarif
```

An exact version name such as `v2.4.1` is treated as a fixed-version candidate by default. Floating aliases such as `v2` or `stable` are only treated as mutable when you explicitly pass `--mutable` during snapshot creation. Naming is only a heuristic; inspect the diff.

For a local Git repository, the tool reads local tag refs with `for-each-ref`. For a URL, it calls `git ls-remote --tags`. If Git authentication is required, Git uses the credential helpers already configured on your machine.

Remote sources are limited to HTTP, HTTPS, SSH, Git transport, and SSH scp-style references such as `git@host:owner/repository.git`. Custom remote-helper protocols and `file://` URLs are rejected; remote Git calls set an explicit protocol allowlist. Local repositories should be supplied as filesystem paths, including paths containing `#`.

The `git://` transport has no transport encryption or server authentication. Prefer HTTPS or SSH, and use `git://` only on a trusted network.

Do not put tokens or passwords in a source URL. TagLedger rejects HTTP(S) URLs with embedded usernames/passwords and heuristically rejects common credential-like query parameter names. This is not a general secret detector and cannot recognize every vendor-specific field. SSH/scp usernames remain part of the stored source identifier and are not treated as secrets by the validator; never place tokens or passwords there. Use Git Credential Manager, another configured credential helper, or an SSH agent/key instead.

## What the report says

- `target-moved`: the same name resolves to a different peeled target.
- `tag-object-changed`: an annotated tag object changed while its peeled target stayed the same.
- `deleted`: a baseline ref is no longer visible.
- `added`: a ref appeared after the baseline.
- `version-like-fixed` drift fails the default check; mutable aliases and unclassified refs are reported for review.

Reports contain tag names, source identifier, timestamps and OIDs. They never include Git stderr or credential-helper output. HTML is self-contained and escapes tag names and source values.

Baselines store the source identifier, tag names and OIDs, plus custom `--include` and `--mutable` globs. Reports may also contain the source and changed tag names/OIDs. These values can expose a Windows username, directory structure, private repository name or internal naming convention. Relative local paths resolve from the working directory used to run the command. Review these fields before committing a baseline or sharing a report. Never put credentials in a source URL.

Exit codes: `0` no error- or warning-level findings (an informational new tag may still be present); `1` fixed-version drift or a review warning; `2` input, Git, or report-write error.

## Baseline updates

`verify` never writes the baseline. It rejects report paths that alias the baseline, collide with each other, or point inside Git administrative metadata. Baselines and reports use same-directory temporary files followed by atomic replacement. After reviewing a drift report, update the baseline through the normal code-review path, or explicitly run:

```powershell
tagledger advance .tagledger/repository.json --yes --accept-changes
```

`--yes` is required for every replacement. `--accept-changes` is additionally required if fixed-version refs moved or disappeared.

## Limits

- Git tag names are not inherently immutable. The baseline records what you observed; it does not establish that this observation was trustworthy.
- To bound memory and runtime for unusually large inputs, Git output is limited to 16 MiB, baseline JSON to 64 MiB, and tag lists to 100,000 entries. Queries time out after 60 seconds and terminate the Git process tree. Exceeding a limit fails safely; very large tag repositories are not currently supported. Control characters in source identifiers are rejected.
- OIDs do not validate release assets, attestations, signing identities, source provenance, or reproducible builds.
- The tool does not inspect action workflow `uses:` references automatically. It audits one Git remote at a time.
- A network error is an error; it is never interpreted as every tag being deleted.

## Development

```powershell
$env:PYTHONPATH = "src"
python -m unittest discover -s tests -v
```

The tests use a temporary local bare Git repository plus mocked remote output. They do not contact a public host. See the [recorded performance notes](PERFORMANCE_NOTES.md) for the limits of earlier synthetic measurements.

## License

MIT. See [LICENSE](LICENSE).

See this README, the project specification and the architecture notes for the documented scope, tested environments and known limits.
