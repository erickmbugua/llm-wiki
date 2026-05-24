"""Tests for core/vault.py — init_vault, vault_stats."""

from core.vault import WIKI_SUBDIRS, init_vault, vault_stats


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
