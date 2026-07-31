<!-- Thanks for contributing to herdr-bridge! -->

## What & why

<!-- What does this change do, and why? Link any related issue: Fixes #123 -->

## Type of change

- [ ] Bug fix (non-breaking)
- [ ] New additive feature (non-breaking — no change to a frozen public signature)
- [ ] Docs / tests / CI only
- [ ] Breaking change (requires a major version bump + migration note — please
      explain below)

## Checklist

- [ ] Tests added/updated and passing (`uv run pytest -q -m "not integration"`)
- [ ] `uv run ruff check .` and `uv run mypy src` are clean
- [ ] The five frozen public signatures are unchanged (or this is an approved
      breaking change — see `BOUNDARIES.md`)
- [ ] `CHANGELOG.md` updated for user-facing changes
- [ ] Commits are signed off (DCO): `git commit -s` — see `CONTRIBUTING.md`

## Notes for reviewers

<!-- Anything that helps review: trade-offs, alternatives considered, edge cases. -->
