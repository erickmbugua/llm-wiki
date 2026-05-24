"""Tests for core/ingest.py — extraction, JSON parsing, page writing, full ingest flow."""

import json
from unittest.mock import MagicMock, patch

import pytest
import requests as req

from core.ingest import (
    _append_log,
    _build_ingest_prompt,
    _check_ollama,
    _extract_text,
    _parse_llm_json,
    _write_pages,
    ingest_source,
)

# ── _extract_text ─────────────────────────────────────────────────────────────


class TestExtractText:
    def test_reads_txt_file(self, tmp_path):
        f = tmp_path / "doc.txt"
        f.write_text("Hello world")
        text, name = _extract_text(str(f))
        assert text == "Hello world"
        assert name == "doc.txt"

    def test_reads_md_file(self, tmp_path):
        f = tmp_path / "note.md"
        f.write_text("# Title\nBody text")
        text, name = _extract_text(str(f))
        assert "Body text" in text
        assert name == "note.md"

    def test_returns_empty_for_nonexistent_file(self, tmp_path):
        text, name = _extract_text(str(tmp_path / "missing.txt"))
        assert text == ""

    def test_fetches_url(self):
        fake_response = MagicMock()
        fake_response.text = (
            "<html><head><title>My Page</title></head><body><p>Content here</p></body></html>"
        )
        with patch("core.ingest.requests.get", return_value=fake_response):
            text, name = _extract_text("https://example.com/page")
        assert "Content here" in text
        assert name == "My Page"

    def test_url_strips_script_tags(self):
        fake_response = MagicMock()
        fake_response.text = "<html><body><script>evil()</script><p>Clean text</p></body></html>"
        with patch("core.ingest.requests.get", return_value=fake_response):
            text, _ = _extract_text("https://example.com")
        assert "evil()" not in text
        assert "Clean text" in text

    def test_truncates_large_content(self, tmp_path):
        f = tmp_path / "big.txt"
        f.write_text("x" * 30_000)
        text, _ = _extract_text(str(f))
        assert len(text) == 24_000


# ── _parse_llm_json ───────────────────────────────────────────────────────────


class TestParseLlmJson:
    def test_parses_valid_json(self):
        raw = json.dumps(
            {"source_page": {"file_path": "Sources/X.md", "content": "# X"}, "page_updates": []}
        )
        result = _parse_llm_json(raw)
        assert result["source_page"]["file_path"] == "Sources/X.md"
        assert result["page_updates"] == []

    def test_strips_markdown_fences(self):
        raw = '```json\n{"source_page": {"file_path": "Sources/X.md", "content": "# X"}, "page_updates": []}\n```'
        result = _parse_llm_json(raw)
        assert result["source_page"]["file_path"] == "Sources/X.md"

    def test_strips_plain_code_fences(self):
        raw = '```\n{"source_page": {"file_path": "Sources/X.md", "content": "# X"}}\n```'
        result = _parse_llm_json(raw)
        assert "source_page" in result

    def test_defaults_page_updates_to_empty_list(self):
        raw = json.dumps({"source_page": {"file_path": "Sources/X.md", "content": "# X"}})
        result = _parse_llm_json(raw)
        assert result["page_updates"] == []

    def test_raises_on_invalid_json(self):
        with pytest.raises(ValueError, match="invalid JSON"):
            _parse_llm_json("not json at all")

    def test_raises_when_source_page_missing(self):
        with pytest.raises(ValueError, match="missing 'source_page'"):
            _parse_llm_json(json.dumps({"page_updates": []}))


# ── _write_pages ──────────────────────────────────────────────────────────────


class TestWritePages:
    def test_writes_source_page(self, tmp_vault):
        wiki = tmp_vault / "wiki"
        result = {
            "source_page": {"file_path": "Sources/Article.md", "content": "# Article\nContent."},
            "page_updates": [],
        }
        written = _write_pages(wiki, result)
        assert "Sources/Article.md" in written
        assert (wiki / "Sources" / "Article.md").read_text() == "# Article\nContent."

    def test_creates_concept_page(self, tmp_vault):
        wiki = tmp_vault / "wiki"
        result = {
            "source_page": {"file_path": "Sources/S.md", "content": "# S"},
            "page_updates": [
                {"file_path": "Concepts/NewConcept.md", "action": "create", "content": "# New"},
            ],
        }
        written = _write_pages(wiki, result)
        assert "Concepts/NewConcept.md" in written
        assert (wiki / "Concepts" / "NewConcept.md").exists()

    def test_merges_existing_page_on_update_action(self, tmp_vault):
        wiki = tmp_vault / "wiki"
        existing = wiki / "Concepts" / "Existing.md"
        existing.write_text("# Original content")
        result = {
            "source_page": {"file_path": "Sources/S.md", "content": "# S"},
            "page_updates": [
                {
                    "file_path": "Concepts/Existing.md",
                    "action": "create",
                    "content": "# New section",
                },
            ],
        }
        _write_pages(wiki, result)
        merged = existing.read_text()
        assert "# Original content" in merged
        assert "# New section" in merged

    def test_overwrites_existing_page_on_update_action(self, tmp_vault):
        wiki = tmp_vault / "wiki"
        existing = wiki / "Concepts" / "Replace.md"
        existing.write_text("# Old")
        result = {
            "source_page": {"file_path": "Sources/S.md", "content": "# S"},
            "page_updates": [
                {"file_path": "Concepts/Replace.md", "action": "update", "content": "# New"},
            ],
        }
        _write_pages(wiki, result)
        assert existing.read_text() == "# New"

    def test_skips_entries_with_empty_content(self, tmp_vault):
        wiki = tmp_vault / "wiki"
        result = {
            "source_page": {"file_path": "Sources/S.md", "content": "# S"},
            "page_updates": [{"file_path": "Concepts/Empty.md", "action": "create", "content": ""}],
        }
        written = _write_pages(wiki, result)
        assert "Concepts/Empty.md" not in written

    def test_creates_nested_dirs(self, tmp_vault):
        wiki = tmp_vault / "wiki"
        result = {
            "source_page": {"file_path": "Sources/Deep/Article.md", "content": "# A"},
            "page_updates": [],
        }
        _write_pages(wiki, result)
        assert (wiki / "Sources" / "Deep" / "Article.md").exists()


# ── _build_ingest_prompt ──────────────────────────────────────────────────────


class TestBuildIngestPrompt:
    def test_contains_vault_name(self):
        prompt = _build_ingest_prompt("MyVault", "", "", "source.txt", "text")
        assert "MyVault" in prompt

    def test_contains_source_text(self):
        prompt = _build_ingest_prompt("V", "", "", "f.txt", "unique_source_content_xyz")
        assert "unique_source_content_xyz" in prompt

    def test_includes_no_related_section_when_empty(self):
        prompt = _build_ingest_prompt("V", "", "", "f.txt", "text")
        assert "(none yet)" in prompt

    def test_includes_related_pages_when_provided(self):
        prompt = _build_ingest_prompt("V", "", "### Existing Page\nContent", "f.txt", "text")
        assert "Existing Page" in prompt

    def test_prompt_instructs_yaml_colon_quoting(self):
        prompt = _build_ingest_prompt("V", "", "", "f.txt", "text")
        assert "colon" in prompt.lower()


# ── _append_log ───────────────────────────────────────────────────────────────


class TestAppendLog:
    def test_appends_entry_to_log(self, tmp_vault):
        _append_log(tmp_vault, "my-source.txt", ["Sources/X.md", "Concepts/Y.md"])
        log_text = (tmp_vault / "wiki" / "log.md").read_text()
        assert "my-source.txt" in log_text
        assert "Sources/X" in log_text

    def test_creates_log_if_missing(self, tmp_path):
        vault = tmp_path / "v"
        (vault / "wiki").mkdir(parents=True)
        _append_log(vault, "source", [])
        assert (vault / "wiki" / "log.md").exists()


# ── ingest_source (full flow, LLM mocked) ────────────────────────────────────


class TestIngestSource:
    def test_full_ingest_writes_pages(self, tmp_vault, fake_llm_response):
        llm_output = json.dumps(
            {
                "source_page": {
                    "file_path": "Sources/Article.md",
                    "content": "---\ntitle: Article\ntags: [test]\n---\nSummary here.",
                },
                "page_updates": [
                    {
                        "file_path": "Concepts/KeyIdea.md",
                        "action": "create",
                        "content": "---\ntitle: Key Idea\ntags: [test]\n---\nA key idea.",
                    }
                ],
            }
        )
        wiki = tmp_vault / "wiki"
        (wiki / "schema.md").write_text("# Schema\nIngest rules here.")

        with (
            patch("core.ingest.litellm.completion", return_value=fake_llm_response(llm_output)),
            patch("core.ingest.resolve_model", return_value="claude-sonnet-4-6"),
        ):
            result = ingest_source(tmp_vault, str(tmp_vault / "wiki" / "schema.md"), "TestVault")

        assert (wiki / "Sources" / "Article.md").exists()
        assert (wiki / "Concepts" / "KeyIdea.md").exists()
        assert "Sources/Article.md" in result["pages_written"]

    def test_dry_run_does_not_write_files(self, tmp_vault, fake_llm_response):
        llm_output = json.dumps(
            {
                "source_page": {"file_path": "Sources/DryRun.md", "content": "# Dry"},
                "page_updates": [],
            }
        )
        src = tmp_vault / "raw" / "test.txt"
        src.write_text("some content")

        with (
            patch("core.ingest.litellm.completion", return_value=fake_llm_response(llm_output)),
            patch("core.ingest.resolve_model", return_value="claude-sonnet-4-6"),
        ):
            result = ingest_source(tmp_vault, str(src), "TestVault", dry_run=True)

        assert not (tmp_vault / "wiki" / "Sources" / "DryRun.md").exists()
        assert result["pages_written"] == []

    def test_raises_when_source_unreadable(self, tmp_vault):
        with pytest.raises(ValueError, match="Could not extract text"):
            ingest_source(tmp_vault, "/nonexistent/path/to/file.xyz", "TestVault")


# ── _check_ollama ─────────────────────────────────────────────────────────────


class TestCheckOllama:
    def test_success_when_model_present(self):
        fake_resp = MagicMock()
        fake_resp.json.return_value = {
            "models": [{"name": "qwen2.5-coder:7b"}, {"name": "llama3:8b"}]
        }
        with patch("core.ingest.requests.get", return_value=fake_resp):
            _check_ollama("ollama/qwen2.5-coder:7b")  # must not raise

    def test_raises_when_server_unreachable(self):
        with (
            patch("core.ingest.requests.get", side_effect=req.exceptions.ConnectionError()),
            pytest.raises(RuntimeError, match="ollama serve"),
        ):
            _check_ollama("ollama/qwen2.5-coder:7b")

    def test_raises_when_model_not_pulled(self):
        fake_resp = MagicMock()
        fake_resp.json.return_value = {"models": [{"name": "llama3:8b"}]}
        with (
            patch("core.ingest.requests.get", return_value=fake_resp),
            pytest.raises(RuntimeError, match="ollama pull"),
        ):
            _check_ollama("ollama/qwen2.5-coder:7b")

    def test_ingest_source_skips_preflight_for_non_ollama(self, tmp_vault, fake_llm_response):
        llm_output = json.dumps(
            {
                "source_page": {"file_path": "Sources/X.md", "content": "# X"},
                "page_updates": [],
            }
        )
        src = tmp_vault / "raw" / "test.txt"
        src.write_text("some content")
        with (
            patch("core.ingest.litellm.completion", return_value=fake_llm_response(llm_output)),
            patch("core.ingest.resolve_model", return_value="claude-sonnet-4-6"),
            patch("core.ingest._check_ollama") as mock_check,
        ):
            ingest_source(tmp_vault, str(src), "TestVault")
        mock_check.assert_not_called()
