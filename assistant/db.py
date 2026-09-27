"""Base SQLite locale : documents, chunks, index plein texte (FTS5) et vecteurs (sqlite-vec).

Un seul fichier .db contient tout : aucun serveur, aucune donnee hors de la machine.
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Iterable, Sequence

import sqlite_vec

SCHEMA_VERSION = 1

_local = threading.local()


def connect(db_path: Path, *, embed_dim: int) -> sqlite3.Connection:
    """Ouvre la base, charge sqlite-vec et applique le schema si besoin."""
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path, timeout=30, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.enable_load_extension(True)
    sqlite_vec.load(conn)
    conn.enable_load_extension(False)
    conn.executescript(
        """
        PRAGMA journal_mode=WAL;
        PRAGMA synchronous=NORMAL;
        PRAGMA foreign_keys=ON;
        """
    )
    _migrate(conn, embed_dim)
    return conn


def _migrate(conn: sqlite3.Connection, embed_dim: int) -> None:
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS meta (
            key   TEXT PRIMARY KEY,
            value TEXT
        );

        -- Un document = un mail, un evenement d'agenda ou un fichier local.
        CREATE TABLE IF NOT EXISTS documents (
            id           INTEGER PRIMARY KEY,
            source       TEXT    NOT NULL,      -- 'mail' | 'event' | 'file'
            external_id  TEXT    NOT NULL,      -- id Gmail / id d'evenement / chemin du fichier
            title        TEXT,
            author       TEXT,
            ts           INTEGER,               -- epoch : date du mail, debut d'evenement, mtime
            url          TEXT,
            body         TEXT,
            meta         TEXT,                  -- JSON libre, propre a chaque source
            content_hash TEXT,
            indexed_at   INTEGER,
            UNIQUE(source, external_id)
        );
        CREATE INDEX IF NOT EXISTS idx_documents_source_ts ON documents(source, ts DESC);

        CREATE TABLE IF NOT EXISTS chunks (
            id     INTEGER PRIMARY KEY,
            doc_id INTEGER NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
            ord    INTEGER NOT NULL,
            text   TEXT    NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_chunks_doc ON chunks(doc_id);

        -- Recherche par mots-cles. remove_diacritics=2 fait correspondre "reunion" et "reunion" accentue.
        CREATE VIRTUAL TABLE IF NOT EXISTS chunks_fts USING fts5(
            text,
            content='chunks',
            content_rowid='id',
            tokenize="unicode61 remove_diacritics 2"
        );

        -- Resultat du tri automatique des mails.
        CREATE TABLE IF NOT EXISTS mail_triage (
            doc_id      INTEGER PRIMARY KEY REFERENCES documents(id) ON DELETE CASCADE,
            category    TEXT,
            urgency     INTEGER,
            action_type TEXT,               -- enumeration fermee (voir triage.ACTION_TYPES)
            summary     TEXT,
            action      TEXT,
            created_at  INTEGER,
            notified_at INTEGER
        );
        CREATE INDEX IF NOT EXISTS idx_triage_urgency ON mail_triage(urgency DESC, created_at DESC);

        -- Version HTML des mails, pour l'affichage uniquement (l'index utilise le texte).
        -- html = '' : le mail n'a pas de version HTML (inutile de la redemander).
        CREATE TABLE IF NOT EXISTS mail_html (
            doc_id INTEGER PRIMARY KEY REFERENCES documents(id) ON DELETE CASCADE,
            html   TEXT NOT NULL
        );

        -- Autorisations d'afficher les images distantes d'un mail : cle
        -- "mail:<doc_id>" (ce mail) ou "sender:<adresse>" (tout l'expediteur).
        CREATE TABLE IF NOT EXISTS image_consent (
            key        TEXT PRIMARY KEY,
            created_at INTEGER
        );

        -- Resumes de mails envoyes sur Matrix. Un mail n'est envoye qu'une fois, et une
        -- reponse a ce message dans Element porte sur ce mail (event_id -> doc_id).
        -- Un resume groupe partage son event_id entre plusieurs mails.
        CREATE TABLE IF NOT EXISTS matrix_mail_notices (
            doc_id   INTEGER PRIMARY KEY REFERENCES documents(id) ON DELETE CASCADE,
            room_id  TEXT    NOT NULL,
            event_id TEXT    NOT NULL,
            sent_at  INTEGER NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_matrix_notices_event ON matrix_mail_notices(event_id);

        -- Conversations de l'assistant (un onglet chacune dans l'interface).
        CREATE TABLE IF NOT EXISTS conversations (
            id         INTEGER PRIMARY KEY,
            title      TEXT,
            created_at INTEGER,
            updated_at INTEGER
        );

        -- Historique des conversations de l'interface.
        CREATE TABLE IF NOT EXISTS messages (
            id         INTEGER PRIMARY KEY,
            role       TEXT NOT NULL,
            content    TEXT NOT NULL,
            citations  TEXT,
            created_at INTEGER
        );

        -- Curseurs des synchros incrementales (Gmail, Agenda, fichiers).
        CREATE TABLE IF NOT EXISTS sync_state (
            name       TEXT PRIMARY KEY,
            cursor     TEXT,
            updated_at INTEGER
        );
        """
    )

    # Bases anterieures aux onglets de conversation : les messages existants sont
    # regroupes dans une premiere conversation plutot que perdus.
    colonnes = {r["name"] for r in conn.execute("PRAGMA table_info(messages)")}
    if "conversation_id" not in colonnes:
        conn.execute(
            "ALTER TABLE messages ADD COLUMN conversation_id INTEGER"
            " REFERENCES conversations(id) ON DELETE CASCADE"
        )
        premier = conn.execute("SELECT MIN(created_at) t, MAX(created_at) u FROM messages").fetchone()
        if premier["t"] is not None:
            cur = conn.execute(
                "INSERT INTO conversations(title, created_at, updated_at) VALUES(?, ?, ?)",
                ("Conversation", premier["t"], premier["u"]),
            )
            conn.execute("UPDATE messages SET conversation_id = ?", (cur.lastrowid,))
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_messages_conv ON messages(conversation_id, id)"
    )

    # Bases anterieures a l'icone d'action : la colonne est ajoutee vide, et
    # l'interface retombe sur triage.fallback_action_type tant qu'elle l'est.
    colonnes = {r["name"] for r in conn.execute("PRAGMA table_info(mail_triage)")}
    if "action_type" not in colonnes:
        conn.execute("ALTER TABLE mail_triage ADD COLUMN action_type TEXT")

    stored = get_meta(conn, "embed_dim")
    if stored is None:
        set_meta(conn, "embed_dim", str(embed_dim))
        set_meta(conn, "schema_version", str(SCHEMA_VERSION))
    elif int(stored) != embed_dim:
        raise RuntimeError(
            f"La base a ete construite avec des embeddings de dimension {stored}, "
            f"la config demande {embed_dim}.\n"
            "Remets l'ancien modele d'embeddings, ou reconstruis l'index : assistant reset-index"
        )

    conn.execute(
        "CREATE VIRTUAL TABLE IF NOT EXISTS chunks_vec USING vec0("
        "  chunk_id INTEGER PRIMARY KEY,"
        f"  embedding FLOAT[{embed_dim}]"
        ")"
    )
    conn.commit()


def thread_connection(db_path: Path, *, embed_dim: int) -> sqlite3.Connection:
    """Connexion propre au thread courant, creee a la demande.

    Un objet sqlite3.Connection ne supporte pas d'etre utilise par deux threads en
    parallele. L'interface web (pool de threads) et la boucle de synchro en ont donc
    chacun la leur ; le fichier WAL gere la concurrence entre elles.
    """
    key = f"conn::{db_path}"
    conn = getattr(_local, key, None)
    if conn is None:
        conn = connect(db_path, embed_dim=embed_dim)
        setattr(_local, key, conn)
    return conn


# --------------------------------------------------------------------------- meta


def get_meta(conn: sqlite3.Connection, key: str) -> str | None:
    row = conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else None


def set_meta(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute(
        "INSERT INTO meta(key, value) VALUES(?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, value),
    )


def get_cursor(conn: sqlite3.Connection, name: str) -> str | None:
    row = conn.execute("SELECT cursor FROM sync_state WHERE name = ?", (name,)).fetchone()
    return row["cursor"] if row else None


def set_cursor(conn: sqlite3.Connection, name: str, cursor: str) -> None:
    conn.execute(
        "INSERT INTO sync_state(name, cursor, updated_at) VALUES(?, ?, ?) "
        "ON CONFLICT(name) DO UPDATE SET cursor = excluded.cursor, updated_at = excluded.updated_at",
        (name, cursor, int(time.time())),
    )
    conn.commit()


# --------------------------------------------------------------------- documents


def upsert_document(
    conn: sqlite3.Connection,
    *,
    source: str,
    external_id: str,
    title: str | None,
    author: str | None,
    ts: int | None,
    url: str | None,
    body: str,
    meta: dict[str, Any] | None = None,
    content_hash: str | None = None,
) -> tuple[int, bool]:
    """Insere ou met a jour un document.

    Retourne (doc_id, contenu_modifie). Si le hash est inchange, rien n'est reecrit
    et le document n'a pas besoin d'etre reindexe.
    """
    row = conn.execute(
        "SELECT id, content_hash FROM documents WHERE source = ? AND external_id = ?",
        (source, external_id),
    ).fetchone()

    payload = (
        title,
        author,
        ts,
        url,
        body,
        json.dumps(meta or {}, ensure_ascii=False),
        content_hash,
    )

    if row is None:
        cur = conn.execute(
            "INSERT INTO documents(source, external_id, title, author, ts, url, body, meta, content_hash)"
            " VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (source, external_id, *payload),
        )
        return int(cur.lastrowid), True

    doc_id = int(row["id"])
    if content_hash is not None and row["content_hash"] == content_hash:
        return doc_id, False

    # indexed_at repasse a NULL : le contenu a bouge, les chunks existants sont perimes.
    conn.execute(
        "UPDATE documents SET title = ?, author = ?, ts = ?, url = ?, body = ?, meta = ?,"
        " content_hash = ?, indexed_at = NULL WHERE id = ?",
        (*payload, doc_id),
    )
    return doc_id, True


def delete_document(conn: sqlite3.Connection, doc_id: int) -> None:
    clear_chunks(conn, doc_id)
    conn.execute("DELETE FROM documents WHERE id = ?", (doc_id,))


def get_document(conn: sqlite3.Connection, doc_id: int) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM documents WHERE id = ?", (doc_id,)).fetchone()


# ------------------------------------------------------------------------ chunks


def clear_chunks(conn: sqlite3.Connection, doc_id: int) -> None:
    """Supprime les chunks d'un document dans les trois tables (chunks, FTS, vecteurs)."""
    ids = [r["id"] for r in conn.execute("SELECT id FROM chunks WHERE doc_id = ?", (doc_id,))]
    if not ids:
        return
    marks = ",".join("?" * len(ids))
    # Les tables externes FTS5 et vec0 ignorent le ON DELETE CASCADE :
    # sans ce nettoyage explicite, l'index garderait des resultats fantomes.
    conn.execute(f"DELETE FROM chunks_fts WHERE rowid IN ({marks})", ids)
    conn.execute(f"DELETE FROM chunks_vec WHERE chunk_id IN ({marks})", ids)
    conn.execute("DELETE FROM chunks WHERE doc_id = ?", (doc_id,))


def add_chunks(
    conn: sqlite3.Connection,
    doc_id: int,
    texts: Sequence[str],
    embeddings: Sequence[Sequence[float]],
) -> None:
    if len(texts) != len(embeddings):
        raise ValueError("Il faut autant d'embeddings que de chunks")
    for ord_, (text, vec) in enumerate(zip(texts, embeddings)):
        cur = conn.execute(
            "INSERT INTO chunks(doc_id, ord, text) VALUES(?, ?, ?)", (doc_id, ord_, text)
        )
        chunk_id = int(cur.lastrowid)
        conn.execute("INSERT INTO chunks_fts(rowid, text) VALUES(?, ?)", (chunk_id, text))
        conn.execute(
            "INSERT INTO chunks_vec(chunk_id, embedding) VALUES(?, ?)",
            (chunk_id, sqlite_vec.serialize_float32(list(vec))),
        )
    conn.execute("UPDATE documents SET indexed_at = ? WHERE id = ?", (int(time.time()), doc_id))


def stats(conn: sqlite3.Connection) -> dict[str, Any]:
    out: dict[str, Any] = {"documents": {}, "chunks": 0}
    for row in conn.execute("SELECT source, COUNT(*) AS n FROM documents GROUP BY source"):
        out["documents"][row["source"]] = row["n"]
    out["chunks"] = conn.execute("SELECT COUNT(*) AS n FROM chunks").fetchone()["n"]
    out["triaged"] = conn.execute("SELECT COUNT(*) AS n FROM mail_triage").fetchone()["n"]
    return out


def iter_rows(conn: sqlite3.Connection, sql: str, params: Iterable[Any] = ()) -> list[sqlite3.Row]:
    return conn.execute(sql, tuple(params)).fetchall()
