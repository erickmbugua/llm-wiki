"""Tests for core/lint.py — structural checks, report saving, full lint flow."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

from core.database import get_db, reconcile
from core.lint import _save_lint_report, _structural_checks, lint_vault

# ── Helpers ───────────────────────────────────────────────────────────────────


def _make_pages(wiki_root: Path, specs: list[tuple[str, str]]) -> None:
    """Write markdown files from (rel_path, content) pairs."""
    for rel, content in specs:
        p = wiki_root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content)


def _reconciled_pages(vault_path: Path) -> list[dict]:
    wiki = vault_path / "wiki"
    conn = get_db(vault_path)
    reconcile(conn, wiki)
    from core.database import list_pages

    pages = list_pages(conn)
    conn.close()
    return pages


# ── _structural_checks ────────────────────────────────────────────────────────


class TestStructuralChecks:
    def test_detects_orphan_page(self, tmp_vault):
        wiki = tmp_vault / "wiki"
        _make_pages(
            wiki,
            [
                ("Concepts/Orphan.md", "---\ntitle: Orphan\n---\nNo links in or out.\n"),
            ],
        )
        pages = _reconciled_pages(tmp_vault)
        result = _structural_checks(wiki, pages)
        assert "Concepts/Orphan.md" in result["orphans"]

    def test_linked_page_not_orphan(self, populated_vault):
        wiki = populated_vault / "wiki"
        pages = _reconciled_pages(populated_vault)
        result = _structural_checks(wiki, pages)
        # Transformers links to Attention → neither should be an orphan
        assert "Concepts/Transformers.md" not in result["orphans"]
        assert "Concepts/Attention.md" not in result["orphans"]

    def test_detects_broken_wikilinks(self, tmp_vault):
        wiki = tmp_vault / "wiki"
        _make_pages(
            wiki,
            [
                ("Concepts/Linker.md", "---\ntitle: Linker\n---\nSee [[DoesNotExist]].\n"),
            ],
        )
        pages = _reconciled_pages(tmp_vault)
        result = _structural_checks(wiki, pages)
        assert "Concepts/Linker.md" in result["broken_links"]
        assert "DoesNotExist" in result["broken_links"]["Concepts/Linker.md"]

    def test_no_broken_links_for_valid_wikilinks(self, populated_vault):
        wiki = populated_vault / "wiki"
        pages = _reconciled_pages(populated_vault)
        result = _structural_checks(wiki, pages)
        # Transformers → [[Attention]] exists
        assert "Concepts/Transformers.md" not in result["broken_links"]

    def test_detects_missing_summary(self, tmp_vault):
        wiki = tmp_vault / "wiki"
        _make_pages(
            wiki,
            [
                ("Concepts/NoBody.md", "---\ntitle: NoBody\n---\n## Just a heading\n"),
            ],
        )
        pages = _reconciled_pages(tmp_vault)
        result = _structural_checks(wiki, pages)
        assert "Concepts/NoBody.md" in result["missing_summaries"]

    def test_empty_vault_returns_no_issues(self, tmp_vault):
        pages = _reconciled_pages(tmp_vault)
        result = _structural_checks(tmp_vault / "wiki", pages)
        assert result["broken_links"] == {}

    def test_root_category_pages_excluded_from_orphan_check(self, tmp_vault):
        # index.md, log.md, schema.md are root-category pages with no links
        pages = _reconciled_pages(tmp_vault)
        result = _structural_checks(tmp_vault / "wiki", pages)
        orphans = result["orphans"]
        assert not any("index.md" in o or "log.md" in o or "schema.md" in o for o in orphans)


# ── _save_lint_report ─────────────────────────────────────────────────────────


class TestSaveLintReport:
    def _structural(self, orphans=None, broken=None, missing=None):
        return {
            "orphans": orphans or [],
            "broken_links": broken or {},
            "missing_summaries": missing or [],
        }

    def test_creates_report_file(self, tmp_vault):
        path = _save_lint_report(
            tmp_vault, tmp_vault / "wiki", self._structural(), "LLM report text."
        )
        assert (tmp_vault / path).exists()

    def test_report_filename_includes_timestamp(self, tmp_vault):
        path = _save_lint_report(tmp_vault, tmp_vault / "wiki", self._structural(), "Report.")
        assert path.startswith("lint-")
        assert path.endswith(".md")

    def test_report_contains_structural_counts(self, tmp_vault):
        structural = self._structural(
            orphans=["Concepts/A.md"],
            broken={"Concepts/B.md": ["Missing"]},
        )
        path = _save_lint_report(tmp_vault, tmp_vault / "wiki", structural, "LLM report.")
        content = (tmp_vault / path).read_text()
        assert "Concepts/A.md" in content
        assert "Concepts/B.md" in content

    def test_report_contains_llm_report(self, tmp_vault):
        path = _save_lint_report(
            tmp_vault, tmp_vault / "wiki", self._structural(), "Unique LLM finding xyz."
        )
        content = (tmp_vault / path).read_text()
        assert "Unique LLM finding xyz." in content

    def test_appends_entry_to_log(self, tmp_vault):
        _save_lint_report(tmp_vault, tmp_vault / "wiki", self._structural(), "Report.")
        log = (tmp_vault / "wiki" / "log.md").read_text()
        assert "Lint pass" in log


# ── lint_vault (full flow, LLM mocked) ───────────────────────────────────────


class TestLintVault:
    def test_returns_structural_dict(self, populated_vault, fake_llm_response):
        with (
            patch(
                "core.lint.litellm.completion", return_value=fake_llm_response("No issues found.")
            ),
            patch("core.lint.resolve_model", return_value="claude-sonnet-4-6"),
        ):
            result = lint_vault(populated_vault)
        assert "orphans" in result["structural"]
        assert "broken_links" in result["structural"]
        assert "missing_summaries" in result["structural"]

    def test_returns_llm_report_string(self, populated_vault, fake_llm_response):
        with (
            patch(
                "core.lint.litellm.completion",
                return_value=fake_llm_response("LLM says: all good."),
            ),
            patch("core.lint.resolve_model", return_value="claude-sonnet-4-6"),
        ):
            result = lint_vault(populated_vault)
        assert result["llm_report"] == "LLM says: all good."

    def test_saves_report_to_vault_root(self, populated_vault, fake_llm_response):
        with (
            patch("core.lint.litellm.completion", return_value=fake_llm_response("Report.")),
            patch("core.lint.resolve_model", return_value="claude-sonnet-4-6"),
        ):
            result = lint_vault(populated_vault)
        assert (populated_vault / result["saved_to"]).exists()

    def test_report_in_vault_root_not_wiki(self, populated_vault, fake_llm_response):
        with (
            patch("core.lint.litellm.completion", return_value=fake_llm_response("Report.")),
            patch("core.lint.resolve_model", return_value="claude-sonnet-4-6"),
        ):
            result = lint_vault(populated_vault)
        # Report must be at vault root, not inside wiki/
        assert not result["saved_to"].startswith("wiki/")


# ── Lint report rotation ───────────────────────────────────────────────────────


class TestRotateLintReports:
    def test_rotate_lint_reports_removes_oldest(self, tmp_path):
        from core.lint import _rotate_lint_reports

        for i in range(12):
            (tmp_path / f"lint-2026-01-{i + 1:02d}-0000.md").write_text("x")
        _rotate_lint_reports(tmp_path, keep=10)
        remaining = sorted(tmp_path.glob("lint-*.md"))
        assert len(remaining) == 10
        assert not (tmp_path / "lint-2026-01-01-0000.md").exists()
        assert not (tmp_path / "lint-2026-01-02-0000.md").exists()

    def test_rotate_lint_reports_keeps_all_when_under_limit(self, tmp_path):
        from core.lint import _rotate_lint_reports

        for i in range(5):
            (tmp_path / f"lint-2026-01-{i + 1:02d}-0000.md").write_text("x")
        _rotate_lint_reports(tmp_path, keep=10)
        assert len(list(tmp_path.glob("lint-*.md"))) == 5
