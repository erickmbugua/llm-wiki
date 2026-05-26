"""Integration tests for the full ingest pipeline.

These tests call ingest_source directly (no HTTP layer) and verify the end-to-end
path: file extraction → mocked LLM → page writes → DB reconcile → backlinks → log → index.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from core.db import db_connection, get_page, list_pages
from core.ingest import ingest_source

from .conftest import VAULT_NAME


@pytest.mark.integration
class TestIngestPipeline:
    def test_text_file_creates_pages_in_db(self, vault_path: Path, llm_stub: MagicMock) -> None:
        """A .txt file dropped into raw/ is extracted and produces pages in the DB."""
        source_file = vault_path / "raw" / "article.txt"
        source_file.write_text("Transformers are a type of neural network architecture.")

        ingest_source(vault_path, str(source_file), VAULT_NAME)

        with db_connection(vault_path) as conn:
            pages = list_pages(conn)
        titles = [p["title"] for p in pages]
        assert "Test Source" in titles
        assert "Test Concept" in titles

    def test_markdown_file_creates_pages_in_db(self, vault_path: Path, llm_stub: MagicMock) -> None:
        """A .md file is extracted and produces the same pages as any other text source."""
        source_file = vault_path / "raw" / "note.md"
        source_file.write_text("# My Note\n\nSome content about machine learning.")

        ingest_source(vault_path, str(source_file), VAULT_NAME)

        with db_connection(vault_path) as conn:
            pages = list_pages(conn)
        titles = [p["title"] for p in pages]
        assert "Test Source" in titles
        assert "Test Concept" in titles

    def test_backlinks_reconciled_after_ingest(self, vault_path: Path, llm_stub: MagicMock) -> None:
        """After ingest, the wikilink from Sources/Test_Source → [[Test_Concept]] produces a backlink.

        The canned LLM response includes a [[Test_Concept]] link in the source page body.
        partial_reconcile should detect this and set backlinks on Test_Concept.
        """
        source_file = vault_path / "raw" / "article.txt"
        source_file.write_text("Content about concepts and their relationships.")

        ingest_source(vault_path, str(source_file), VAULT_NAME)

        with db_connection(vault_path) as conn:
            concept_page = get_page(conn, "Concepts/Test_Concept.md")

        assert concept_page is not None
        assert "Sources/Test_Source.md" in concept_page["backlinks"]

    def test_log_appended_after_ingest(self, vault_path: Path, llm_stub: MagicMock) -> None:
        """wiki/log.md receives a new entry for every completed ingest."""
        source_file = vault_path / "raw" / "article.txt"
        source_file.write_text("Some content to ingest.")

        log_path = vault_path / "wiki" / "log.md"
        content_before = log_path.read_text() if log_path.exists() else ""

        ingest_source(vault_path, str(source_file), VAULT_NAME)

        content_after = log_path.read_text()
        assert len(content_after) > len(content_before)
        assert "article.txt" in content_after

    def test_index_rebuilt_after_ingest(self, vault_path: Path, llm_stub: MagicMock) -> None:
        """wiki/index.md lists the newly written page titles after ingest."""
        source_file = vault_path / "raw" / "article.txt"
        source_file.write_text("Some content to ingest.")

        ingest_source(vault_path, str(source_file), VAULT_NAME)

        index_path = vault_path / "wiki" / "index.md"
        assert index_path.exists()
        index_content = index_path.read_text()
        # rebuild_index writes [[File_Stem]] wikilinks, not YAML title strings
        assert "Test_Source" in index_content
        assert "Test_Concept" in index_content
