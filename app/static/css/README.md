# app/static/css/

One file: `style.css`. All styles for the dashboard are here — no preprocessor, no utility framework.

---

## Design system

All design tokens are CSS custom properties on `:root`:

| Variable | Value | Purpose |
|----------|-------|---------|
| `--bg` | `#0d0f14` | Page background |
| `--bg2` | `#13161d` | Sidebar, panels |
| `--bg3` | `#1a1e28` | Input fields, code blocks |
| `--border` | `rgba(255,255,255,0.07)` | Subtle dividers |
| `--text` | `#d4d8e2` | Primary text |
| `--text-dim` | `#6b7280` | Secondary text, hints |
| `--accent` | `#7c6af7` | Primary accent (purple) |
| `--accent-glow` | `rgba(124,106,247,0.25)` | Glow backgrounds |
| `--green` | `#34d399` | Concepts nodes, success |
| `--amber` | `#fbbf24` | Entities nodes |
| `--red` | `#f87171` | Error states |
| `--font-sans` | Inter / Outfit / system-ui | Body text |
| `--font-mono` | JetBrains Mono / Fira Code | Code, result boxes |
| `--radius` | `10px` | Default border radius |
| `--sidebar-w` | `220px` | Fixed sidebar width |
| `--transition` | `180ms ease` | All interactive transitions |

To change the accent color globally, update `--accent` and `--accent-glow`.

---

## Layout

The root grid: `#app { display: grid; grid-template-columns: var(--sidebar-w) 1fr }`.

Explorer uses a nested grid: `#explorer-layout { display: grid; grid-template-columns: 260px 1fr }`.

---

## Key components

**`.nav-btn`** — sidebar navigation buttons. `.active` state uses `--accent-glow` background.

**`.tab`** — full-height sections toggled with the `.hidden` class. Only one is visible at a time.

**`.panel`** — centered content container for Search, Ingest, Query, Log tabs. `max-width: 760px; margin: 40px auto`.

**`.result-box`** — monospace output box for LLM responses and ingest output. Add `.error` class to turn it red.

**`.search-hit`** — card for each search result. Hover border transitions to `--accent`.

**`#graph-canvas`** — fills `#tab-graph` 100% with `position: relative` on the parent.

---

## Glassmorphism

Applied on `#graph-controls` only: `backdrop-filter: blur(8px)` with a semi-transparent background. Not applied broadly to avoid performance issues on the Canvas tab.

---

## Scrollbar

Custom thin scrollbar (5px) with a transparent track and `--border`-colored thumb, applied globally with `::-webkit-scrollbar`. Firefox uses the default.
