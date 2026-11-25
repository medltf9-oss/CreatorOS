# GUI transformation — 0.8.0

This record covers the visual transformation that shipped with the public 0.8.0
release. It describes what changed, how it was verified and what was not
verified. The design rules live in [`DESIGN_SYSTEM.md`](DESIGN_SYSTEM.md).

## Brief and interpretation

The brief asked for an elegant, calm, premium desktop workspace: a dark
graphite-blue canvas, a deep-blue sidebar, mint / rose / lavender accents, one
deliberately light feature panel, moderate radii, and no neon or glass. The
reference image was not attached to the task, so the implementation interprets
the written palette and geometry rather than copying an unseen composition.

The existing product contracts were preserved: 49 routes, the local API, the
credential vault, publishing approvals, analytics availability states and the
data-root policy. This was a presentation and information-architecture change,
not a new feature claim.

## What changed

### One token system

- The renderer's existing Tailwind `slate` / `sky` / `emerald` / `amber` /
  `rose` scales are remapped once in `@theme`, so all 49 routes inherit the
  graphite, mint, rose and lavender palette without page-by-page colour edits.
- Semantic `--app-*` tokens cover surfaces, borders, text, accents and charts.
- Radii are 7 px for controls, 12 px for panels and 14 px for dialogs. Shadows
  are one soft ambient layer, not glass or glow.
- Dark and light are separately designed. Light is a cool-neutral workspace
  (`#eef2f5`), not an inversion. The rail stays deep blue in both themes.

### Shell

- 252 px expanded rail / 72 px collapsed rail, persisted in
  `localStorage['creatoros.rail.collapsed']`, with Lucide outline icons, grouped
  sections, a light selected pill, a mint indicator, hover and focus states.
- 56 px top bar with breadcrumb, one page title, live Ollama/backend status and a
  labelled theme control. Duplicate page `<h1>` elements were removed from
  publishing and social pages so the top bar is the single title.
- Content area at `24px 30px 64px`, max width 1480 px, with no page-level
  horizontal overflow.
- The theme is applied through `document.documentElement.dataset.theme` and
  persisted in `localStorage['creatoros.theme']`.

### Dashboard

The old technical landing page moved to **AI infrastructure**. The new
**Overview** is a product dashboard:

- A light feature panel with the product promise, two real actions (open the
  media library, compose a draft), a four-step production loop, and an honest
  meta strip (readiness, connected accounts, stored media, version).
- Four live metrics: media assets, connected accounts, local AI models, and
  items needing attention.
- Content pipeline, recent AI work, studio status, analytics empty state and
  runtime health, all read from existing APIs.
- Loading, stale, empty, error and unavailable states are preserved. Nothing is
  simulated or filled with placeholder numbers.

### Page consistency

- `Card`, `Button`, `Field`, `Badge`, `Stat` and `EmptyState` now share one
  radius, spacing and typography scale.
- Analytics uses the shared `PageHeader` without a duplicate heading; its
  "last collection" value uses the shared `Stat` rather than a custom uppercase
  box.
- Publishing and social overview headers align their explanation and actions on
  one row at desktop widths and wrap cleanly when space is tight.
- Intentional ellipsis in long prompts, IDs and logs keeps the full value in a
  `title` tooltip; table and card layouts do not hide content.

### Screenshots

- `docs/assets/screens/overview-dark.png` and `overview-light.png`
- `library-dark.png`, `publishing-dark.png`, `model-lab-dark.png`
- `analytics-dark.png`, `analytics-light.png`

They were captured from the running development build at 1264×735 CSS pixels
(the default window content size), not composed or retouched.

## Verification

| Check | Result |
| --- | --- |
| Route audit, dark | 49/49 pages; 0 contrast, 0 clipping, 0 horizontal overflow, 0 bad text, 0 console errors |
| Route audit, light | 49/49 pages; 0 contrast, 0 clipping, 0 horizontal overflow, 0 bad text, 0 console errors |
| 1280×800 viewport | No document or content overflow; dashboard reflows to four metric columns |
| 1024×768 viewport | No overflow; feature panel and metrics reflow to two columns |
| TypeScript | `pnpm typecheck` passes |
| Renderer tests | 200 passed, 0 failed |
| Production build | `pnpm build` succeeds |

The audit is `scripts/ui_drive.py` plus `temp/page_audit.py`, driven over the
Chrome DevTools Protocol against all 49 routes in both themes. It checks text
contrast against the computed background, element clipping, page overflow,
`undefined`/`NaN`/`[object Object]` text, broken images, and renderer console
errors. Deliberate ellipsis with a `title` is reported separately rather than
counted as hidden content.

Production bundle after the change:

| Asset | Raw | Gzip |
| --- | --- | --- |
| Shell entry | 305.79 kB | 69.41 kB |
| Vendor | 218.74 kB | 68.27 kB |
| Analytics (lazy) | 158.81 kB | 34.51 kB |
| CSS | 56.66 kB | 11.18 kB |

## What was not done

- The reference image was unavailable, so pixel-level fidelity to it cannot be
  claimed.
- No motion system, custom font or illustrated artwork was added. The product
  uses the Windows system UI fonts and Lucide icons already in the dependency
  graph.
- The screenshots show the maintainer's local library and installed models; they
  are real local state, not demo fixtures.
- This work did not add provider connections, publish any content, or verify a
  clean Windows installation. Those remain separate evidence tracks.
