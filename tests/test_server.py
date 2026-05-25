"""Tests for core/server.py — FastAPI REST endpoints via TestClient."""

from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from core.config import GlobalConfig
from core.server import app

# ── Client fixture ────────────────────────────────────────────────────────────


@pytest.fixture
def client(populated_vault, monkeypatch):
    """
    TestClient with GlobalConfig patched to point at populated_vault.
    All server routes call GlobalConfig.load() internally.
    """
    cfg = GlobalConfig()
    cfg.vaults = {"TestVault": str(populated_vault)}
    cfg.default_vault = "TestVault"
    monkeypatch.setattr("core.server.GlobalConfig.load", lambda: cfg)
    return TestClient(app)


@pytest.fixture
def vault_name():
    return "TestVault"


# ── /api/vaults ───────────────────────────────────────────────────────────────


class TestApiVaults:
    def test_returns_vault_list(self, client, vault_name):
        r = client.get("/api/vaults")
        assert r.status_code == 200
        data = r.json()
        assert vault_name in data["vaults"]
        assert data["default"] == vault_name


# ── /api/vaults/{name}/status ─────────────────────────────────────────────────


class TestApiStatus:
    def test_returns_stats(self, client, vault_name):
        r = client.get(f"/api/vaults/{vault_name}/status")
        assert r.status_code == 200
        data = r.json()
        assert data["name"] == vault_name
        assert "total_pages" in data
        assert "raw_queued" in data
        assert "model" in data

    def test_404_for_unknown_vault(self, client):
        r = client.get("/api/vaults/DoesNotExist/status")
        assert r.status_code == 404


# ── /api/vaults/{name}/pages ─────────────────────────────────────────────────


class TestApiPages:
    def test_returns_page_list(self, client, vault_name):
        r = client.get(f"/api/vaults/{vault_name}/pages")
        assert r.status_code == 200
        pages = r.json()["pages"]
        titles = [p["title"] for p in pages]
        assert "Transformers" in titles
        assert "Attention" in titles

    def test_filters_by_category(self, client, vault_name):
        r = client.get(f"/api/vaults/{vault_name}/pages?category=Concepts")
        assert r.status_code == 200
        pages = r.json()["pages"]
        assert all(p["category"] == "Concepts" for p in pages)

    def test_pages_have_required_fields(self, client, vault_name):
        r = client.get(f"/api/vaults/{vault_name}/pages")
        page = r.json()["pages"][0]
        for field in ("file_path", "title", "category", "summary", "tags", "backlinks"):
            assert field in page


# ── /api/vaults/{name}/pages/content ─────────────────────────────────────────


class TestApiPageContent:
    def test_returns_file_content(self, client, vault_name):
        r = client.get(f"/api/vaults/{vault_name}/pages/content?file_path=Concepts/Transformers.md")
        assert r.status_code == 200
        data = r.json()
        assert "file_path" in data
        assert "content" in data
        assert "Transformers" in data["content"]

    def test_404_for_missing_page(self, client, vault_name):
        r = client.get(f"/api/vaults/{vault_name}/pages/content?file_path=Concepts/Missing.md")
        assert r.status_code == 404

    def test_path_traversal_rejected(self, client, vault_name):
        r = client.get(
            f"/api/vaults/{vault_name}/pages/content?file_path=../../.llm-wiki/config.json"
        )
        assert r.status_code == 400


# ── /api/vaults/{name}/search ────────────────────────────────────────────────


class TestApiSearch:
    def test_returns_results_for_matching_query(self, client, vault_name):
        r = client.get(f"/api/vaults/{vault_name}/search?q=attention")
        assert r.status_code == 200
        results = r.json()["results"]
        assert len(results) > 0

    def test_returns_empty_for_no_match(self, client, vault_name):
        r = client.get(f"/api/vaults/{vault_name}/search?q=xyzzy_impossible_term")
        assert r.status_code == 200
        assert r.json()["results"] == []

    def test_respects_limit_param(self, client, vault_name):
        r = client.get(f"/api/vaults/{vault_name}/search?q=the&limit=1")
        assert len(r.json()["results"]) <= 1


# ── /api/vaults/{name}/graph ──────────────────────────────────────────────────


class TestApiGraph:
    def test_returns_nodes_and_edges(self, client, vault_name):
        r = client.get(f"/api/vaults/{vault_name}/graph")
        assert r.status_code == 200
        data = r.json()
        assert "nodes" in data
        assert "edges" in data

    def test_nodes_have_required_fields(self, client, vault_name):
        r = client.get(f"/api/vaults/{vault_name}/graph")
        node = r.json()["nodes"][0]
        for field in ("id", "title", "file_path", "category", "backlink_count"):
            assert field in node

    def test_edges_reference_valid_node_ids(self, client, vault_name):
        r = client.get(f"/api/vaults/{vault_name}/graph")
        data = r.json()
        node_ids = {n["id"] for n in data["nodes"]}
        for edge in data["edges"]:
            assert edge["source"] in node_ids
            assert edge["target"] in node_ids


# ── /api/vaults/{name}/log ───────────────────────────────────────────────────


class TestApiLog:
    def test_returns_log_content(self, client, vault_name):
        r = client.get(f"/api/vaults/{vault_name}/log")
        assert r.status_code == 200
        assert "content" in r.json()

    def test_returns_empty_string_when_no_log(self, client, vault_name, populated_vault):
        (populated_vault / "wiki" / "log.md").unlink()
        r = client.get(f"/api/vaults/{vault_name}/log")
        assert r.status_code == 200
        assert r.json()["content"] == ""


# ── /api/vaults/{name}/reconcile ─────────────────────────────────────────────


class TestApiReconcile:
    def test_returns_reconcile_stats(self, client, vault_name):
        r = client.post(f"/api/vaults/{vault_name}/reconcile")
        assert r.status_code == 200
        data = r.json()
        assert "added" in data
        assert "updated" in data
        assert "removed" in data


# ── /api/vaults/{name}/ingest ────────────────────────────────────────────────


class TestApiIngest:
    def test_calls_ingest_source_and_returns_result(self, client, vault_name):
        with patch(
            "core.server.ingest_source",
            return_value={
                "source_page": {"file_path": "Sources/Via_API.md", "content": "..."},
                "page_updates": [],
                "pages_written": ["Sources/Via_API.md"],
            },
        ) as mock_ingest:
            r = client.post(
                f"/api/vaults/{vault_name}/ingest",
                json={"source": "https://example.com", "dry_run": False},
            )
        assert r.status_code == 200
        mock_ingest.assert_called_once()

    def test_dry_run_flag_passed_through(self, client, vault_name):
        with patch(
            "core.server.ingest_source",
            return_value={
                "source_page": {"file_path": "Sources/X.md", "content": ""},
                "page_updates": [],
                "pages_written": [],
            },
        ) as mock_ingest:
            client.post(
                f"/api/vaults/{vault_name}/ingest",
                json={"source": "/tmp/test.txt", "dry_run": True},
            )
        assert mock_ingest.call_args.kwargs.get("dry_run") is True


# ── /api/vaults/{name}/query ─────────────────────────────────────────────────


class TestApiQuery:
    def test_returns_answer(self, client, vault_name):
        # server.py does a local import: `from .query import query_wiki`
        with patch(
            "core.server.query_wiki", return_value={"answer": "42", "sources": [], "saved_to": None}
        ):
            r = client.post(
                f"/api/vaults/{vault_name}/query", json={"question": "What is the answer?"}
            )
        assert r.status_code == 200
        assert r.json()["answer"] == "42"

    def test_passes_save_as_when_provided(self, client, vault_name):
        with patch(
            "core.server.query_wiki",
            return_value={"answer": "A", "sources": [], "saved_to": "Concepts/SavedQ.md"},
        ) as mock_query:
            client.post(
                f"/api/vaults/{vault_name}/query", json={"question": "Q?", "save_as": "SavedQ"}
            )
        assert mock_query.call_args.kwargs.get("save_as") == "SavedQ"


# ── /api/vaults/{name}/lint ──────────────────────────────────────────────────


class TestApiLint:
    def test_returns_lint_result(self, client, vault_name):
        # server.py does a local import: `from .lint import lint_vault`
        with patch(
            "core.server.lint_vault",
            return_value={
                "structural": {"orphans": [], "broken_links": {}, "missing_summaries": []},
                "llm_report": "All good.",
                "saved_to": "lint-20260524-1200.md",
            },
        ):
            r = client.post(f"/api/vaults/{vault_name}/lint")
        assert r.status_code == 200
        data = r.json()
        assert "structural" in data
        assert "llm_report" in data
        assert "saved_to" in data
