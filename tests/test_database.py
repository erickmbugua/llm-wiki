"""Tests for core/database.py — schema, CRUD, FTS5 search, reconcile, backlinks, queue."""

import json
import time
from unittest.mock import MagicMock, patch

import pytest

from core.database import (
    _extract_summary,
    _infer_category,
    _rebuild_backlinks_full,
    _rebuild_backlinks_incremental,
    compute_embedding,
    create_job,
    delete_page,
    get_db,
    get_job,
    get_page,
    get_pending_queue,
    hybrid_search,
    list_jobs,
    list_pages,
    mark_queue_item,
    partial_reconcile,
    queue_raw_file,
    reconcile,
    search,
    update_job_status,
    upsert_page,
    vector_search,
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

    def test_falls_back_gracefully_on_invalid_yaml(self, tmp_vault, db_conn):
        wiki = tmp_vault / "wiki"
        md = wiki / "Concepts" / "BadYaml.md"
        # Unquoted colon in YAML value — common model output error
        md.write_text("---\ntitle: Managed Agents: A Deep Dive\ntags: [ai]\n---\nContent.\n")
        upsert_page(db_conn, wiki, md)  # must not raise
        row = get_page(db_conn, "Concepts/BadYaml.md")
        assert row is not None
        assert row["title"] == "BadYaml"  # falls back to filename stem


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

    def test_duplicate_file_stays_single_row(self, db_conn):
        queue_raw_file(db_conn, "/tmp/dup.pdf")
        queue_raw_file(db_conn, "/tmp/dup.pdf")
        pending = [p for p in get_pending_queue(db_conn) if p["file_path"] == "/tmp/dup.pdf"]
        assert len(pending) == 1

    def test_requeue_failed_file_resets_to_pending(self, db_conn):
        queue_raw_file(db_conn, "/tmp/retry.pdf")
        mark_queue_item(db_conn, "/tmp/retry.pdf", "failed", error="timeout")
        # dropping the same file again should reset it so it can be retried
        queue_raw_file(db_conn, "/tmp/retry.pdf")
        pending = get_pending_queue(db_conn)
        match = next((p for p in pending if p["file_path"] == "/tmp/retry.pdf"), None)
        assert match is not None, "re-queued file should appear as pending"
        row = db_conn.execute(
            "SELECT status, error, processed_at FROM ingest_queue WHERE file_path=?",
            ("/tmp/retry.pdf",),
        ).fetchone()
        assert row["status"] == "pending"
        assert row["error"] is None
        assert row["processed_at"] is None

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


# ── partial_reconcile ─────────────────────────────────────────────────────────


class TestPartialReconcile:
    def test_indexes_only_given_files(self, tmp_vault):
        """Only the explicitly passed paths are re-indexed; pre-existing others are untouched."""
        wiki = tmp_vault / "wiki"
        conn = get_db(tmp_vault)

        # Pre-index a page that will NOT be in the changed_paths list
        pre = wiki / "Concepts" / "Existing.md"
        pre.write_text("---\ntitle: Existing\ntags: []\n---\nAlready indexed.\n")
        upsert_page(conn, wiki, pre)
        old_mtime = conn.execute(
            "SELECT mtime FROM pages WHERE file_path=?", ("Concepts/Existing.md",)
        ).fetchone()["mtime"]

        # Write two new files that will be in changed_paths
        new_a = wiki / "Concepts" / "Alpha.md"
        new_b = wiki / "Concepts" / "Beta.md"
        new_a.write_text("---\ntitle: Alpha\ntags: []\n---\nFirst new page.\n")
        new_b.write_text("---\ntitle: Beta\ntags: []\n---\nSecond new page.\n")

        stats = partial_reconcile(conn, wiki, [new_a, new_b])
        conn.close()

        assert stats["added"] == 2
        assert stats["updated"] == 0

        conn2 = get_db(tmp_vault)
        assert get_page(conn2, "Concepts/Alpha.md") is not None
        assert get_page(conn2, "Concepts/Beta.md") is not None
        # Pre-existing page mtime is unchanged (was not re-indexed)
        new_mtime = conn2.execute(
            "SELECT mtime FROM pages WHERE file_path=?", ("Concepts/Existing.md",)
        ).fetchone()["mtime"]
        assert new_mtime == old_mtime
        conn2.close()

    def test_rebuilds_backlinks(self, tmp_vault):
        """Backlinks column is populated for pages referencing each other."""
        wiki = tmp_vault / "wiki"
        conn = get_db(tmp_vault)

        src = wiki / "Concepts" / "Source.md"
        tgt = wiki / "Concepts" / "Target.md"
        src.write_text("---\ntitle: Source\ntags: []\n---\nSee [[Target]] for details.\n")
        tgt.write_text("---\ntitle: Target\ntags: []\n---\nTarget page.\n")

        partial_reconcile(conn, wiki, [src, tgt])

        tgt_row = conn.execute(
            "SELECT backlinks FROM pages WHERE file_path=?", ("Concepts/Target.md",)
        ).fetchone()
        assert tgt_row is not None
        backlinks = json.loads(tgt_row["backlinks"] or "[]")
        assert "Concepts/Source.md" in backlinks
        conn.close()


# ── _rebuild_backlinks collision detection ────────────────────────────────────


class TestRebuildBacklinksCollision:
    def test_collision_logs_warning_and_is_stable(self, tmp_vault, caplog):
        """Two pages with the same stem must not raise; a WARNING must be emitted."""
        import logging

        wiki = tmp_vault / "wiki"
        conn = get_db(tmp_vault)

        (wiki / "Concepts").mkdir(parents=True, exist_ok=True)
        (wiki / "Entities").mkdir(parents=True, exist_ok=True)
        (wiki / "Concepts" / "Python.md").write_text(
            "---\ntitle: Python\ntags: []\n---\nConcept page.\n"
        )
        (wiki / "Entities" / "Python.md").write_text(
            "---\ntitle: Python\ntags: []\n---\nEntity page.\n"
        )
        upsert_page(conn, wiki, wiki / "Concepts" / "Python.md")
        upsert_page(conn, wiki, wiki / "Entities" / "Python.md")

        with caplog.at_level(logging.WARNING, logger="core.database"):
            _rebuild_backlinks_full(conn)  # must not raise

        assert any("collision" in msg.lower() or "Python" in msg for msg in caplog.messages)

        # Result must be stable — calling again produces identical backlinks
        first = conn.execute("SELECT file_path, backlinks FROM pages ORDER BY file_path").fetchall()
        _rebuild_backlinks_full(conn)
        second = conn.execute(
            "SELECT file_path, backlinks FROM pages ORDER BY file_path"
        ).fetchall()
        assert [(r["file_path"], r["backlinks"]) for r in first] == [
            (r["file_path"], r["backlinks"]) for r in second
        ]
        conn.close()


# ── links table & incremental backlinks ──────────────────────────────────────


class TestLinksTable:
    def test_upsert_page_writes_links(self, tmp_vault, db_conn):
        """Upserting a page with two wikilinks writes both rows to the links table."""
        wiki = tmp_vault / "wiki"
        md = wiki / "Concepts" / "Linker.md"
        md.write_text("---\ntitle: Linker\ntags: []\n---\nSee [[Alpha]] and [[Beta]].\n")
        upsert_page(db_conn, wiki, md)
        rows = db_conn.execute(
            "SELECT target_stem FROM links WHERE source_path=? ORDER BY target_stem",
            ("Concepts/Linker.md",),
        ).fetchall()
        stems = {r["target_stem"] for r in rows}
        assert stems == {"Alpha", "Beta"}

    def test_upsert_page_replaces_links_on_update(self, tmp_vault, db_conn):
        """Updating a page removes stale link rows and keeps only current outgoing links."""
        wiki = tmp_vault / "wiki"
        md = wiki / "Concepts" / "Evolving.md"
        md.write_text("---\ntitle: Evolving\ntags: []\n---\nSee [[Alpha]] and [[Beta]].\n")
        upsert_page(db_conn, wiki, md)
        # Remove Beta link
        md.write_text("---\ntitle: Evolving\ntags: []\n---\nSee [[Alpha]] only.\n")
        upsert_page(db_conn, wiki, md)
        rows = db_conn.execute(
            "SELECT target_stem FROM links WHERE source_path=?",
            ("Concepts/Evolving.md",),
        ).fetchall()
        stems = {r["target_stem"] for r in rows}
        assert "Beta" not in stems
        assert "Alpha" in stems

    def test_delete_page_removes_links(self, tmp_vault, db_conn):
        """Deleting a page removes all of its outgoing link rows from the links table."""
        wiki = tmp_vault / "wiki"
        md = wiki / "Concepts" / "ToDel.md"
        md.write_text("---\ntitle: ToDel\ntags: []\n---\nSee [[Other]].\n")
        upsert_page(db_conn, wiki, md)
        rows_before = db_conn.execute(
            "SELECT * FROM links WHERE source_path=?", ("Concepts/ToDel.md",)
        ).fetchall()
        assert len(rows_before) == 1
        delete_page(db_conn, "Concepts/ToDel.md")
        rows_after = db_conn.execute(
            "SELECT * FROM links WHERE source_path=?", ("Concepts/ToDel.md",)
        ).fetchall()
        assert len(rows_after) == 0

    def test_rebuild_backlinks_full_correct(self, tmp_vault, db_conn):
        """Two pages linking to a third page produce two backlinks on that third page."""
        wiki = tmp_vault / "wiki"
        (wiki / "Concepts" / "A.md").write_text("---\ntitle: A\ntags: []\n---\nSee [[C]].\n")
        (wiki / "Concepts" / "B.md").write_text("---\ntitle: B\ntags: []\n---\nAlso [[C]].\n")
        (wiki / "Concepts" / "C.md").write_text("---\ntitle: C\ntags: []\n---\nTarget page.\n")
        upsert_page(db_conn, wiki, wiki / "Concepts" / "A.md")
        upsert_page(db_conn, wiki, wiki / "Concepts" / "B.md")
        upsert_page(db_conn, wiki, wiki / "Concepts" / "C.md")
        _rebuild_backlinks_full(db_conn)
        row = get_page(db_conn, "Concepts/C.md")
        assert row is not None
        backlinks = json.loads(row["backlinks"])
        assert "Concepts/A.md" in backlinks
        assert "Concepts/B.md" in backlinks

    def test_rebuild_backlinks_incremental_only_affects_neighbourhood(self, tmp_vault, db_conn):
        """Incremental rebuild updates the changed page's targets; untouched pages stay empty."""
        wiki = tmp_vault / "wiki"
        # Five isolated pages with no links
        for i in range(5):
            md = wiki / "Concepts" / f"Page{i}.md"
            md.write_text(f"---\ntitle: Page{i}\ntags: []\n---\nPage {i} content.\n")
            upsert_page(db_conn, wiki, md)

        # Linker page that points at Page0
        linker = wiki / "Concepts" / "Linker.md"
        linker.write_text("---\ntitle: Linker\ntags: []\n---\nSee [[Page0]].\n")
        upsert_page(db_conn, wiki, linker)

        _rebuild_backlinks_incremental(db_conn, ["Concepts/Linker.md"])

        # Page0 gets the backlink
        row0 = get_page(db_conn, "Concepts/Page0.md")
        assert row0 is not None
        assert "Concepts/Linker.md" in json.loads(row0["backlinks"])

        # Pages 1–4 are untouched — their backlinks column stays at the default '[]'
        for i in range(1, 5):
            row = get_page(db_conn, f"Concepts/Page{i}.md")
            assert row is not None
            assert json.loads(row["backlinks"]) == []

    def test_backlinks_wikilink_collision_warning(self, tmp_vault, caplog):
        """_rebuild_backlinks_full logs a WARNING when two pages share the same stem."""
        import logging

        wiki = tmp_vault / "wiki"
        conn = get_db(tmp_vault)
        (wiki / "Concepts" / "Dup.md").write_text("---\ntitle: Dup\ntags: []\n---\nConcept.\n")
        (wiki / "Entities" / "Dup.md").write_text("---\ntitle: Dup\ntags: []\n---\nEntity.\n")
        upsert_page(conn, wiki, wiki / "Concepts" / "Dup.md")
        upsert_page(conn, wiki, wiki / "Entities" / "Dup.md")

        with caplog.at_level(logging.WARNING, logger="core.database"):
            _rebuild_backlinks_full(conn)

        assert any("collision" in msg.lower() or "Dup" in msg for msg in caplog.messages)
        conn.close()


# ── ingest_jobs ───────────────────────────────────────────────────────────────


class TestIngestJobs:
    def test_create_job_and_get_job_round_trip(self, db_conn):
        create_job(db_conn, job_id="job-1", vault="TestVault", source="/raw/doc.pdf")
        job = get_job(db_conn, "job-1")
        assert job is not None
        assert job["id"] == "job-1"
        assert job["vault"] == "TestVault"
        assert job["source"] == "/raw/doc.pdf"
        assert job["status"] == "pending"
        assert job["pages_written"] == []

    def test_get_job_returns_none_for_missing_id(self, db_conn):
        assert get_job(db_conn, "nonexistent-id") is None

    def test_update_job_status_running_sets_started_at(self, db_conn):
        create_job(db_conn, job_id="job-2", vault="V", source="src")
        update_job_status(db_conn, "job-2", "running")
        job = get_job(db_conn, "job-2")
        assert job is not None
        assert job["status"] == "running"
        assert job["started_at"] is not None
        assert job["finished_at"] is None

    def test_update_job_status_done_sets_finished_at_and_pages(self, db_conn):
        create_job(db_conn, job_id="job-3", vault="V", source="src")
        update_job_status(db_conn, "job-3", "running")
        update_job_status(db_conn, "job-3", "done", pages_written=["Sources/X.md"])
        job = get_job(db_conn, "job-3")
        assert job is not None
        assert job["status"] == "done"
        assert job["finished_at"] is not None
        assert job["pages_written"] == ["Sources/X.md"]

    def test_update_job_status_failed_stores_error(self, db_conn):
        create_job(db_conn, job_id="job-4", vault="V", source="src")
        update_job_status(db_conn, "job-4", "failed", error="LLM timeout")
        job = get_job(db_conn, "job-4")
        assert job is not None
        assert job["status"] == "failed"
        assert job["error"] == "LLM timeout"

    def test_list_jobs_newest_first(self, db_conn):
        create_job(db_conn, job_id="old-job", vault="V", source="old")
        create_job(db_conn, job_id="new-job", vault="V", source="new")
        jobs = list_jobs(db_conn)
        ids = [j["id"] for j in jobs]
        assert ids.index("new-job") < ids.index("old-job")

    def test_list_jobs_respects_limit(self, db_conn):
        for i in range(5):
            create_job(db_conn, job_id=f"job-{i}", vault="V", source=f"src-{i}")
        assert len(list_jobs(db_conn, limit=3)) == 3


# ── Semantic search (embeddings + vector KNN + hybrid RRF) ────────────────────

_DIM = 768
_ZERO_VEC = [0.0] * _DIM
_ONE_VEC = [1.0] + [0.0] * (_DIM - 1)


class TestSemanticSearch:
    def test_compute_embedding_returns_list_of_floats(self):
        fake_response = MagicMock()
        fake_response.data = [MagicMock(embedding=[0.1] * _DIM)]
        with patch("core.database.litellm.embedding", return_value=fake_response):
            result = compute_embedding("hello world", model="ollama/nomic-embed-text")
        assert isinstance(result, list)
        assert len(result) == _DIM
        assert all(isinstance(v, float) for v in result)

    def test_compute_embedding_raises_runtime_error_on_failure(self):
        with (
            patch("core.database.litellm.embedding", side_effect=ConnectionError("model down")),
            pytest.raises(RuntimeError, match="Embedding failed"),
        ):
            compute_embedding("hello", model="bad-model")

    def test_upsert_page_stores_embedding(self, tmp_vault):
        wiki = tmp_vault / "wiki"
        page = wiki / "Concepts" / "Emb.md"
        page.write_text("---\ntitle: Emb\ntags: []\n---\nEmbedding test page.\n")

        conn = get_db(tmp_vault)
        upsert_page(conn, wiki, page, embedding=_ONE_VEC)

        row = conn.execute(
            "SELECT v.rowid FROM page_vectors v JOIN pages p ON v.rowid = p.id WHERE p.file_path=?",
            ("Concepts/Emb.md",),
        ).fetchone()
        conn.close()
        assert row is not None

    def test_vector_search_returns_matching_page(self, tmp_vault):
        wiki = tmp_vault / "wiki"
        page = wiki / "Concepts" / "Vec.md"
        page.write_text("---\ntitle: Vec\ntags: []\n---\nVector page.\n")

        conn = get_db(tmp_vault)
        upsert_page(conn, wiki, page, embedding=_ONE_VEC)

        results = vector_search(conn, _ONE_VEC, limit=5)
        conn.close()

        assert len(results) > 0
        assert results[0]["file_path"] == "Concepts/Vec.md"

    def test_vector_search_returns_empty_when_no_embeddings(self, tmp_vault):
        wiki = tmp_vault / "wiki"
        page = wiki / "Concepts" / "NoEmb.md"
        page.write_text("---\ntitle: NoEmb\ntags: []\n---\nNo embedding.\n")

        conn = get_db(tmp_vault)
        upsert_page(conn, wiki, page)  # no embedding
        results = vector_search(conn, _ONE_VEC, limit=5)
        conn.close()

        assert results == []

    def test_hybrid_search_fuses_fts_and_vector_results(self, tmp_vault):
        wiki = tmp_vault / "wiki"
        page = wiki / "Concepts" / "Hybrid.md"
        page.write_text("---\ntitle: Hybrid\ntags: []\n---\nquantum physics superposition.\n")

        conn = get_db(tmp_vault)
        upsert_page(conn, wiki, page, embedding=_ONE_VEC)

        results = hybrid_search(conn, "quantum physics", _ONE_VEC, limit=5)
        conn.close()

        assert len(results) > 0
        paths = [r["file_path"] for r in results]
        assert "Concepts/Hybrid.md" in paths

    def test_hybrid_search_falls_back_to_lexical_when_no_embedding(self, tmp_vault):
        wiki = tmp_vault / "wiki"
        page = wiki / "Concepts" / "LexOnly.md"
        page.write_text("---\ntitle: LexOnly\ntags: []\n---\nneutral buoyancy experiment.\n")

        conn = get_db(tmp_vault)
        upsert_page(conn, wiki, page)

        results = hybrid_search(conn, "neutral buoyancy", None, limit=5)
        conn.close()

        assert len(results) > 0
        assert results[0]["file_path"] == "Concepts/LexOnly.md"
