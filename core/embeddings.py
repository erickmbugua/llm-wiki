from __future__ import annotations

import logging

import litellm

__all__ = ["compute_embedding"]

log = logging.getLogger(__name__)


def compute_embedding(text: str, model: str) -> list[float]:
    """Compute a dense embedding vector for text using the given litellm model.

    Args:
        text: Text to embed. Truncated to 8192 chars to stay within model context limits.
        model: litellm embedding model string (e.g. ``"ollama/nomic-embed-text"``).

    Returns:
        A list of floats representing the embedding vector.

    Raises:
        RuntimeError: The embedding model is unavailable or returns an unexpected shape.
    """
    try:
        response = litellm.embedding(  # pyright: ignore[reportAttributeAccessIssue]
            model=model, input=[text[:8192]]
        )
        result = response.data[0].embedding  # pyright: ignore[reportAttributeAccessIssue]
        return [float(v) for v in result]
    except Exception as exc:
        raise RuntimeError(
            f"Embedding failed for model '{model}': {exc}. "
            "Ensure the embedding model is running and pulled."
        ) from exc
