## Problem

What is broken, missing or unclear? Link the issue if one exists.

## Change

What did you change, and why this approach? Call out data-model, security,
provider-contract or design-system impact.

## Verification

List the exact commands and results. For UI changes include dark and light
checks at 1280×800 and 1424×835.

- [ ] `python -m pytest -q` (backend)
- [ ] `pnpm typecheck`
- [ ] `pnpm test`
- [ ] `pnpm build`
- [ ] Route audit for affected pages, both themes
- [ ] No secrets, personal data, databases, media or model weights staged

## Limitations

What remains unverified or externally blocked? Do not describe a plan as done.
