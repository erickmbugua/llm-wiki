from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path

GLOBAL_CONFIG_DIR = Path.home() / ".llm-wiki"
GLOBAL_CONFIG_FILE = GLOBAL_CONFIG_DIR / "config.json"
VAULT_INTERNAL_DIR = ".llm-wiki"
VAULT_CONFIG_FILE = "config.json"
VAULT_DB_FILE = "wiki.db"


@dataclass
class GlobalConfig:
    vaults: dict[str, str] = field(default_factory=lambda: {})
    default_vault: str | None = None
    model: str = "claude-sonnet-4-6"
    server_port: int = 8000

    @classmethod
    def load(cls) -> GlobalConfig:
        """Load config from ~/.llm-wiki/config.json, returning defaults if the file is absent."""
        if GLOBAL_CONFIG_FILE.exists():
            data = json.loads(GLOBAL_CONFIG_FILE.read_text())
            return cls(**{k: v for k, v in data.items() if k in cls.__dataclass_fields__})
        return cls()

    def save(self) -> None:
        """Persist the current config to ~/.llm-wiki/config.json, creating the directory if needed."""
        GLOBAL_CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        GLOBAL_CONFIG_FILE.write_text(json.dumps(asdict(self), indent=2))

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

    @classmethod
    def load(cls, vault_path: Path) -> VaultConfig:
        """Load per-vault config from <vault>/.llm-wiki/config.json, returning defaults if absent.

        Args:
            vault_path: Root directory of the vault.

        Returns:
            A VaultConfig instance populated from disk (or default values).
        """
        cfg_file = vault_path / VAULT_INTERNAL_DIR / VAULT_CONFIG_FILE
        if cfg_file.exists():
            data = json.loads(cfg_file.read_text())
            return cls(**{k: v for k, v in data.items() if k in cls.__dataclass_fields__})
        return cls()

    def save(self, vault_path: Path) -> None:
        """Persist per-vault config to <vault>/.llm-wiki/config.json.

        Args:
            vault_path: Root directory of the vault.
        """
        cfg_dir = vault_path / VAULT_INTERNAL_DIR
        cfg_dir.mkdir(parents=True, exist_ok=True)
        (cfg_dir / VAULT_CONFIG_FILE).write_text(json.dumps(asdict(self), indent=2))


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
