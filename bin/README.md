# bin/

Contains one file: `llm-wiki` — the executable CLI entry point.

---

## llm-wiki

A minimal Python wrapper script. Its only job is to put the project root on `sys.path` so that `core/` and `main.py` are importable from any working directory, then hand off to the Click CLI defined in `main.py`.

```python
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from main import cli
cli()
```

The file is `chmod +x` so it can be invoked directly:

```bash
bin/llm-wiki init ~/my-vault
bin/llm-wiki serve
```

### Adding it to PATH

To use `llm-wiki` from anywhere without specifying the full path:

```bash
# Option 1: symlink into a directory already on PATH
ln -s /path/to/llm-wiki/bin/llm-wiki /usr/local/bin/llm-wiki

# Option 2: add the bin/ directory to PATH in ~/.zshrc
export PATH="/path/to/llm-wiki/bin:$PATH"
```

### All commands (defined in main.py)

| Command | Description |
|---------|-------------|
| `init [PATH] [--name]` | Initialize vault structure at PATH |
| `list` | List all registered vaults |
| `status [--vault]` | Show page count, queue, model for a vault |
| `use <vault>` | Set the default vault |
| `set-model <model> [--vault]` | Set LiteLLM model string globally or per-vault |
| `ingest <source> [--vault] [--dry-run]` | Ingest a file or URL |
| `query <question> [--vault] [--save-as]` | LLM Q&A from wiki content |
| `lint [--vault]` | Structural + LLM lint pass |
| `reconcile [--vault]` | Re-sync FTS5 index with files on disk |
| `serve [--port] [--host]` | Start the web dashboard + vault watchers |
