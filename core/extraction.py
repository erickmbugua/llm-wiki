"""Text extraction from local files and remote URLs.

Public surface:
- SOURCE_CHAR_LIMIT — default character cap for extracted text
- extract_text()    — dispatch to the right extractor based on source type
- fetch_url()       — HTTP fetch + BeautifulSoup strip
- extract_pdf()     — pypdf extraction (optional dependency)
- extract_docx()    — python-docx extraction (optional dependency)
"""

from __future__ import annotations

import logging
from pathlib import Path

import requests
from bs4 import BeautifulSoup

log = logging.getLogger(__name__)

__all__ = ["SOURCE_CHAR_LIMIT", "extract_docx", "extract_pdf", "extract_text", "fetch_url"]

SOURCE_CHAR_LIMIT = 24_000

_BINARY_SUFFIXES = frozenset(
    {
        ".xlsx",
        ".xls",
        ".pptx",
        ".ppt",
        ".doc",
        ".zip",
        ".tar",
        ".gz",
        ".mp3",
        ".mp4",
        ".png",
        ".jpg",
        ".jpeg",
        ".gif",
        ".webp",
    }
)


def extract_text(source: str, char_limit: int = SOURCE_CHAR_LIMIT) -> tuple[str, str]:
    """Dispatch text extraction to the appropriate handler based on the source string.

    Supported formats: .txt, .md, .pdf, .docx, and HTTP/HTTPS URLs.
    Known binary formats (.xlsx, .xls, .pptx, .ppt, .doc, images, archives, media)
    raise ValueError immediately rather than feeding garbled bytes to the LLM.
    Unknown text-like formats fall back to plain-text reading.

    Args:
        source: A file path or HTTP/HTTPS URL.
        char_limit: Maximum characters to return. Defaults to ``SOURCE_CHAR_LIMIT``.

    Returns:
        A tuple of (extracted_text, display_name). Text is capped at ``char_limit``
        characters. Returns ``("", source)`` when extraction is not possible.

    Raises:
        ValueError: The file extension is a known unsupported binary format.
    """
    if source.startswith("http://") or source.startswith("https://"):
        return fetch_url(source, char_limit=char_limit)

    p = Path(source)
    if not p.exists():
        return "", source

    suffix = p.suffix.lower()
    if suffix in (".txt", ".md"):
        return p.read_text(errors="replace")[:char_limit], p.name
    if suffix == ".pdf":
        return extract_pdf(p, char_limit=char_limit), p.name
    if suffix == ".docx":
        return extract_docx(p, char_limit=char_limit), p.name
    if suffix in _BINARY_SUFFIXES:
        raise ValueError(
            f"Unsupported file type '{suffix}'. "
            "Supported formats: .txt, .md, .pdf, .docx, and HTTP/HTTPS URLs."
        )
    # fallback: try reading as text (handles .rst, .yaml, .json, etc.)
    try:
        return p.read_text(errors="replace")[:char_limit], p.name
    except Exception:
        return "", p.name


def fetch_url(url: str, char_limit: int = SOURCE_CHAR_LIMIT) -> tuple[str, str]:
    """Fetch a URL, strip boilerplate HTML tags, and return plain text with the page title.

    Args:
        url: HTTP or HTTPS URL to fetch.
        char_limit: Maximum characters to return. Defaults to ``SOURCE_CHAR_LIMIT``.

    Returns:
        A tuple of (plain_text, page_title). Text is capped at ``char_limit`` characters.

    Raises:
        requests.HTTPError: The server returned a non-2xx status code.
    """
    resp = requests.get(url, timeout=20, headers={"User-Agent": "llm-wiki/1.0"})
    resp.raise_for_status()
    soup = BeautifulSoup(resp.text, "html.parser")
    for tag in soup(["script", "style", "nav", "footer", "aside"]):
        tag.decompose()
    title = soup.title.string.strip() if soup.title and soup.title.string else url
    text = soup.get_text(separator="\n", strip=True)
    return text[:char_limit], title


def extract_pdf(path: Path, char_limit: int = SOURCE_CHAR_LIMIT) -> str:
    """Extract text from a PDF file using pypdf.

    Args:
        path: Path to the PDF file.
        char_limit: Maximum characters to return. Defaults to ``SOURCE_CHAR_LIMIT``.

    Returns:
        Concatenated page text capped at ``char_limit`` characters,
        or an empty string if pypdf is not installed.
    """
    try:
        import pypdf  # pyright: ignore[reportMissingImports]

        reader = pypdf.PdfReader(str(path))  # pyright: ignore[reportUnknownMemberType]
        pages: list[str] = [  # pyright: ignore[reportUnknownMemberType,reportUnknownVariableType]
            p.extract_text() or "" for p in reader.pages
        ]
        return "\n".join(pages)[:char_limit]
    except ImportError:
        log.warning("pypdf not installed; install it for PDF support: pip install pypdf")
        return ""


def extract_docx(path: Path, char_limit: int = SOURCE_CHAR_LIMIT) -> str:
    """Extract plain text from a .docx file using python-docx.

    Args:
        path: Path to the .docx file.
        char_limit: Maximum characters to return. Defaults to ``SOURCE_CHAR_LIMIT``.

    Returns:
        Concatenated paragraph text capped at ``char_limit`` characters,
        or an empty string if python-docx is not installed.
    """
    try:
        import docx  # python-docx package name is 'docx' at import time  # pyright: ignore[reportMissingImports]

        doc = docx.Document(str(path))  # pyright: ignore[reportUnknownMemberType]
        text = "\n".join(  # pyright: ignore[reportUnknownMemberType,reportUnknownVariableType]
            p.text for p in doc.paragraphs if p.text.strip()
        )
        return text[:char_limit]
    except ImportError:
        log.warning(
            "python-docx not installed; install it for .docx support: pip install python-docx"
        )
        return ""
