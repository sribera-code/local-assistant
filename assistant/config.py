"""Chargement de la configuration depuis config.yaml."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = Path(os.environ.get("ASSISTANT_CONFIG", ROOT / "config.yaml"))


@dataclass
class OllamaConfig:
    host: str = "http://127.0.0.1:11434"
    chat_model: str = "qwen3.5:4b"
    triage_model: str = "qwen3.5:4b"
    embed_model: str = "qwen3-embedding:0.6b"
    embed_dim: int = 1024
    num_ctx: int = 32768
    triage_num_ctx: int = 8192
    keep_alive: str = "15m"
    request_timeout: int = 300


@dataclass
class PathsConfig:
    db: str = "data/assistant.db"
    google_client_secret: str = "secrets/client_secret.json"
    google_token: str = "secrets/token.json"

    def resolve(self, value: str) -> Path:
        # expanduser() d'abord : sans lui, "~/dossier" serait pris pour un chemin
        # relatif et resolu sous le repo, ce qui casse les chemins hors du projet.
        p = Path(value).expanduser()
        return p if p.is_absolute() else ROOT / p

    @property
    def db_path(self) -> Path:
        return self.resolve(self.db)

    @property
    def client_secret_path(self) -> Path:
        return self.resolve(self.google_client_secret)

    @property
    def token_path(self) -> Path:
        return self.resolve(self.google_token)


@dataclass
class GmailConfig:
    initial_backfill: int = 500
    query: str = "-in:spam -in:trash"
    poll_seconds: int = 120


@dataclass
class CalendarConfig:
    calendar_ids: list[str] = field(default_factory=lambda: ["*"])
    exclude_patterns: list[str] = field(default_factory=lambda: ["#weeknum@"])
    past_days: int = 30
    future_days: int = 90
    poll_seconds: int = 600


@dataclass
class FilesConfig:
    roots: list[str] = field(default_factory=list)
    extensions: list[str] = field(
        default_factory=lambda: [".txt", ".md", ".pdf", ".docx", ".xlsx", ".csv", ".json", ".yaml", ".yml"]
    )
    exclude_dirs: list[str] = field(
        default_factory=lambda: [".git", "node_modules", ".venv", "__pycache__", "dist", "build"]
    )
    max_file_mb: float = 25


@dataclass
class TriageConfig:
    enabled: bool = True
    notify_min_urgency: int = 4
    vip: list[str] = field(default_factory=list)
    quiet_hours: dict[str, int] | None = field(default_factory=lambda: {"start": 22, "end": 8})


@dataclass
class WebConfig:
    host: str = "127.0.0.1"
    port: int = 8765
    open_browser: bool = True
    # Synchro automatique en tache de fond pendant que l'interface tourne.
    background_sync: bool = True


@dataclass
class RetrievalConfig:
    top_k: int = 8
    chunk_chars: int = 1200
    chunk_overlap: int = 150


@dataclass
class Config:
    ollama: OllamaConfig = field(default_factory=OllamaConfig)
    paths: PathsConfig = field(default_factory=PathsConfig)
    gmail: GmailConfig = field(default_factory=GmailConfig)
    calendar: CalendarConfig = field(default_factory=CalendarConfig)
    files: FilesConfig = field(default_factory=FilesConfig)
    triage: TriageConfig = field(default_factory=TriageConfig)
    web: WebConfig = field(default_factory=WebConfig)
    retrieval: RetrievalConfig = field(default_factory=RetrievalConfig)


_SECTIONS = {
    "ollama": OllamaConfig,
    "paths": PathsConfig,
    "gmail": GmailConfig,
    "calendar": CalendarConfig,
    "files": FilesConfig,
    "triage": TriageConfig,
    "web": WebConfig,
    "retrieval": RetrievalConfig,
}


def _build_section(name: str, raw: dict[str, Any]) -> Any:
    cls = _SECTIONS[name]
    known = {f.name for f in cls.__dataclass_fields__.values()}
    unknown = set(raw) - known
    if unknown:
        raise ValueError(f"config.yaml : cles inconnues dans '{name}' : {sorted(unknown)}")
    return cls(**raw)


def load_config(path: Path | None = None) -> Config:
    path = path or CONFIG_PATH
    if not path.exists():
        example = ROOT / "config.example.yaml"
        raise FileNotFoundError(
            f"Configuration absente : {path}\n"
            f"Copie le modele puis adapte-le :  copy {example.name} config.yaml"
        )
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    unknown = set(raw) - set(_SECTIONS)
    if unknown:
        raise ValueError(f"config.yaml : sections inconnues {sorted(unknown)}")
    return Config(**{name: _build_section(name, raw.get(name) or {}) for name in raw})


_cached: Config | None = None


def get_config() -> Config:
    """Configuration partagee, chargee une seule fois par process."""
    global _cached
    if _cached is None:
        _cached = load_config()
    return _cached
