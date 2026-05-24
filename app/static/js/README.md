# app/static/js/

Two ES Module scripts. They share state through `window` properties but are otherwise independent.

---

## app.js

The main application controller. Loaded first; calls `boot()` immediately on module evaluation.

### State

```js
let currentVault = null;   // name of the active vault (string)
let allPages = [];          // full page list for the current vault
```

`currentVault` is also set on `window` so `graph.js` can read it.

### Boot sequence

```
boot()
  └── loadVaults()         GET /api/vaults → populate <select>
        └── refreshAll()
              ├── loadPages()    GET /api/vaults/{v}/pages
              └── loadStats()    GET /api/vaults/{v}/status → sidebar stats
```

After boot, each tab is initialized lazily:
- Graph activates on `vault-changed` event or when the Graph nav button is clicked
- Log content loads when the Log tab is activated

### Key functions

| Function | Trigger | What it does |
|---|---|---|
| `loadVaults()` | boot | Fetches vault list, sets up vault switcher change handler |
| `refreshAll()` | vault switch | Reloads pages + stats, dispatches `vault-changed` |
| `loadPages(category)` | vault switch, category filter | Fetches page list, renders `#page-list` |
| `loadPageContent(filePath)` | page click | Fetches full content, renders markdown in `#page-viewer` |
| `setupCategoryFilter()` | boot | Wires `.category-btn` clicks to `loadPages(category)` |
| `setupSearch()` | boot | Wires search input + button to `GET /search` |
| `setupIngest()` | boot | Wires ingest button to `POST /ingest` |
| `setupQuery()` | boot | Wires query button to `POST /query` |
| `setupLog()` | boot | Wires lint button to `POST /lint`; `loadLog()` on tab switch |

### Global functions on `window`

`window.openPage(path)` — used by wikilink `onclick` attributes in rendered markdown. Switches to Explorer tab and loads the target page.

`window.openExplorerPage(path)` — used by search result `onclick`. Same but triggered from the Search tab.

### `markdownToHtml(md)`

A simple regex-based markdown renderer (no dependency). Handles: headings (h1–h6), code fences, inline code, bold, italic, `[[wikilinks]]` (rendered as clickable links to `openPage()`), standard markdown links, horizontal rules, and unordered lists. Wraps remaining lines in `<p>` tags.

**Limitation**: does not handle nested lists, blockquotes, or tables. To upgrade, swap the body with a call to a proper parser (e.g. `marked`) while keeping the function signature `markdownToHtml(md) → string`.

### `api(url, opts)`

Thin wrapper over `fetch()`. Throws a `Error` with `detail` message if the response is not ok. All panel functions use this.

---

## graph.js

A self-contained force-directed graph renderer using the HTML Canvas API. No external libraries.

### Activation

Listens for two custom events dispatched by `app.js`:
- `graph-activate` — fired when the Graph nav button is clicked
- `vault-changed` — fired on vault switch (re-fetches graph data if graph tab is visible)

### Data model

Fetched from `GET /api/vaults/{name}/graph`:
```json
{
  "nodes": [{ "id": 0, "title": "...", "file_path": "...", "category": "...", "backlink_count": 3 }],
  "edges": [{ "source": 0, "target": 1 }]
}
```

Nodes are augmented with physics state: `x, y, vx, vy, radius`.
- Initial positions: evenly distributed on a circle centered on the canvas
- Radius: `5 + min(backlink_count * 2, 14)` — hub pages appear larger

### Physics simulation (`tick()`)

Runs every animation frame via `requestAnimationFrame`.

| Force | Formula | Purpose |
|---|---|---|
| Repulsion | `kr / dist²` (kr = 3500) | Nodes push each other apart |
| Spring | `(dist - k) * 0.04` (k = 120) | Edges pull connected nodes toward rest length |
| Center gravity | `(center - pos) * 0.002` | Prevents graph from drifting off screen |
| Friction | `vx *= 0.82` | Damps oscillation |

### Colors by category

```js
Sources:  '#7c6af7'  // accent purple
Concepts: '#34d399'  // green
Entities: '#fbbf24'  // amber
root:     '#6b7280'  // dim gray
```

### Interactions

| Interaction | Behavior |
|---|---|
| Hover node | Shows title + stats in `#graph-info`; node glows white |
| Drag node | Pin node to cursor; physics continues on others |
| Drag background | Pan the viewport |
| Scroll wheel | Zoom in/out (centered on cursor) |
| Reset button | Resets `transform` to `{x:0, y:0, scale:1}` |

Transform state (`transform.x`, `transform.y`, `transform.scale`) is applied via `ctx.translate` + `ctx.scale` each frame. Mouse coordinates are converted to graph-space via `toGraph(e)` before hit testing.

### Extension points

- **Click to navigate**: in `canvas.onmousedown`, after finding `dragging`, check for a quick click and call `window.openPage(node.file_path)` to navigate to the page in Explorer
- **Edge labels**: render edge weight or link type by drawing text at the midpoint of each edge in `draw()`
- **Cluster coloring**: add a community-detection pass over the adjacency list and assign cluster colors to nodes
