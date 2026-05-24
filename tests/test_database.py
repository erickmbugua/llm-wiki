"""Tests for core/database.py — schema, CRUD, FTS5 search, reconcile, backlinks, queue."""

import json
import time

import pytest

from core.database import (
    _extract_summary,
    _infer_category,
    delete_page,
    get_db,
    get_page,
    get_pending_queue,
    list_pages,
    mark_queue_item,
    queue_raw_file,
    reconcile,
    search,
    upsert_page,
)

# ── Schema / get_db ───────────────────────────────────────────────────────────


class TestGetDb:
    def test_creates_db_file(self, tmp_vault):
        conn = get_db(tmp_vault)
        conn.close()
        assert (tmp_vault / ".llm-wiki" / "wiki.db").exists()

    def test_creates_pages_table(self, tmp_vault):
        conn = get_db(tmp_vault)
        tables = {
            r[0]
            for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
        }
        conn.close()
        assert "pages" in tables
        assert "ingest_queue" in tables

    def test_idempotent_on_second_call(self, tmp_vault):
        conn1 = get_db(tmp_vault)
        conn1.close()
        conn2 = get_db(tmp_vault)  # must not raise
        conn2.close()


# ── _infer_category ───────────────────────────────────────────────────────────


class TestInferCategory:
    @pytest.mark.parametrize(
        "rel_path,expected",
        [
            ("Sources/Paper.md", "Sources"),
            ("Concepts/Attention.md", "Concepts"),
            ("Entities/OpenAI.md", "Entities"),
            ("index.md", "root"),
            ("log.md", "root"),
            ("Unknown/Something.md", "root"),
        ],
    )
    def test_infers_correctly(self, rel_path, expected):
        assert _infer_category(rel_path) == expected


# ── _extract_summary ──────────────────────────────────────────────────────────


class TestExtractSummary:
    def test_returns_first_non_heading_line(self):
        content = "# Heading\n\nThis is the summary.\nSecond line."
        assert _extract_summary(content) == "This is the summary."

    def test_skips_headings_and_table_rows(self):
        content = "## H2\n| col | col |\n\nActual summary here."
        assert _extract_summary(content) == "Actual summary here."

    def test_returns_empty_for_blank_content(self):
        assert _extract_summary("") == ""
        assert _extract_summary("# Only heading") == ""

    def test_truncates_to_300_chars(self):
        long_line = "x" * 400
        assert len(_extract_summary(long_line)) == 300


# ── upsert_page / get_page / delete_page ─────────────────────────────────────


class TestUpsertPage:
    def test_inserts_new_page(self, tmp_vault, db_conn):
        wiki = tmp_vault / "wiki"
        md = wiki / "Concepts" / "Test.md"
        md.write_text("---\ntitle: Test Page\ntags: [foo]\n---\nSummary line.\n")
        upsert_page(db_conn, wiki, md)
        row = get_page(db_conn, "Concepts/Test.md")
        assert row is not None
        assert row["title"] == "Test Page"
        assert row["category"] == "Concepts"
        assert row["summary"] == "Summary line."
        assert json.loads(row["tags"]) == ["foo"]

    def test_updates_existing_page(self, tmp_vault, db_conn):
        wiki = tmp_vault / "wiki"
        md = wiki / "Concepts" / "Evolving.md"
        md.write_text("---\ntitle: V1\n---\nFirst version.\n")
        upsert_page(db_conn, wiki, md)
        md.write_text("---\ntitle: V2\n---\nSecond version.\n")
        upsert_page(db_conn, wiki, md)
        row = get_page(db_conn, "Concepts/Evolving.md")
        assert row is not None
        assert row["title"] == "V2"
        assert row["summary"] == "Second version."

    def test_falls_back_to_stem_for_title(self, tmp_vault, db_conn):
        wiki = tmp_vault / "wiki"
        md = wiki / "Concepts" / "NoFrontmatter.md"
        md.write_text("Just content, no YAML.\n")
        upsert_page(db_conn, wiki, md)
        row = get_page(db_conn, "Concepts/NoFrontmatter.md")
        assert row is not None
        assert row["title"] == "NoFrontmatter"


class TestDeletePage:
    def test_removes_page_record(self, tmp_vault, db_conn):
        wiki = tmp_vault / "wiki"
        md = wiki / "Concepts" / "Temp.md"
        md.write_text("---\ntitle: Temp\n---\nContent.\n")
        upsert_page(db_conn, wiki, md)
        assert get_page(db_conn, "Concepts/Temp.md") is not None
        delete_page(db_conn, "Concepts/Temp.md")
        assert get_page(db_conn, "Concepts/Temp.md") is None

    def test_delete_nonexistent_does_not_raise(self, db_conn):
        delete_page(db_conn, "does/not/exist.md")  # must not raise


# ── list_pages ────────────────────────────────────────────────────────────────


class TestListPages:
    def test_returns_all_pages(self, populated_vault):
        conn = get_db(populated_vault)
        pages = list_pages(conn)
        conn.close()
        titles = {p["title"] for p in pages}
        assert "Transformers" in titles
        assert "Attention" in titles

    def test_filters_by_category(self, populated_vault):
        conn = get_db(populated_vault)
        sources = list_pages(conn, category="Sources")
        concepts = list_pages(conn, category="Concepts")
        conn.close()
        assert all(p["category"] == "Sources" for p in sources)
        assert all(p["category"] == "Concepts" for p in concepts)


# ── search ────────────────────────────────────────────────────────────────────


class TestSearch:
    def test_returns_relevant_results(self, populated_vault):
        conn = get_db(populated_vault)
        results = search(conn, "attention mechanism")
        conn.close()
        titles = [r["title"] for r in results]
        assert "Attention" in titles

    def test_returns_empty_for_no_match(self, populated_vault):
        conn = get_db(populated_vault)
        results = search(conn, "xyzzy_nonexistent_term")
        conn.close()
        assert results == []

    def test_respects_limit(self, populated_vault):
        conn = get_db(populated_vault)
        results = search(conn, "the", limit=1)
        conn.close()
        assert len(results) <= 1


# ── reconcile ─────────────────────────────────────────────────────────────────


class TestReconcile:
    def test_adds_new_files(self, tmp_vault, db_conn):
        wiki = tmp_vault / "wiki"
        (wiki / "Concepts" / "New.md").write_text("---\ntitle: New\n---\nContent.\n")
        stats = reconcile(db_conn, wiki)
        assert stats["added"] >= 1
        assert get_page(db_conn, "Concepts/New.md") is not None

    def test_removes_deleted_files(self, tmp_vault, db_conn):
        wiki = tmp_vault / "wiki"
        md = wiki / "Concepts" / "Temp.md"
        md.write_text("---\ntitle: Temp\n---\nContent.\n")
        reconcile(db_conn, wiki)
        md.unlink()
        reconcile(db_conn, wiki)
        assert get_page(db_conn, "Concepts/Temp.md") is None

    def test_updates_modified_files(self, tmp_vault, db_conn):
        wiki = tmp_vault / "wiki"
        md = wiki / "Concepts" / "Mutable.md"
        md.write_text("---\ntitle: V1\n---\nOriginal.\n")
        reconcile(db_conn, wiki)
        # Force mtime to change
        time.sleep(0.05)
        md.write_text("---\ntitle: V2\n---\nUpdated.\n")
        stats = reconcile(db_conn, wiki)
        assert stats["updated"] >= 1
        row = get_page(db_conn, "Concepts/Mutable.md")
        assert row is not None
        assert row["title"] == "V2"

    def test_idempotent_when_nothing_changes(self, populated_vault):
        conn = get_db(populated_vault)
        stats = reconcile(conn, populated_vault / "wiki")
        conn.close()
        assert stats["added"] == 0
        assert stats["updated"] == 0
        assert stats["removed"] == 0


# ── backlinks ─────────────────────────────────────────────────────────────────


class TestBacklinks:
    def test_builds_backlinks_from_wikilinks(self, populated_vault):
        conn = get_db(populated_vault)
        row = get_page(conn, "Concepts/Attention.md")
        conn.close()
        assert row is not None
        backlinks = json.loads(row["backlinks"])
        assert "Concepts/Transformers.md" in backlinks

    def test_no_self_backlinks(self, tmp_vault, db_conn):
        wiki = tmp_vault / "wiki"
        md = wiki / "Concepts" / "Self.md"
        md.write_text("---\ntitle: Self\n---\nLinks to [[Self]].\n")
        reconcile(db_conn, wiki)
        row = get_page(db_conn, "Concepts/Self.md")
        assert row is not None
        assert "Concepts/Self.md" not in json.loads(row["backlinks"])

    def test_alias_links_resolved(self, tmp_vault, db_conn):
        wiki = tmp_vault / "wiki"
        (wiki / "Concepts" / "Target.md").write_text("---\ntitle: Target\n---\nTarget page.\n")
        (wiki / "Concepts" / "Linker.md").write_text(
            "---\ntitle: Linker\n---\nSee [[Target|alias text]].\n"
        )
        reconcile(db_conn, wiki)
        row = get_page(db_conn, "Concepts/Target.md")
        assert row is not None
        assert "Concepts/Linker.md" in json.loads(row["backlinks"])


# ── ingest_queue ──────────────────────────────────────────────────────────────


class TestIngestQueue:
    def test_queue_raw_file_adds_pending_item(self, db_conn):
        queue_raw_file(db_conn, "/tmp/source.pdf")
        pending = get_pending_queue(db_conn)
        assert any(p["file_path"] == "/tmp/source.pdf" for p in pending)

    def test_duplicate_file_ignored(self, db_conn):
        queue_raw_file(db_conn, "/tmp/dup.pdf")
        queue_raw_file(db_conn, "/tmp/dup.pdf")
        pending = [p for p in get_pending_queue(db_conn) if p["file_path"] == "/tmp/dup.pdf"]
        assert len(pending) == 1

    def test_mark_queue_item_done(self, db_conn):
        queue_raw_file(db_conn, "/tmp/done.pdf")
        mark_queue_item(db_conn, "/tmp/done.pdf", "done")
        pending = get_pending_queue(db_conn)
        assert not any(p["file_path"] == "/tmp/done.pdf" for p in pending)

    def test_mark_queue_item_failed_stores_error(self, db_conn):
        queue_raw_file(db_conn, "/tmp/fail.pdf")
        mark_queue_item(db_conn, "/tmp/fail.pdf", "failed", error="parse error")
        row = db_conn.execute(
            "SELECT status, error FROM ingest_queue WHERE file_path=?",
            ("/tmp/fail.pdf",),
        ).fetchone()
        assert row["status"] == "failed"
        assert row["error"] == "parse error"
