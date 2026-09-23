"""Recherche hybride : FTS5 (mots-cles) + sqlite-vec (sens), fusionnes par RRF.

Les deux approches se rattrapent l'une l'autre : le plein texte trouve un numero
de facture ou un nom propre, le vectoriel trouve "mon rendez-vous chez le dentiste"
dans un mail qui parle de "consultation dentaire".
"""

from __future__ import annotations

import json
import re
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Sequence

import sqlite_vec

from . import db
from .config import Config
from .llm import LLM

RRF_K = 60  # constante d'amortissement du Reciprocal Rank Fusion
_TOKEN_RE = re.compile(r"[\w'-]{2,}", re.UNICODE)


@dataclass
class Hit:
    doc_id: int
    chunk_id: int
    source: str
    title: str
    author: str | None
    ts: int | None
    url: str | None
    text: str
    score: float
    meta: dict[str, Any] = field(default_factory=dict)

    @property
    def date_label(self) -> str:
        if not self.ts:
            return ""
        return datetime.fromtimestamp(self.ts).astimezone().strftime("%d/%m/%Y %H:%M")

    def as_context(self, index: int) -> str:
        kind = {"mail": "MAIL", "event": "AGENDA", "file": "FICHIER"}.get(self.source, self.source)
        head = f"[{index}] ({kind}) {self.title}"
        if self.date_label:
            head += f" - {self.date_label}"
        if self.author:
            head += f" - {self.author}"
        return f"{head}\n{self.text}"

    def as_dict(self) -> dict[str, Any]:
        return {
            "doc_id": self.doc_id,
            "source": self.source,
            "title": self.title,
            "author": self.author,
            "date": self.date_label,
            "url": self.url,
            "extrait": self.text[:600],
            "score": round(self.score, 4),
        }


def fts_query(text: str) -> str:
    """Transforme une question en requete FTS5 sure.

    Les tokens sont mis entre guillemets : sans cela, un ':' ou un '-' dans la
    question serait interprete comme un operateur FTS5 et ferait echouer la requete.
    """
    tokens = _TOKEN_RE.findall(text.lower())
    if not tokens:
        return ""
    return " OR ".join(f'"{t}"' for t in tokens[:32])


def _keyword_candidates(
    conn: sqlite3.Connection, query: str, limit: int
) -> list[tuple[int, float]]:
    match = fts_query(query)
    if not match:
        return []
    rows = conn.execute(
        "SELECT rowid AS chunk_id, bm25(chunks_fts) AS rank FROM chunks_fts"
        " WHERE chunks_fts MATCH ? ORDER BY rank LIMIT ?",
        (match, limit),
    ).fetchall()
    # bm25() de SQLite renvoie un score negatif, le plus petit etant le meilleur.
    return [(r["chunk_id"], -r["rank"]) for r in rows]


def _vector_candidates(
    conn: sqlite3.Connection, llm: LLM, query: str, limit: int
) -> list[tuple[int, float]]:
    vector = llm.embed_one(query)
    rows = conn.execute(
        "SELECT chunk_id, distance FROM chunks_vec"
        " WHERE embedding MATCH ? AND k = ? ORDER BY distance",
        (sqlite_vec.serialize_float32(vector), limit),
    ).fetchall()
    return [(r["chunk_id"], -r["distance"]) for r in rows]


def _fuse(*ranked_lists: Sequence[tuple[int, float]]) -> list[tuple[int, float]]:
    """Reciprocal Rank Fusion : melange des classements sans avoir a comparer des scores
    d'echelles differentes (bm25 et distance cosinus ne sont pas commensurables)."""
    scores: dict[int, float] = {}
    for ranked in ranked_lists:
        for rank, (chunk_id, _) in enumerate(ranked, start=1):
            scores[chunk_id] = scores.get(chunk_id, 0.0) + 1.0 / (RRF_K + rank)
    return sorted(scores.items(), key=lambda kv: kv[1], reverse=True)


def search(
    conn: sqlite3.Connection,
    llm: LLM,
    cfg: Config,
    query: str,
    *,
    sources: Sequence[str] | None = None,
    top_k: int | None = None,
    since_ts: int | None = None,
    until_ts: int | None = None,
    author: str | None = None,
) -> list[Hit]:
    top_k = top_k or cfg.retrieval.top_k
    # On ratisse large avant fusion et filtrage : les filtres (source, dates) ne
    # peuvent pas etre pousses dans la recherche KNN de vec0.
    pool = max(top_k * 8, 60)

    fused = _fuse(
        _keyword_candidates(conn, query, pool),
        _vector_candidates(conn, llm, query, pool),
    )
    if not fused:
        return []

    order = {chunk_id: score for chunk_id, score in fused}
    marks = ",".join("?" * len(order))
    sql = (
        "SELECT c.id AS chunk_id, c.text, d.id AS doc_id, d.source, d.title, d.author,"
        "       d.ts, d.url, d.meta"
        f" FROM chunks c JOIN documents d ON d.id = c.doc_id WHERE c.id IN ({marks})"
    )
    params: list[Any] = list(order)

    if sources:
        sql += f" AND d.source IN ({','.join('?' * len(sources))})"
        params.extend(sources)
    if since_ts is not None:
        sql += " AND d.ts >= ?"
        params.append(since_ts)
    if until_ts is not None:
        sql += " AND d.ts <= ?"
        params.append(until_ts)
    if author:
        sql += " AND (d.author LIKE ? OR d.title LIKE ?)"
        params.extend([f"%{author}%", f"%{author}%"])

    hits = [
        Hit(
            doc_id=row["doc_id"],
            chunk_id=row["chunk_id"],
            source=row["source"],
            title=row["title"] or "",
            author=row["author"],
            ts=row["ts"],
            url=row["url"],
            text=row["text"],
            score=order[row["chunk_id"]],
            meta=json.loads(row["meta"] or "{}"),
        )
        for row in conn.execute(sql, params)
    ]
    hits.sort(key=lambda h: h.score, reverse=True)

    # Un seul chunk par document : mieux vaut huit documents differents que huit
    # morceaux du meme mail.
    seen: set[int] = set()
    unique: list[Hit] = []
    for hit in hits:
        if hit.doc_id in seen:
            continue
        seen.add(hit.doc_id)
        unique.append(hit)
        if len(unique) >= top_k:
            break
    return unique


def build_context(hits: Sequence[Hit]) -> str:
    return "\n\n---\n\n".join(hit.as_context(i) for i, hit in enumerate(hits, start=1))


def recent_mails(
    conn: sqlite3.Connection, *, limit: int = 30, unread_only: bool = False
) -> list[sqlite3.Row]:
    # Boite de reception seulement : la synchro rapatrie aussi les archives et les
    # mails envoyes, qui n'ont rien a faire dans "les derniers mails recus". Le
    # reste de l'historique reste atteignable par `rechercher`.
    sql = (
        "SELECT d.*, t.urgency, t.category, t.summary, t.action"
        " FROM documents d LEFT JOIN mail_triage t ON t.doc_id = d.id"
        " WHERE d.source = 'mail'"
        "   AND COALESCE(json_extract(d.meta, '$.folder'), 'inbox') = 'inbox'"
    )
    if unread_only:
        sql += " AND json_extract(d.meta, '$.unread') = 1"
    sql += " ORDER BY d.ts DESC LIMIT ?"
    return db.iter_rows(conn, sql, (limit,))
