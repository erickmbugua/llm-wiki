from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, field
from pathlib import Path

GLOBAL_CONFIG_DIR = Path.home() / ".llm-wiki"
GLOBAL_CONFIG_FILE = GLOBAL_CONFIG_DIR / "config.json"
VAULT_INTERNAL_DIR = ".llm-wiki"
VAULT_CONFIG_FILE = "config.json"
VAULT_DB_FILE = "wiki.db"

__all__ = [
    "VAULT_DB_FILE",
    "VAULT_INTERNAL_DIR",
    "GlobalConfig",
    "VaultConfig",
    "resolve_chunk_config",
    "resolve_context_chars",
    "resolve_embedding_config",
    "resolve_model",
]

# Process-level config caches — eliminated repeated disk reads during a single operation.
# Cleared by save() and by _clear_*_cache() helpers used in tests and server reset.
_global_cfg_cache: GlobalConfig | None = None
_vault_cfg_cache: dict[str, VaultConfig] = {}


def _clear_global_config_cache() -> None:
    """Invalidate the GlobalConfig process cache. Call after any mutation saved to disk."""
    global _global_cfg_cache
    _global_cfg_cache = None


def _clear_vault_config_cache(vault_path: Path | None = None) -> None:
    """Invalidate VaultConfig cache for one vault (or all vaults when vault_path is None)."""
    global _vault_cfg_cache
    if vault_path is None:
        _vault_cfg_cache.clear()
    else:
        _vault_cfg_cache.pop(str(vault_path), None)


@dataclass
class GlobalConfig:
    vaults: dict[str, str] = field(default_factory=lambda: {})
    default_vault: str | None = None
    model: str = "ollama/qwen2.5-coder:7b"
    server_port: int = 8000
    context_chars: int = 24_000
    chunk_size: int = 20_000
    chunk_overlap: int = 500
    embedding_model: str = "ollama/nomic-embed-text"
    embedding_dim: int = 768

    @classmethod
    def _config_path(cls) -> Path:
        """Return the GlobalConfig file path, honouring LLM_WIKI_HOME when set.

        The env var allows e2e tests and alternative installations to redirect the
        config location without modifying ~/.llm-wiki.

        Returns:
            Path to the config JSON file that load() and save() should use.
        """
        home = os.environ.get("LLM_WIKI_HOME")
        return Path(home) / "config.json" if home else GLOBAL_CONFIG_FILE

    @classmethod
    def load(cls) -> GlobalConfig:
        """Load config from the global config file, returning defaults if the file is absent.

        The config file location defaults to ``~/.llm-wiki/config.json`` and can be
        overridden by setting the ``LLM_WIKI_HOME`` environment variable to an alternate
        directory. This is useful for e2e tests and multi-user installations.

        Results are cached for the lifetime of the process. Call ``_clear_global_config_cache()``
        to force a re-read (e.g. after mutating the file from another code path).

        Automatically drops any registered vault whose path no longer exists on disk
        and updates the file if anything was removed.
        """
        global _global_cfg_cache
        if _global_cfg_cache is not None:
            return _global_cfg_cache
        config_file = cls._config_path()
        if config_file.exists():
            data = json.loads(config_file.read_text())
            instance = cls(**{k: v for k, v in data.items() if k in cls.__dataclass_fields__})
        else:
            instance = cls()
        if instance.reconcile_vaults():
            instance.save()  # save() sets _global_cfg_cache = self
            return instance
        _global_cfg_cache = instance
        return _global_cfg_cache

    def save(self) -> None:
        """Persist the current config to the global config file, creating the directory if needed.

        Respects ``LLM_WIKI_HOME`` the same way ``load()`` does. Also invalidates the
        process cache so the next ``load()`` reads the updated file.
        """
        config_file = self._config_path()
        config_file.parent.mkdir(parents=True, exist_ok=True)
        config_file.write_text(json.dumps(asdict(self), indent=2))
        global _global_cfg_cache
        _global_cfg_cache = self

    def reconcile_vaults(self) -> list[str]:
        """Drop registered vaults whose paths no longer exist on disk.

        If the current default vault is removed, the default is reassigned to the
        first remaining vault, or set to ``None`` when no vaults remain.

        Returns:
            Names of vaults that were removed from the registry.
        """
        dropped = [name for name, path in self.vaults.items() if not Path(path).exists()]
        for name in dropped:
            del self.vaults[name]
        if self.default_vault in dropped:
            self.default_vault = next(iter(self.vaults), None)
        return dropped

    def register_vault(self, name: str, path: Path) -> None:
        """Add a vault to the registry and save. Sets it as default if it is the first vault.

        Args:
            name: Human-readable vault identifier (e.g. "AI-Agents").
            path: Absolute or relative path to the vault root; resolved to absolute.
        """
        self.vaults[name] = str(path.resolve())
        if self.default_vault is None:
            self.default_vault = name
        self.save()

    def resolve_vault(self, name: str | None = None) -> tuple[str, Path]:
        """Return the (name, path) pair for the requested vault, falling back to the default.

        Args:
            name: Vault name to look up. Uses ``default_vault`` when None.

        Returns:
            A tuple of (vault_name, vault_path).

        Raises:
            ValueError: No name provided and no default is set.
            KeyError: The requested vault name is not registered.
        """
        target = name or self.default_vault
        if target is None:
            raise ValueError("No vault specified and no default set. Run `llm-wiki init` first.")
        if target not in self.vaults:
            raise KeyError(f"Vault '{target}' not registered. Run `llm-wiki list` to see vaults.")
        return target, Path(self.vaults[target])


@dataclass
class VaultConfig:
    name: str = ""
    model: str | None = None  # overrides global when set
    context_chars: int | None = None  # overrides global when set
    chunk_size: int | None = None  # overrides global when set
    chunk_overlap: int | None = None  # overrides global when set
    embedding_model: str | None = None  # overrides global when set
    embedding_dim: int | None = None  # overrides global when set

    @classmethod
    def load(cls, vault_path: Path) -> VaultConfig:
        """Load per-vault config from <vault>/.llm-wiki/config.json, returning defaults if absent.

        Results are cached per vault path for the lifetime of the process. Call
        ``_clear_vault_config_cache(vault_path)`` to force a re-read after an external mutation.

        Args:
            vault_path: Root directory of the vault.

        Returns:
            A VaultConfig instance populated from disk (or default values).
        """
        key = str(vault_path)
        if key in _vault_cfg_cache:
            return _vault_cfg_cache[key]
        cfg_file = vault_path / VAULT_INTERNAL_DIR / VAULT_CONFIG_FILE
        if cfg_file.exists():
            data = json.loads(cfg_file.read_text())
            instance = cls(**{k: v for k, v in data.items() if k in cls.__dataclass_fields__})
        else:
            instance = cls()
        _vault_cfg_cache[key] = instance
        return instance

    def save(self, vault_path: Path) -> None:
        """Persist per-vault config to <vault>/.llm-wiki/config.json.

        Also updates the process cache so the next ``load()`` reflects the saved values.

        Args:
            vault_path: Root directory of the vault.
        """
        cfg_dir = vault_path / VAULT_INTERNAL_DIR
        cfg_dir.mkdir(parents=True, exist_ok=True)
        (cfg_dir / VAULT_CONFIG_FILE).write_text(json.dumps(asdict(self), indent=2))
        _vault_cfg_cache[str(vault_path)] = self


def resolve_model(vault_path: Path | None = None) -> str:
    """Return the effective LLM model string, using a three-level priority chain.

    Priority: vault-level override > global config model > hardcoded default.

    Args:
        vault_path: Root directory of the vault. When None, only the global config is checked.

    Returns:
        A litellm-compatible model string (e.g. ``"claude-sonnet-4-6"``).
    """
    global_cfg = GlobalConfig.load()
    if vault_path is not None:
        vault_cfg = VaultConfig.load(vault_path)
        if vault_cfg.model:
            return vault_cfg.model
    return global_cfg.model


def resolve_context_chars(vault_path: Path | None = None) -> int:
    """Return the effective source char limit using the same priority chain as resolve_model.

    Priority: vault-level override > global config > hardcoded default (24_000).

    Args:
        vault_path: Root of the vault. When None, only the global config is checked.

    Returns:
        Maximum number of characters to feed to the LLM per source document.
    """
    global_cfg = GlobalConfig.load()
    if vault_path is not None:
        vault_cfg = VaultConfig.load(vault_path)
        if vault_cfg.context_chars is not None:
            return vault_cfg.context_chars
    return global_cfg.context_chars


def resolve_chunk_config(vault_path: Path | None = None) -> tuple[int, int]:
    """Return the effective chunk_size and chunk_overlap using the three-level priority chain.

    Priority: vault-level override > global config > hardcoded defaults (20_000, 500).

    Args:
        vault_path: Root of the vault. When None, only the global config is checked.

    Returns:
        A tuple of (chunk_size, chunk_overlap). chunk_size is the maximum characters per
        chunk; chunk_overlap is the characters of context shared between adjacent chunks.
    """
    global_cfg = GlobalConfig.load()
    chunk_size = global_cfg.chunk_size
    chunk_overlap = global_cfg.chunk_overlap
    if vault_path is not None:
        vault_cfg = VaultConfig.load(vault_path)
        if vault_cfg.chunk_size is not None:
            chunk_size = vault_cfg.chunk_size
        if vault_cfg.chunk_overlap is not None:
            chunk_overlap = vault_cfg.chunk_overlap
    return chunk_size, chunk_overlap


def resolve_embedding_config(vault_path: Path | None = None) -> tuple[str, int]:
    """Return the effective embedding model and dimension using the three-level priority chain.

    Priority: vault-level override > global config > hardcoded defaults
    (``"ollama/nomic-embed-text"``, ``768``).

    Args:
        vault_path: Root of the vault. When None, only the global config is checked.

    Returns:
        A tuple of (embedding_model, embedding_dim).
    """
    global_cfg = GlobalConfig.load()
    embedding_model = global_cfg.embedding_model
    embedding_dim = global_cfg.embedding_dim
    if vault_path is not None:
        vault_cfg = VaultConfig.load(vault_path)
        if vault_cfg.embedding_model is not None:
            embedding_model = vault_cfg.embedding_model
        if vault_cfg.embedding_dim is not None:
            embedding_dim = vault_cfg.embedding_dim
    return embedding_model, embedding_dim
