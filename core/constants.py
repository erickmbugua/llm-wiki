"""Project-wide constants shared across core modules."""

from __future__ import annotations

# Canonical set of top-level wiki category directory names.
# Import this wherever a membership check or directory iteration is needed so
# that adding a new category (e.g. "Projects") requires only one change here.
WIKI_CATEGORIES: frozenset[str] = frozenset({"Sources", "Concepts", "Entities"})
