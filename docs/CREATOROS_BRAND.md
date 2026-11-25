# CreatorOS brand assets

One mark, two geometries, one source of truth. Everything the installer and the running app show is
generated from `assets/brand/*.svg` by `scripts/make-icons.cjs` — no icon is drawn by hand twice.

## The mark

A **C** whose opening holds a **play triangle**: the creator (C) and the thing that ships (play). The
C is also the outer sweep of an aperture, which is the metaphor the analytics side of the product
leans on, so the same mark covers both halves of the app.

| | |
| --- | --- |
| Tile | 1024 × 1024, radius 220 (Windows 11 squircle ratio, 21.5%) |
| Ring | radius 330, stroke 96, 304° drawn, gap centred at 3 o'clock |
| Play | centred triangle, 216 wide, optically nudged 70 units left of the ring centre |
| Tile colour | `#1b2333` → `#0f1115` (the app window's own background) |
| Accent | `#7dd3fc` → `#38bdf8` → `#6366f1` (sky-400 → sky-400 → indigo-500) |
| Play | `#e0f2fe` → `#a5b4fc` |

The colours are the UI's own accent ramp: `App.tsx` and the analytics pages use `sky-*` for primary
actions and `indigo-*` for secondary emphasis. The icon does not introduce a colour the product does
not already use.

## Two files, not two brands

`creatoros-icon.svg` is the master. `creatoros-icon-small.svg` is the same mark redrawn for 16–32 px:
tile radius 150, no glow, flat accent, ring stroke 128, larger play. It exists because a 96-unit
gradient ring at 16 px is one anti-aliased pixel and the opening closes up. Chromium rasterises the
small file at the small sizes and the master at everything else.

`scripts/make-icons.cjs` renders each size **at that size** rather than downscaling one bitmap, and
writes the `.ico` container itself: each square is a PNG payload inside the ICONDIR, so every size
keeps its own anti-aliasing (Windows accepts PNG payloads for all sizes since Vista).

## Generated files

| Path | Sizes | Used by |
| --- | --- | --- |
| `assets/brand/creatoros-icon-{16,24,32,48,64,128,256,512,1024}.png` | as named | docs, store listings, README |
| `app/desktop/build/icon.ico` | 16, 24, 32, 48, 64, 128, 256 | the app exe, the installer, Explorer |
| `app/desktop/build/installerIcon.ico` | same bytes | NSIS installer/uninstaller |
| `app/desktop/build/icon.png` | 512 | electron-builder's non-Windows fallback |

Regenerate after any change to either SVG:

```
cd app\desktop
npm run icons
```

The renderer runs inside Electron (already a dependency) because it needs Chromium; it is not part of
the shipped bundle, and `assets/brand/` is the only committed output. Rendering takes ~2 minutes on
this machine: each window is created, painted and captured at its own size, and the software
rasteriser has no GPU.

## What the icon is not

* No letterform logo. "CreatorOS" set as text is not legible below 64 px, and the product name already
  appears in the window title, the Start Menu entry and the shortcut label.
* No photographic or AI-generated artwork: it must be reproducible from a diffable source file.
* No platform-substitute icon (no generic blue "play" square): the C is what makes it ours.

## In-app usage

* Window title: `CreatorOS` (`electron/main.cjs`), the taskbar uses `build/icon.ico` through the exe.
* The sidebar shows the name and version, not the mark, at 1440 px; the mark appears in the window
  icon and in the installer's header bitmap only.
* Taskbar and Start Menu identity come from `appId` + shortcut, so the icon must be present at build
  time — a missing `build/icon.ico` makes electron-builder silently use the default Electron icon,
  which is why `scripts/build-release.ps1` regenerates icons and asserts they exist before packaging.
