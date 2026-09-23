"""Orchestration : synchroniser les sources, indexer ce qui a change, trier les mails.

Utilise aussi bien par la ligne de commande que par la boucle de fond de l'interface.
"""

from __future__ import annotations

import sqlite3
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Sequence

from . import db, indexer, triage
from .config import Config
from .ingest import files as files_src
from .ingest import gcal, gmail
from .ingest.google_auth import ReauthRequired
from .llm import LLM

SOURCES = ("mail", "event", "file")
_SYNCERS = {"mail": gmail.sync, "event": gcal.sync, "file": files_src.sync}

# Un seul thread a le droit de synchroniser a la fois : sans cela, un clic sur
# "Synchroniser" pendant la boucle de fond declencherait deux embeddings simultanes
# sur un GPU qui n'a pas la VRAM pour.
_sync_lock = threading.Lock()


@dataclass
class SyncReport:
    changed: dict[str, int] = field(default_factory=dict)
    indexed_chunks: int = 0
    notifications: list[dict[str, Any]] = field(default_factory=list)
    errors: dict[str, str] = field(default_factory=dict)
    skipped: bool = False
    duration_s: float = 0.0

    @property
    def total_changed(self) -> int:
        return sum(self.changed.values())

    def summary(self) -> str:
        if self.skipped:
            return "synchro deja en cours, passage ignore"
        parts = [f"{k}: {v}" for k, v in self.changed.items() if v]
        label = ", ".join(parts) if parts else "rien de nouveau"
        if self.errors:
            label += " | erreurs : " + "; ".join(f"{k} ({v})" for k, v in self.errors.items())
        return f"{label} - {self.indexed_chunks} chunks en {self.duration_s:.1f}s"


def sync_all(
    conn: sqlite3.Connection,
    llm: LLM,
    cfg: Config,
    *,
    sources: Sequence[str] = SOURCES,
    full: bool = False,
    do_triage: bool = True,
    notify_user: bool = True,
    progress: Callable[[str], None] | None = None,
) -> SyncReport:
    report = SyncReport()
    if not _sync_lock.acquire(blocking=False):
        report.skipped = True
        return report

    started = time.monotonic()
    try:
        for source in sources:
            syncer = _SYNCERS.get(source)
            if syncer is None:
                continue
            if source == "file" and not cfg.files.roots:
                continue  # aucun dossier configure
            if progress:
                progress(f"synchro {source}...")
            try:
                changed = syncer(conn, cfg, full=full)
            except (FileNotFoundError, ReauthRequired) as exc:
                # Identifiants Google absents, ou jeton revoque : on n'ouvre surtout pas
                # un navigateur depuis une synchro de fond. Les fichiers locaux, eux,
                # continuent de fonctionner.
                report.errors[source] = str(exc).splitlines()[0]
                continue
            except Exception as exc:
                report.errors[source] = f"{type(exc).__name__}: {exc}"
                continue
            report.changed[source] = len(changed)

        pending = indexer.pending_document_ids(conn)
        if pending:
            if progress:
                progress(f"indexation de {len(pending)} documents...")
            report.indexed_chunks = indexer.index_documents(conn, llm, cfg, pending)

        if do_triage and "mail" in sources:
            # doc_ids=None : tous les mails pas encore tries, pas seulement ceux de
            # cette passe. Une synchro interrompue laisse sinon des mails non classes
            # que plus rien ne viendrait reprendre.
            if progress:
                progress("tri des mails...")
            report.notifications = triage.run(conn, llm, cfg, None, notify_user=notify_user)
    finally:
        report.duration_s = time.monotonic() - started
        _sync_lock.release()

    return report


class BackgroundSync(threading.Thread):
    """Boucle de fond : verifie les mails souvent, l'agenda et les fichiers moins souvent."""

    def __init__(self, llm: LLM, cfg: Config) -> None:
        super().__init__(name="background-sync", daemon=True)
        self.llm = llm
        self.cfg = cfg
        self._stop = threading.Event()
        self.last_report: SyncReport | None = None
        self.last_run_ts: int | None = None

    def stop(self) -> None:
        self._stop.set()

    def run(self) -> None:
        # Connexion propre a ce thread (voir db.thread_connection).
        conn = db.thread_connection(self.cfg.paths.db_path, embed_dim=self.cfg.ollama.embed_dim)
        mail_every = max(30, self.cfg.gmail.poll_seconds)
        slow_every = max(mail_every, self.cfg.calendar.poll_seconds)
        last_slow = 0.0

        while not self._stop.is_set():
            now = time.monotonic()
            sources: list[str] = ["mail"]
            if now - last_slow >= slow_every:
                sources = list(SOURCES)
                last_slow = now
            try:
                self.last_report = sync_all(conn, self.llm, self.cfg, sources=sources)
                self.last_run_ts = int(time.time())
            except Exception:
                pass  # une synchro ratee ne doit jamais arreter la boucle
            self._stop.wait(mail_every)


def reset_index(conn: sqlite3.Connection, cfg: Config) -> None:
    indexer.reset_index(conn)
    db.set_meta(conn, "embed_dim", str(cfg.ollama.embed_dim))
    conn.commit()
