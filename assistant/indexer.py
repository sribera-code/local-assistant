"""Decoupage en chunks, calcul des embeddings et mise a jour de l'index."""

from __future__ import annotations

import re
import sqlite3
from typing import Callable, Iterable, Sequence

from . import db
from .config import Config
from .llm import LLM

EMBED_BATCH = 16
_PARAGRAPH_RE = re.compile(r"\n\s*\n")


def chunk_text(text: str, *, size: int = 1200, overlap: int = 150) -> list[str]:
    """Decoupe en respectant les paragraphes tant que possible.

    Un chunk qui s'arrete au milieu d'une phrase donne des embeddings flous ;
    on colle donc des paragraphes entiers jusqu'a la taille cible.
    """
    text = text.strip()
    if not text:
        return []
    if len(text) <= size:
        return [text]

    chunks: list[str] = []
    current = ""
    for para in _PARAGRAPH_RE.split(text):
        para = para.strip()
        if not para:
            continue
        if len(current) + len(para) + 2 <= size:
            current = f"{current}\n\n{para}" if current else para
            continue
        if current:
            chunks.append(current)
            current = ""
        if len(para) <= size:
            current = para
            continue
        # Paragraphe plus long que la taille cible : on le coupe avec recouvrement.
        start = 0
        while start < len(para):
            chunks.append(para[start : start + size])
            start += size - overlap
    if current:
        chunks.append(current)
    return [c for c in chunks if c.strip()]


def index_documents(
    conn: sqlite3.Connection,
    llm: LLM,
    cfg: Config,
    doc_ids: Iterable[int],
    *,
    progress: Callable[[int, int], None] | None = None,
) -> int:
    """(Re)calcule les chunks et embeddings des documents donnes. Retourne le nombre de chunks."""
    ids = list(dict.fromkeys(doc_ids))
    total_chunks = 0

    for position, doc_id in enumerate(ids, start=1):
        row = db.get_document(conn, doc_id)
        if row is None:
            continue
        texts = chunk_text(
            row["body"] or "",
            size=cfg.retrieval.chunk_chars,
            overlap=cfg.retrieval.chunk_overlap,
        )
        # Embeddings calcules AVANT de toucher a la base : la suppression des anciens
        # chunks ouvre une transaction d'ecriture, qui resterait sinon ouverte pendant
        # les appels a Ollama et bloquerait tous les autres ecrivains.
        vectors: list[Sequence[float]] = []
        for start in range(0, len(texts), EMBED_BATCH):
            vectors.extend(llm.embed(texts[start : start + EMBED_BATCH]))

        db.clear_chunks(conn, doc_id)
        if texts:
            db.add_chunks(conn, doc_id, texts, vectors)  # pose aussi indexed_at
        else:
            # Rien a indexer : on le note quand meme, sinon il repasserait a chaque synchro.
            conn.execute(
                "UPDATE documents SET indexed_at = strftime('%s','now') WHERE id = ?", (doc_id,)
            )
        conn.commit()
        if not texts:
            continue
        total_chunks += len(texts)
        if progress:
            progress(position, len(ids))

    return total_chunks


def pending_document_ids(conn: sqlite3.Connection) -> list[int]:
    """Documents jamais indexes, ou modifies depuis leur derniere indexation.

    upsert_document remet indexed_at a NULL des que le contenu change, donc cette
    seule colonne suffit a retrouver tout ce qui reste a embedder.
    """
    rows = db.iter_rows(conn, "SELECT id FROM documents WHERE indexed_at IS NULL ORDER BY ts DESC")
    return [r["id"] for r in rows]


def reset_index(conn: sqlite3.Connection) -> None:
    """Vide chunks / FTS / vecteurs en gardant les documents : permet de rembedder."""
    conn.executescript(
        """
        DELETE FROM chunks_fts;
        DELETE FROM chunks_vec;
        DELETE FROM chunks;
        UPDATE documents SET indexed_at = NULL;
        DELETE FROM meta WHERE key = 'embed_dim';
        """
    )
    conn.commit()
