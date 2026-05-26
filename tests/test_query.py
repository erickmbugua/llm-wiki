"""Tests for core/query.py — context building, save_as, full query flow."""

from collections.abc import Callable
from pathlib import Path
from unittest.mock import MagicMock, patch

from core.prompts import build_query_prompt
from core.query import build_context, query_wiki, save_answer

# ── build_context ─────────────────────────────────────────────────────────────


class TestBuildContext:
    def test_returns_empty_message_when_no_pages(self, tmp_vault: Path) -> None:
        wiki = tmp_vault / "wiki"
        context, sources = build_context(tmp_vault, wiki, "anything")
        assert "No relevant pages" in context
        assert sources == []

    def test_returns_relevant_page_content(self, populated_vault: Path) -> None:
        wiki = populated_vault / "wiki"
        _context, sources = build_context(populated_vault, wiki, "attention mechanism")
        assert len(sources) > 0
        assert any("Attention" in s or "Concepts" in s for s in sources)

    def test_sources_are_file_paths(self, populated_vault: Path) -> None:
        wiki = populated_vault / "wiki"
        _, sources = build_context(populated_vault, wiki, "transformer")
        for s in sources:
            assert s.endswith(".md")


# ── build_query_prompt ────────────────────────────────────────────────────────


class TestBuildQueryPrompt:
    def test_contains_question(self) -> None:
        prompt = build_query_prompt("What is attention?", "some context")
        assert "What is attention?" in prompt

    def test_contains_context(self) -> None:
        prompt = build_query_prompt("Q?", "unique_context_marker_xyz")
        assert "unique_context_marker_xyz" in prompt


# ── save_answer ───────────────────────────────────────────────────────────────


class TestSaveAnswer:
    def test_creates_file_at_given_path(self, tmp_vault: Path) -> None:
        wiki = tmp_vault / "wiki"
        path = save_answer(wiki, "Concepts/MyAnswer.md", "What is X?", "X is Y.", [])
        assert (wiki / "Concepts" / "MyAnswer.md").exists()
        assert path == "Concepts/MyAnswer.md"

    def test_defaults_to_concepts_dir(self, tmp_vault: Path) -> None:
        wiki = tmp_vault / "wiki"
        path = save_answer(wiki, "JustAName", "Q?", "A.", [])
        assert path == "Concepts/JustAName.md"

    def test_file_contains_question_and_answer(self, tmp_vault: Path) -> None:
        wiki = tmp_vault / "wiki"
        save_answer(wiki, "Concepts/QA.md", "What is gravity?", "A force.", [])
        content = (wiki / "Concepts" / "QA.md").read_text()
        assert "What is gravity?" in content
        assert "A force." in content

    def test_file_contains_source_links(self, tmp_vault: Path) -> None:
        wiki = tmp_vault / "wiki"
        save_answer(wiki, "Concepts/QA.md", "Q?", "A.", ["Sources/Ref.md"])
        content = (wiki / "Concepts" / "QA.md").read_text()
        assert "Sources/Ref" in content

    def test_creates_parent_dirs(self, tmp_vault: Path) -> None:
        wiki = tmp_vault / "wiki"
        save_answer(wiki, "Concepts/Sub/Answer.md", "Q?", "A.", [])
        assert (wiki / "Concepts" / "Sub" / "Answer.md").exists()

    def test_has_yaml_frontmatter(self, tmp_vault: Path) -> None:
        wiki = tmp_vault / "wiki"
        save_answer(wiki, "Concepts/FM.md", "Q?", "A.", [])
        content = (wiki / "Concepts" / "FM.md").read_text()
        assert content.startswith("---")
        assert "type: query-answer" in content


# ── query_wiki (full flow, LLM mocked) ───────────────────────────────────────


class TestQueryWiki:
    def test_returns_answer(
        self, populated_vault: Path, fake_llm_response: Callable[[str], MagicMock]
    ) -> None:
        with (
            patch(
                "core.query.litellm.completion", return_value=fake_llm_response("The answer is 42.")
            ),
            patch("core.query.resolve_model", return_value="claude-sonnet-4-6"),
        ):
            result = query_wiki(populated_vault, "What is the meaning of life?")
        assert result["answer"] == "The answer is 42."

    def test_returns_source_list(
        self, populated_vault: Path, fake_llm_response: Callable[[str], MagicMock]
    ) -> None:
        with (
            patch("core.query.litellm.completion", return_value=fake_llm_response("Answer.")),
            patch("core.query.resolve_model", return_value="claude-sonnet-4-6"),
        ):
            result = query_wiki(populated_vault, "attention mechanism")
        assert isinstance(result["sources"], list)

    def test_save_as_creates_page(
        self, populated_vault: Path, fake_llm_response: Callable[[str], MagicMock]
    ) -> None:
        with (
            patch("core.query.litellm.completion", return_value=fake_llm_response("Saved answer.")),
            patch("core.query.resolve_model", return_value="claude-sonnet-4-6"),
        ):
            result = query_wiki(populated_vault, "Q?", save_as="MyAnswer")
        assert result["saved_to"] == "Concepts/MyAnswer.md"
        assert (populated_vault / "wiki" / "Concepts" / "MyAnswer.md").exists()

    def test_saved_to_is_none_when_not_requested(
        self, populated_vault: Path, fake_llm_response: Callable[[str], MagicMock]
    ) -> None:
        with (
            patch("core.query.litellm.completion", return_value=fake_llm_response("A.")),
            patch("core.query.resolve_model", return_value="claude-sonnet-4-6"),
        ):
            result = query_wiki(populated_vault, "Q?")
        assert result["saved_to"] is None


# ── build_context — hybrid search wiring ──────────────────────────────────────


_FAKE_VEC = [0.1] * 768


class TestBuildContextHybridSearch:
    def test_hybrid_search_called_with_embedding(self, populated_vault: Path) -> None:
        wiki = populated_vault / "wiki"
        with (
            patch("core.query.compute_embedding", return_value=_FAKE_VEC),
            patch("core.query.hybrid_search", return_value=[]) as mock_search,
        ):
            build_context(populated_vault, wiki, "attention")

        mock_search.assert_called_once()
        positional = mock_search.call_args[0]
        assert positional[1] == "attention"
        assert positional[2] == _FAKE_VEC

    def test_falls_back_to_none_embedding_on_error(self, populated_vault: Path) -> None:
        wiki = populated_vault / "wiki"
        with (
            patch("core.query.compute_embedding", side_effect=RuntimeError("model down")),
            patch("core.query.hybrid_search", return_value=[]) as mock_search,
        ):
            build_context(populated_vault, wiki, "attention")

        positional = mock_search.call_args[0]
        assert positional[2] is None
