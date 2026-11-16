# TagLedger

TagLedger keeps an explicit record of the Git tags a dependency exposed at a point in time, then checks whether those names still point to the same Git objects. It is designed for maintainers who want a reviewable history file they can commit beside their dependency configuration.

It is a local CLI. Remote checks read tag refs only. TagLedger does not checkout code, rewrite a ref, push, or update a baseline during `verify`.

**This tool identifies drift; it cannot tell which revision is authentic.** Verify the first baseline through a trusted source before committing it.

简体中文主页：[README.md](README.md) · [用户验收指南](USER_ACCEPTANCE.md) · [架构](ARCHITECTURE.md) · [项目规格](PROJECT_SPEC.md) · [技术机制](TECHNICAL_INNOVATION.md)

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

Do not put tokens or passwords in a source URL. TagLedger rejects HTTP(S) URLs with embedded usernames/passwords and credential-like query parameters so they cannot be copied into a committed baseline or report. SSH/scp usernames remain part of the stored source identifier and are not treated as secrets by the validator; never place tokens or passwords there. Use Git Credential Manager, another configured credential helper, or an SSH agent/key instead.

## What the report says

- `target-moved`: the same name resolves to a different peeled target.
- `tag-object-changed`: an annotated tag object changed while its peeled target stayed the same.
- `deleted`: a baseline ref is no longer visible.
- `added`: a ref appeared after the baseline.
- `version-like-fixed` drift fails the default check; mutable aliases and unclassified refs are reported for review.

Reports contain tag names, source identifier, timestamps and OIDs. They never include Git stderr or credential-helper output. HTML is self-contained and escapes tag names and source values.

Exit codes: `0` clean; `1` drift or review finding; `2` input, Git, or report-write error.

## Baseline updates

`verify` never writes the baseline, and report paths that alias the baseline or collide with each other are rejected. After reviewing a drift report, update the baseline through the normal code-review path, or explicitly run:

```powershell
tagledger advance .tagledger/repository.json --yes --accept-changes
```

`--yes` is required for every replacement. `--accept-changes` is additionally required if fixed-version refs moved or disappeared.

## Limits

- Git tag names are not inherently immutable. The baseline records what you observed; it does not establish that this observation was trustworthy.
- OIDs do not validate release assets, attestations, signing identities, source provenance, or reproducible builds.
- The tool does not inspect action workflow `uses:` references automatically. It audits one Git remote at a time.
- A network error is an error; it is never interpreted as every tag being deleted.

## Development

```powershell
$env:PYTHONPATH = "src"
python -m unittest discover -s tests -v
```

The tests use a temporary local bare Git repository plus mocked remote output. They do not contact a public host.

## License

MIT. See [LICENSE](LICENSE).
