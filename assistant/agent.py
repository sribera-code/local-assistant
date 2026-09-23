"""Boucle d'agent : le LLM appelle les outils locaux, puis repond en citant ses sources."""

from __future__ import annotations

import json
import re
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from .config import Config
from .llm import LLM
from .tools import TOOL_SCHEMAS, Toolbox

MAX_TOOL_ROUNDS = 4

SOURCE_LABELS = {
    "mail": "ses mails Gmail",
    "event": "son agenda Google",
    "file": "ses fichiers locaux",
}

SYSTEM_PROMPT = """\
Tu es l'assistant personnel de l'utilisateur, execute entierement sur sa machine.
Tu as acces, via des outils, a {sources}.
{absentes}
Regles :
- Pour toute question sur ses mails, son agenda ou ses fichiers, appelle un outil.
  Ne devine jamais un contenu que tu n'as pas lu.
- Appuie-toi uniquement sur ce que les outils renvoient. Si l'information n'y est pas,
  dis-le franchement et propose une autre recherche.
- Cite chaque information en mettant entre crochets le doc_id exact renvoye par l'outil,
  juste apres l'affirmation concernee : par exemple "la reunion est mardi [42]".
  N'invente jamais un doc_id et ne renumerote pas les sources.
- Un evenement d'agenda n'est une reunion que s'il a des participants. Les entrees
  venant d'un agenda d'abonnement (jours feries, fete des prenoms, calendrier sportif)
  ne concernent personne : ne les presente jamais comme des rendez-vous. Le champ
  "agenda" de chaque evenement dit d'ou il vient.
- Reponds en francais, de maniere concise et directe. Pas de preambule.
- Aujourd'hui, nous sommes le {today}.
"""


@dataclass
class AgentAnswer:
    text: str
    citations: list[dict[str, Any]] = field(default_factory=list)
    tool_trace: list[dict[str, Any]] = field(default_factory=list)


def _system_message(conn: sqlite3.Connection) -> dict[str, str]:
    """Construit le prompt systeme en fonction de ce qui est reellement indexe.

    Annoncer au modele un acces aux fichiers locaux alors qu'aucun dossier n'est
    configure l'amene a proposer des recherches qui ne peuvent rien donner.
    """
    present = {
        row["source"]
        for row in conn.execute(
            "SELECT source, COUNT(*) n FROM documents GROUP BY source HAVING n > 0"
        )
    }
    dispo = [label for key, label in SOURCE_LABELS.items() if key in present]
    manquantes = [label for key, label in SOURCE_LABELS.items() if key not in present]

    sources = " et ".join(filter(None, [", ".join(dispo[:-1]), dispo[-1]])) if dispo else "rien"
    absentes = ""
    if manquantes:
        absentes = (
            f"En revanche, {' ni '.join(manquantes)} ne sont PAS indexes pour l'instant :\n"
            "ne propose pas d'y chercher quoi que ce soit.\n"
        )

    today = datetime.now().astimezone().strftime("%A %d %B %Y, %H:%M")
    return {
        "role": "system",
        "content": SYSTEM_PROMPT.format(today=today, sources=sources, absentes=absentes),
    }


def ask(
    conn: sqlite3.Connection,
    llm: LLM,
    cfg: Config,
    question: str,
    *,
    history: list[dict[str, str]] | None = None,
    think: bool = False,
    focus_doc: int | None = None,
) -> AgentAnswer:
    """Pose une question a l'assistant, outils compris.

    `focus_doc` : document sur lequel porte la question (bouton "Que dois-je en
    faire ?" d'un mail, "Resumer" d'un fichier). Il est lu d'office, comme si le
    modele avait appele `lire_document` : un 4B ne pense pas toujours a le faire et
    repond alors qu'il n'a pas acces au mail.
    """
    toolbox = Toolbox(conn, llm, cfg)
    messages: list[dict[str, Any]] = [_system_message(conn)]
    messages.extend(history or [])
    messages.append({"role": "user", "content": question})

    trace: list[dict[str, Any]] = []
    if focus_doc is not None:
        args = {"doc_id": int(focus_doc)}
        messages.append(
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [{"function": {"name": "lire_document", "arguments": args}}],
            }
        )
        messages.append(
            {
                "role": "tool",
                "tool_name": "lire_document",
                "content": json.dumps(toolbox.lire_document(**args), ensure_ascii=False),
            }
        )
        trace.append({"outil": "lire_document", "arguments": args})

    for _ in range(MAX_TOOL_ROUNDS):
        reply = llm.chat(messages, tools=TOOL_SCHEMAS, think=think)
        if not reply.wants_tools:
            return AgentAnswer(
                text=reply.content.strip(),
                citations=_citations(toolbox, reply.content),
                tool_trace=trace,
            )

        messages.append(
            {
                "role": "assistant",
                "content": reply.content,
                "tool_calls": reply.tool_calls,
            }
        )
        for call in reply.tool_calls:
            fn = call.get("function") or {}
            name = fn.get("name", "")
            args = fn.get("arguments") or {}
            if isinstance(args, str):  # certains modeles renvoient les arguments en texte
                try:
                    args = json.loads(args)
                except json.JSONDecodeError:
                    args = {}
            result = toolbox.call(name, args)
            trace.append({"outil": name, "arguments": args})
            messages.append(
                {
                    "role": "tool",
                    "tool_name": name,
                    "content": json.dumps(result, ensure_ascii=False)[:12_000],
                }
            )

    # Plafond d'appels atteint : on force une reponse a partir de ce qui a ete collecte.
    messages.append(
        {
            "role": "user",
            "content": "Reponds maintenant avec les informations deja recuperees, sans nouvel outil.",
        }
    )
    final = llm.chat(messages, think=False)
    return AgentAnswer(
        text=final.content.strip(),
        citations=_citations(toolbox, final.content),
        tool_trace=trace,
    )


_CITE_RE = re.compile(r"\[(\d{1,6})\]")


def _citations(toolbox: Toolbox, answer: str) -> list[dict[str, Any]]:
    """Documents a afficher sous la reponse.

    On privilegie ceux que le modele a reellement cites entre crochets ; s'il n'en a
    cite aucun, on retombe sur l'ensemble des documents que les outils lui ont montres.
    """
    cited = [int(m) for m in _CITE_RE.findall(answer or "")]
    ordered = [toolbox.seen_docs[doc_id] for doc_id in dict.fromkeys(cited) if doc_id in toolbox.seen_docs]
    return ordered or list(toolbox.seen_docs.values())


def save_message(
    conn: sqlite3.Connection,
    role: str,
    content: str,
    citations: list[dict[str, Any]] | None = None,
    *,
    conversation_id: int | None = None,
) -> None:
    now = int(datetime.now().timestamp())
    conn.execute(
        "INSERT INTO messages(role, content, citations, created_at, conversation_id)"
        " VALUES(?, ?, ?, ?, ?)",
        (role, content, json.dumps(citations or [], ensure_ascii=False), now, conversation_id),
    )
    if conversation_id is not None:
        conn.execute("UPDATE conversations SET updated_at = ? WHERE id = ?", (now, conversation_id))
    conn.commit()


def load_history(
    conn: sqlite3.Connection, limit: int = 12, *, conversation_id: int | None = None
) -> list[dict[str, Any]]:
    rows = conn.execute(
        "SELECT role, content, citations, created_at FROM messages"
        " WHERE conversation_id IS ? ORDER BY id DESC LIMIT ?",
        (conversation_id, limit),
    ).fetchall()
    return [
        {
            "role": r["role"],
            "content": r["content"],
            "citations": json.loads(r["citations"] or "[]"),
            "created_at": r["created_at"],
        }
        for r in reversed(rows)
    ]


# ------------------------------------------------------------- conversations

NEW_CONVERSATION_TITLE = "Nouvelle conversation"


def list_conversations(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    """Conversations dans l'ordre des onglets (creation), avec leur nombre de messages."""
    return [
        dict(r)
        for r in conn.execute(
            "SELECT c.id, c.title, c.created_at, c.updated_at,"
            "       (SELECT COUNT(*) FROM messages m WHERE m.conversation_id = c.id) n"
            " FROM conversations c ORDER BY c.created_at, c.id"
        )
    ]


LAST_CONVERSATION_KEY = "last_conversation_id"


def create_conversation(conn: sqlite3.Connection, title: str = NEW_CONVERSATION_TITLE) -> int:
    """Nouvelle conversation, avec un numero JAMAIS reattribue.

    Sans AUTOINCREMENT, SQLite redonne le numero de la derniere conversation supprimee :
    une reponse encore en route pour l'ancienne atterrirait dans la nouvelle, et
    l'interface la prendrait pour celle deja affichee.
    """
    now = int(datetime.now().timestamp())
    dernier = conn.execute("SELECT COALESCE(MAX(id), 0) m FROM conversations").fetchone()["m"]
    memo = conn.execute("SELECT value FROM meta WHERE key = ?", (LAST_CONVERSATION_KEY,)).fetchone()
    nouveau = max(int(dernier), int(memo["value"]) if memo else 0) + 1
    conn.execute(
        "INSERT INTO conversations(id, title, created_at, updated_at) VALUES(?, ?, ?, ?)",
        (nouveau, title, now, now),
    )
    conn.execute(
        "INSERT INTO meta(key, value) VALUES(?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (LAST_CONVERSATION_KEY, str(nouveau)),
    )
    conn.commit()
    return nouveau


def delete_conversation(conn: sqlite3.Connection, conversation_id: int) -> None:
    # Le plus grand numero deja attribue est retenu AVANT la suppression : c'est lui
    # qui empeche create_conversation de le redonner (voir sa docstring).
    conn.execute(
        "INSERT INTO meta(key, value) SELECT ?, MAX(id) FROM conversations WHERE true"
        " ON CONFLICT(key) DO UPDATE SET value = MAX(CAST(value AS INTEGER), CAST(excluded.value AS INTEGER))",
        (LAST_CONVERSATION_KEY,),
    )
    conn.execute("DELETE FROM messages WHERE conversation_id = ?", (conversation_id,))
    conn.execute("DELETE FROM conversations WHERE id = ?", (conversation_id,))
    conn.commit()


def title_from_question(question: str, max_chars: int = 40) -> str:
    """Titre d'onglet : le debut de la premiere question."""
    titre = " ".join(question.split())
    return titre if len(titre) <= max_chars else titre[: max_chars - 1].rstrip() + "…"
