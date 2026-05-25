"""Tests for core/vault.py — init_vault, vault_stats, rebuild_index."""

from unittest.mock import patch

from core.vault import WIKI_SUBDIRS, init_vault, rebuild_index, vault_stats


class TestInitVault:
    def test_creates_raw_directory(self, tmp_path):
        init_vault(tmp_path / "v", "V")
        assert (tmp_path / "v" / "raw").is_dir()

    def test_creates_wiki_subdirectories(self, tmp_path):
        init_vault(tmp_path / "v", "V")
        for subdir in WIKI_SUBDIRS:
            assert (tmp_path / "v" / "wiki" / subdir).is_dir()

    def test_creates_index_log_schema(self, tmp_path):
        init_vault(tmp_path / "v", "V")
        wiki = tmp_path / "v" / "wiki"
        assert (wiki / "index.md").exists()
        assert (wiki / "log.md").exists()
        assert (wiki / "schema.md").exists()

    def test_creates_internal_dir_and_gitignore(self, tmp_path):
        init_vault(tmp_path / "v", "V")
        internal = tmp_path / "v" / ".llm-wiki"
        assert internal.is_dir()
        assert (internal / ".gitignore").read_text() == "wiki.db\n"

    def test_writes_vault_config(self, tmp_path):
        init_vault(tmp_path / "v", "MyVault")
        from core.config import VaultConfig

        cfg = VaultConfig.load(tmp_path / "v")
        assert cfg.name == "MyVault"

    def test_schema_contains_vault_name(self, tmp_path):
        init_vault(tmp_path / "v", "CoolVault")
        schema = (tmp_path / "v" / "wiki" / "schema.md").read_text()
        assert "CoolVault" in schema

    def test_does_not_overwrite_existing_files(self, tmp_path):
        vault = tmp_path / "v"
        init_vault(vault, "V")
        (vault / "wiki" / "schema.md").write_text("custom content")
        init_vault(vault, "V")  # second call
        assert (vault / "wiki" / "schema.md").read_text() == "custom content"

    def test_creates_parent_dirs_if_needed(self, tmp_path):
        deep = tmp_path / "a" / "b" / "c"
        init_vault(deep, "Deep")
        assert (deep / "raw").is_dir()

    def test_index_has_yaml_frontmatter(self, tmp_path):
        init_vault(tmp_path / "v", "V")
        index = (tmp_path / "v" / "wiki" / "index.md").read_text()
        assert index.startswith("---")
        assert "title: Index" in index


class TestVaultStats:
    def test_empty_vault_returns_zeros(self, tmp_vault):
        stats = vault_stats(tmp_vault)
        # index.md, log.md, schema.md exist but Sources/Concepts/Entities are empty
        assert stats["categories"]["Sources"] == 0
        assert stats["categories"]["Concepts"] == 0
        assert stats["categories"]["Entities"] == 0
        assert stats["raw_queued"] == 0

    def test_counts_pages_in_categories(self, tmp_vault):
        wiki = tmp_vault / "wiki"
        (wiki / "Concepts" / "PageA.md").write_text("# A")
        (wiki / "Concepts" / "PageB.md").write_text("# B")
        (wiki / "Sources" / "SrcA.md").write_text("# Src")
        stats = vault_stats(tmp_vault)
        assert stats["categories"]["Concepts"] == 2
        assert stats["categories"]["Sources"] == 1

    def test_counts_raw_queued_files(self, tmp_vault):
        (tmp_vault / "raw" / "doc.txt").write_text("content")
        (tmp_vault / "raw" / "doc2.pdf").write_text("content")
        stats = vault_stats(tmp_vault)
        assert stats["raw_queued"] == 2

    def test_total_pages_includes_all_md_files(self, tmp_vault):
        wiki = tmp_vault / "wiki"
        (wiki / "Concepts" / "X.md").write_text("# X")
        stats = vault_stats(tmp_vault)
        # 3 root pages (index, log, schema) + 1 concept
        assert stats["total_pages"] == 4


class TestRebuildIndex:
    def _pages(self, overrides: list[dict]) -> list[dict]:
        """Build minimal page dicts with defaults for missing keys."""
        defaults = {"file_path": "", "title": "Untitled", "category": "Concepts", "summary": ""}
        return [{**defaults, **p} for p in overrides]

    def test_rebuild_index_empty_vault(self, tmp_vault):
        rebuild_index(tmp_vault)
        index = (tmp_vault / "wiki" / "index.md").read_text()
        assert "No pages yet" in index

    def test_rebuild_index_has_yaml_frontmatter(self, tmp_vault):
        rebuild_index(tmp_vault)
        index = (tmp_vault / "wiki" / "index.md").read_text()
        assert index.startswith("---")
        assert "title: Index" in index

    def test_rebuild_index_groups_by_category(self, tmp_vault):
        pages = self._pages(
            [
                {
                    "file_path": "Concepts/Foo.md",
                    "title": "Foo",
                    "category": "Concepts",
                    "summary": "Foo desc",
                },
                {
                    "file_path": "Sources/Bar.md",
                    "title": "Bar",
                    "category": "Sources",
                    "summary": "Bar desc",
                },
            ]
        )
        with patch("core.vault.list_pages", return_value=pages):
            rebuild_index(tmp_vault)
        index = (tmp_vault / "wiki" / "index.md").read_text()
        assert "## Concepts" in index
        assert "## Sources" in index
        assert "[[Foo]]" in index
        assert "[[Bar]]" in index

    def test_rebuild_index_sorts_by_title(self, tmp_vault):
        pages = self._pages(
            [
                {
                    "file_path": "Concepts/Zebra.md",
                    "title": "Zebra",
                    "category": "Concepts",
                    "summary": "",
                },
                {
                    "file_path": "Concepts/Apple.md",
                    "title": "Apple",
                    "category": "Concepts",
                    "summary": "",
                },
                {
                    "file_path": "Concepts/Mango.md",
                    "title": "Mango",
                    "category": "Concepts",
                    "summary": "",
                },
            ]
        )
        with patch("core.vault.list_pages", return_value=pages):
            rebuild_index(tmp_vault)
        index = (tmp_vault / "wiki" / "index.md").read_text()
        apple_pos = index.index("Apple")
        mango_pos = index.index("Mango")
        zebra_pos = index.index("Zebra")
        assert apple_pos < mango_pos < zebra_pos

    def test_rebuild_index_truncates_summary(self, tmp_vault):
        long_summary = "x" * 200
        pages = self._pages(
            [
                {
                    "file_path": "Concepts/Foo.md",
                    "title": "Foo",
                    "category": "Concepts",
                    "summary": long_summary,
                },
            ]
        )
        with patch("core.vault.list_pages", return_value=pages):
            rebuild_index(tmp_vault)
        index = (tmp_vault / "wiki" / "index.md").read_text()
        # The 120-char truncated summary should appear, not the full 200-char one
        assert "x" * 121 not in index
        assert "x" * 120 in index

    def test_rebuild_index_escapes_pipe_in_summary(self, tmp_vault):
        pages = self._pages(
            [
                {
                    "file_path": "Concepts/Foo.md",
                    "title": "Foo",
                    "category": "Concepts",
                    "summary": "A | B",
                },
            ]
        )
        with patch("core.vault.list_pages", return_value=pages):
            rebuild_index(tmp_vault)
        index = (tmp_vault / "wiki" / "index.md").read_text()
        assert "A — B" in index
        # A raw "|" inside the summary would split the table cell — verify it was escaped
        table_line = [line for line in index.splitlines() if "[[Foo]]" in line][0]
        assert "A | B" not in table_line

    def test_rebuild_index_skips_root_category(self, tmp_vault):
        pages = self._pages(
            [
                {"file_path": "index.md", "title": "Index", "category": "root", "summary": ""},
                {"file_path": "log.md", "title": "Log", "category": "root", "summary": ""},
                {
                    "file_path": "Concepts/Real.md",
                    "title": "Real",
                    "category": "Concepts",
                    "summary": "",
                },
            ]
        )
        with patch("core.vault.list_pages", return_value=pages):
            rebuild_index(tmp_vault)
        index = (tmp_vault / "wiki" / "index.md").read_text()
        assert "[[Index]]" not in index
        assert "[[Log]]" not in index
        assert "[[Real]]" in index
