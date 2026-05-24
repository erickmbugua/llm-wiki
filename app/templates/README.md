# app/templates/

Contains `index.html` — the single HTML page that is the entire dashboard UI.

---

## index.html

Served by FastAPI at `GET /` via `FileResponse`. It is **not** templated by Jinja2 or any server-side engine — all dynamic content is fetched from the REST API by JavaScript at runtime.

### Structure

```
<body>
  #app
  ├── <aside id="sidebar">
  │   ├── #logo
  │   ├── #vault-selector   ← <select> populated by app.js boot()
  │   ├── #nav              ← .nav-btn[data-tab] buttons drive tab switching
  │   └── #sidebar-stats    ← injected by loadStats()
  └── <main id="main">
      ├── #tab-explorer     ← file tree + markdown viewer
      ├── #tab-graph        ← <canvas id="graph-canvas">
      ├── #tab-search
      ├── #tab-ingest
      ├── #tab-query
      └── #tab-log
```

### Script loading

Both scripts are loaded as ES Modules (`type="module"`), so they run deferred after HTML parsing and can use `import`/`export`. They share state through `window` properties (e.g. `window.currentVault`, `window.openPage`).

`app.js` is loaded first and calls `boot()` immediately. `graph.js` is self-contained — it wires up by listening for the `graph-activate` and `vault-changed` custom events dispatched by `app.js`.

### Adding a new tab

1. Add a `<button class="nav-btn" data-tab="my-tab">My Tab</button>` inside `#nav`
2. Add a `<section id="tab-my-tab" class="tab hidden">` inside `#main`
3. Implement `setupMyTab()` in `app.js` and call it from `boot()`
