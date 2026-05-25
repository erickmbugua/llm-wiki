# P2 — SSRF Protection for Source URL Fetching

## Problem Statement

`_fetch_url` in `core/ingest.py:260` passes the user-supplied URL directly to
`requests.get` with no validation beyond checking that it starts with `http://` or
`https://` (which happens earlier in `_extract_text`). There is no check on:

- The resolved IP address of the host
- Whether the host is a private/loopback address
- Whether the port is unusual

This is a Server-Side Request Forgery (SSRF) vulnerability. If the server is ever bound to
a non-loopback address (the CLI explicitly supports `--host 0.0.0.0`, and the `.env` pattern
is common), an attacker on the local network could supply:

```
http://169.254.169.254/latest/meta-data/   # AWS EC2 instance metadata
http://192.168.1.1/admin                   # home router admin panel
http://localhost:5432/                     # local PostgreSQL
http://127.0.0.1:11434/api/generate       # Ollama API — can run arbitrary prompts
```

The tool would fetch these URLs, pass the content to the LLM as a "source", and potentially
exfiltrate the response in the generated wiki page.

Even in the default `127.0.0.1` binding, the Ollama API endpoint is a real risk — it runs
on the same machine and can accept model commands. A malicious source link embedded in a
document ingested via a trusted URL could chain into an Ollama API call.

---

## Implementation Plan

### Step 1 — Add URL validation function

**File:** `core/ingest.py`

Add a new private function:

```python
import ipaddress
import socket
from urllib.parse import urlparse


def _validate_url(url: str) -> None:
    """Validate that a URL is safe to fetch (scheme check + private IP block).

    Resolves the hostname to an IP address and rejects private, loopback, and
    link-local ranges. This prevents SSRF attacks where a crafted URL reaches
    internal services.

    Args:
        url: URL string to validate.

    Raises:
        ValueError: The scheme is not http or https.
        ValueError: The URL resolves to a private, loopback, or reserved IP address.
        ValueError: The hostname cannot be resolved.
    """
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        raise ValueError(f"Unsupported URL scheme '{parsed.scheme}'. Only http and https are allowed.")

    hostname = parsed.hostname
    if not hostname:
        raise ValueError(f"URL has no hostname: {url}")

    # Resolve the hostname to an IP and check it is not a private range
    try:
        addr_info = socket.getaddrinfo(hostname, None)
    except socket.gaierror as e:
        raise ValueError(f"Could not resolve hostname '{hostname}': {e}") from e

    for family, _, _, _, sockaddr in addr_info:
        ip_str = sockaddr[0]
        try:
            ip = ipaddress.ip_address(ip_str)
        except ValueError:
            continue
        if (
            ip.is_private
            or ip.is_loopback
            or ip.is_link_local
            or ip.is_reserved
            or ip.is_multicast
        ):
            raise ValueError(
                f"URL '{url}' resolves to a private/reserved IP address ({ip}). "
                "Fetching internal network addresses is not allowed."
            )
```

---

### Step 2 — Call `_validate_url` before fetching

**File:** `core/ingest.py:_extract_text`

In the URL branch, add validation before dispatching to `_fetch_url`:

```python
if source.startswith("http://") or source.startswith("https://"):
    _validate_url(source)   # raises ValueError if unsafe
    return _fetch_url(source, char_limit=char_limit)
```

`ValueError` from `_validate_url` propagates to `ingest_source`, which propagates it as
an HTTP 500 (or queue failure). A future improvement would be to map it to HTTP 400 in the
server layer (the URL is a client error, not a server error), but that is a cosmetic
improvement.

---

### Step 3 — Add an allow-list override for local development

Some legitimate use cases need to fetch from localhost (e.g. a local documentation server
at `http://localhost:3000`). Add an escape hatch via an environment variable rather than
exposing it in the config (where users might accidentally enable it in production):

**File:** `core/ingest.py:_validate_url`

```python
import os

# Emergency escape hatch — disables SSRF protection for local development.
# Never set this in a shared or networked environment.
_SSRF_PROTECTION_DISABLED = os.environ.get("LLM_WIKI_DISABLE_SSRF_CHECK", "").lower() == "true"

def _validate_url(url: str) -> None:
    if _SSRF_PROTECTION_DISABLED:
        log.warning("SSRF protection is disabled (LLM_WIKI_DISABLE_SSRF_CHECK=true). "
                    "Do not use this in a networked environment.")
        return
    ...
```

---

### Step 4 — Write tests

**File:** `tests/test_ingest.py`

- `test_validate_url_accepts_public_https`: `https://example.com` → no exception
- `test_validate_url_rejects_http_localhost`: `http://localhost/foo` → ValueError
- `test_validate_url_rejects_private_ip`: `http://192.168.1.1/` → ValueError
- `test_validate_url_rejects_loopback`: `http://127.0.0.1:5432/` → ValueError
- `test_validate_url_rejects_non_http_scheme`: `ftp://example.com` → ValueError
- `test_validate_url_rejects_metadata_endpoint`: `http://169.254.169.254/` → ValueError
- `test_extract_text_validates_url_before_fetch`: mock `_validate_url` to raise ValueError →
  `_fetch_url` is never called
- `test_ssrf_disabled_env_var_bypasses_check`: set env var → localhost URL accepted with
  warning logged

---

### Step 5 — Documentation

- `CLAUDE.md` Known Gotchas — add a note that `_fetch_url` rejects private IP ranges and
  document the `LLM_WIKI_DISABLE_SSRF_CHECK` escape hatch for local development scenarios
- CLI `ingest --help` — no change needed (error surfaces as a clear ValueError message)

---

### Estimated scope

| Area | Files | Changes |
|---|---|---|
| Ingest | `core/ingest.py` | `_validate_url` function, call in `_extract_text` |
| Tests | `tests/test_ingest.py` | 8 new test cases |
| Docs | `CLAUDE.md` | Known Gotchas entry |

No new dependencies (`ipaddress` and `socket` are stdlib). Fully backward-compatible for
public URLs. Adds a one-time DNS lookup per URL fetch (negligible latency vs. the full
HTTP fetch that follows).
