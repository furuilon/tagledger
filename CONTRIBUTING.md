# Contributing

Contributions should preserve read-only verification and explicit baseline changes.

- Add a fixture for every new Git ref/object shape.
- Do not print remote stderr or credential-helper output.
- Add tests for malformed input, clean state, drift and failed transport.
- Explain whether a new classification is a documented Git fact or a naming heuristic.
- Keep release authenticity, signatures and provenance outside the claim unless they are independently implemented and tested.

Run `python -m unittest discover -s tests -v` with `PYTHONPATH=src` before proposing changes.
