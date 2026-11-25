# CreatorOS design system

CreatorOS is a desktop workspace, not a marketing site. The interface should
feel calm, quiet and precise: strong structure, restrained colour, readable
data, and one obvious next action. This document is the source of truth for the
renderer in `app/desktop/src`.

The implementation lives in:

- `src/styles.css` — theme tokens, shell, feature panel, shared surfaces.
- `src/components/ui.tsx` — Card, Button, Field, Badge, Stat, Progress,
  EmptyState, ErrorNote and formatting helpers.
- `src/App.tsx` — navigation model, rail, top bar, theme and routing.

## Principles

1. **Calm before decoration.** Neutral surfaces carry the layout; colour marks
   state, selection and one primary action. No neon, glow, glass or gradient
   mesh.
2. **Structure you can read at a glance.** One page title, clear section
   headings, consistent 4 px spacing rhythm, and aligned numeric columns.
3. **Honest data.** Empty, unavailable, stale and synthetic states are labelled.
   Never invent a number, a connected account or a successful publish.
4. **Local-first confidence.** The shell states what is live (backend, Ollama,
   vault) without exposing technical noise on product pages. Diagnostics live
   on the AI infrastructure page.
5. **Light and dark are equal.** Both themes are designed, not inverted. Both
   must pass the same contrast, overflow and clipping checks.

## Colour

Tokens are defined in `@theme` and exposed as semantic CSS variables. Components
use semantic tokens or the Tailwind palette; raw hex values belong in
`styles.css`, not in page markup.

### Dark workspace (default)

| Role | Token | Value |
| --- | --- | --- |
| App background | `--app-bg` | `#232f38` graphite blue |
| Surface | `--app-surface` | `#26343d` |
| Raised surface | `--app-surface-raised` | `#2c3b45` |
| Elevated surface | `--app-surface-elevated` | `#33434d` |
| Border | `--app-border` | `#3a4a54` |
| Strong border | `--app-border-strong` | `#465863` |
| Text | `--app-text` | `#f1f4f5` |
| Muted text | `--app-muted` | `#aab5bd` |
| Faint text | `--app-faint` | `#96a3ad` |
| Accent / selected | `--app-accent` | `#76e7c8` mint |
| Accent strong | `--app-accent-strong` | `#0f7a63` |

### Light workspace

| Role | Value |
| --- | --- |
| App background | `#eef2f5` cool grey |
| Surface | `#ffffff` |
| Text | `#15242d` |
| Muted text | `#526572` |
| Border | `#d8e1e5` |
| Accent | `#0d6b58` (mint, darkened for text contrast) |
| Selected rail surface | `#edf3f1` |

The navigation rail is **deep blue in both themes** (`--rail-bg: #1e3442`,
selected pill `#edf3f1`, mint 3 px indicator). This keeps the product identity
stable while the workspace changes.

### Accents

| Meaning | Token | Dark | Light |
| --- | --- | --- | --- |
| Primary / success | mint / emerald | `#76e7c8` / `#4fd6a8` | `#0d6b58` / `#0a6b4c` |
| Attention | amber | `#e2a848` | `#8a5a0b` |
| Danger | rose | `#f47e9d` | `#a11f40` |
| Informational | lavender | `#a2a5c6` | `#5b5f86` |

Colour is never the only carrier of meaning: a badge also has a label, and a
trend line also has a legend.

### Tailwind palette remap

The renderer was written against Tailwind's `slate`, `sky`, `emerald`,
`amber` and `rose` scales. Those scales are remapped once in `@theme` so every
existing page inherits the product palette without a per-page rewrite. Do not
add a second palette or hard-code a nearby hex in a page.

## Typography

- **Display / numbers:** `var(--font-display)` ("Segoe UI Variable Display",
  "Segoe UI", system-ui).
- **Body:** `var(--font-sans)`.
- **Identifiers, hashes, metrics:** `var(--font-mono)` (Cascadia Mono,
  Consolas).

| Role | Size | Weight | Notes |
| --- | --- | --- | --- |
| Feature title | 26 px | 650 | Only on the dashboard feature panel |
| Page title (top bar) | 16 px | 650 | One per route; breadcrumb section sits beside it |
| Section / card title | 14 px | 600 | Sentence case |
| Body | 13 px | 400 | 1.55 line height |
| Secondary | 12–12.5 px | 400 | Muted colour |
| Micro label | 10.5–11 px | 600 | Avoid long uppercase runs |

Rules: sentence case for labels and buttons; tabular numerals for metrics;
letter-spacing no looser than `0.01em`; no text below 10 px.

## Spacing, radius and elevation

- Spacing unit: **4 px**. Common gaps: 8, 12, 16, 20, 24, 32.
- Content padding: `24px 30px 64px`; max content width `1480px`.
- Panel padding: 20–24 px. Card header gap: 16 px.
- Radii: control `7px`, panel `12px`, dialog `14px`, pill `999px`.
- Shadows: one soft ambient shadow for floating surfaces; cards use a 1 px
  border and, at most, `0 1px 2px` separation. No heavy drop shadows.
- The light "feature panel" is the one intentionally bright surface. It uses
  `--feature-bg` and must stay readable in dark mode.

## Shell

- Rail: **252 px** expanded, **72 px** collapsed, persisted in
  `localStorage['creatoros.rail.collapsed']`.
- Rail header: 30 px brand mark, product name and one-line promise.
- Rail groups: Studio, Content library, Agent workforce, Social accounts,
  Publishing, Analytics. Icons are 15 px Lucide outlines.
- Selected item: light pill, dark text, mint icon, 3 px left indicator.
- Top bar: **56 px**, breadcrumb plus page title on the left; live backend,
  Ollama and theme controls on the right.
- Content scrolls inside `main`; the rail and top bar stay fixed.
- At `max-width: 980px` the rail collapses to icons automatically. At
  `480px` the top bar hides status text but keeps the theme control.

## Components

- **Card** — section container with a 14 px title, optional 12.5 px subtitle,
  optional actions, and a 16 px header gap. Use `bg-slate-900`; never nest a
  card directly inside another card without a dashed or soft inset.
- **Button** — 32 px high, 7 px radius, 13 px medium. Variants: default (soft
  surface), primary (mint, white text), danger (rose), ghost (transparent).
  Icon-only buttons need `aria-label` and a title.
- **Field** — 12 px label above the control, 12 px hint below, error in rose.
  Controls are 36 px high on forms, 32 px in dense toolbars.
- **Badge** — pill with a tinted background and 1 px ring, 11 px mono. Always
  textual.
- **Stat** — label, 22 px tabular value, optional unit. Used in grids of 2–4.
- **Feature panel** — dashboard-only light panel: eyebrow, 26 px promise,
  supporting copy, primary/secondary actions, production-loop list on the
  right, and a meta strip (readiness, accounts, storage, version).
- **EmptyState** — dashed inset with a plain-language reason and a next step.
  Never use it for a feature that is actually broken.
- **Charts** — mint primary series, lavender/teal/rose/amber for additional
  series; 1 px grid lines; 11 px axis labels; legend always present. Charts
  must expose an honest empty state when no observations exist.

## Navigation and page patterns

- Dashboard: feature panel → four metrics → pipeline + studio status →
  recent AI work + health → analytics and infrastructure detail.
- Data pages: page subtitle/actions → filters → table or grid → pagination.
- Forms: grouped cards, one primary action per group, inline validation.
- Detail pages: entity title, status badges, key facts, then related records.
  Destructive actions are separated and confirmed.
- Tables: 13 px body, 11–12 px headers, right-aligned numeric columns, row
  hover, horizontal scroll only inside a labelled container.

## Accessibility and quality gates

- Body text contrast ≥ 4.5:1; large text and non-text indicators ≥ 3:1.
- Visible focus ring on every interactive element; keyboard order follows
  visual order.
- No layout may clip text or produce horizontal page overflow at 1280×800 or
  1424×835.
- Respect `prefers-reduced-motion`; transitions are 120–180 ms and never
  required to understand state.
- Every icon-only control has an accessible name. Decorative icons use
  `aria-hidden="true"`.
- The route audit (`scripts/ui_drive.py` + `temp/page_audit.py`) checks all 49
  routes in both themes for contrast, clipping, overflow, bad dimensions and
  console errors. Run it after any shell or token change.

## Adding a page

1. Add the route to `PageId` and `NAV` in `App.tsx`, with an icon.
2. Use the existing `Card`, `Stat`, `Button`, `Field` and `EmptyState`
   components; do not introduce a parallel component library.
3. Use semantic tokens or the mapped Tailwind palette only.
4. Provide loading, empty, error and stale states.
5. Verify dark and light at 1280×800 and 1424×835, then run the full route
   audit.
