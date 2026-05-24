# app/static/

Static assets served by FastAPI at the `/static/` path prefix via `StaticFiles`.

```
static/
├── css/
│   └── style.css     All styling (layout, theme, components)
└── js/
    ├── app.js        Application logic and API integration
    └── graph.js      Force-directed graph: physics + Canvas rendering
```

No build step. Files are served directly as-is. Adding a new file here makes it immediately available at `/static/<css|js>/<filename>`.
