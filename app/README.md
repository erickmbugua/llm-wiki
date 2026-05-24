# app/

The web dashboard — served by FastAPI at `http://localhost:8000`. Zero Node.js, zero npm, zero build step. Vanilla HTML5, CSS3, and ES Modules only.

---

## Structure

```
app/
├── templates/
│   └── index.html      Single HTML page (the whole dashboard)
└── static/
    ├── css/
    │   └── style.css   All styles: layout, theme, components
    └── js/
        ├── app.js      Application logic: tabs, API calls, all panel behavior
        └── graph.js    Force-directed Canvas graph: physics simulation + rendering
```

---

## How it works

The dashboard is a **single-page application** without a framework. On load:

1. `app.js` `boot()` fetches `/api/vaults` to populate the vault selector
2. Selecting a vault triggers `refreshAll()` — loads pages + stats in parallel
3. Each tab is a `<section>` that is shown/hidden by toggling the `hidden` class
4. All data is fetched from the FastAPI REST API at runtime — no state is persisted in the browser

---

## Vault selector

The `<select id="vault-select">` at the top of the sidebar is the global context switch. Changing it fires `refreshAll()`, which re-fetches pages and stats for the new vault and dispatches a `vault-changed` custom event that `graph.js` also listens to.

---

## Tabs

| Tab | Panel ID | Description |
|-----|----------|-------------|
| Explorer | `tab-explorer` | File tree + markdown viewer |
| Graph | `tab-graph` | Force-directed page relationship graph |
| Search | `tab-search` | FTS5 search with clickable results |
| Ingest | `tab-ingest` | Ingest a URL or file path |
| Query | `tab-query` | LLM Q&A grounded in wiki content |
| Log | `tab-log` | Activity log + lint trigger |

---

## Markdown rendering

`app.js` includes a minimal `markdownToHtml(md)` function — it handles headings, bold, italic, inline code, code blocks, `[[wikilinks]]`, standard links, lists, and horizontal rules. It is intentionally not a full CommonMark parser.

**Wikilinks** are rendered as clickable anchors that call `openPage(path)` which loads the page in the Explorer tab without a full navigation.

To add full CommonMark support, replace `markdownToHtml()` with a call to a CDN-hosted parser (e.g. `marked.js`) — the function signature can stay the same.

---

## Extension points

- **New tab**: add a `<section id="tab-X">` in `index.html`, a `.nav-btn[data-tab="X"]` in the sidebar, and a `setupX()` function in `app.js`
- **Richer markdown**: swap `markdownToHtml()` with a proper parser
- **Real-time updates**: add a WebSocket endpoint in `core/server.py` and listen in `app.js` to push ingest/lint progress without polling
- **Page editing**: add a `<textarea>` to the Explorer viewer and a `PUT /api/vaults/{name}/pages/content` endpoint in `core/server.py`
