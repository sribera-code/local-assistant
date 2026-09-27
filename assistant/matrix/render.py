"""Mise en forme d'une reponse de l'assistant pour Element.

Meme principe que renderMarkdown dans web/static/app.js : tout le texte est echappe
d'abord, puis seul un jeu ferme de balises est reintroduit. Rien de ce que produit le
modele - ni le contenu de mail qu'il recopie - ne peut injecter de HTML.
"""

from __future__ import annotations

import html
import re
from typing import Any

from .. import triage

# Seuls ces schemas deviennent des liens (voir SCHEMAS_SURS cote web).
_SAFE_URL = re.compile(r"^https?://", re.IGNORECASE)
_CITE_RE = re.compile(r"\[(\d{1,6})\]")
SOURCE_LABELS = {"mail": "mail", "event": "agenda", "file": "fichier"}


def _link(citation: dict[str, Any] | None, doc_id: str) -> str:
    # Sur le telephone, un fichier local ne s'ouvre pas : seuls les liens Gmail et
    # Agenda sont cliquables.
    url = (citation or {}).get("url") or ""
    if citation and citation.get("source") != "file" and _SAFE_URL.match(url):
        return f'<a href="{html.escape(url)}">[{doc_id}]</a>'
    return f"[{doc_id}]"


def _inline(text: str, citations: dict[str, dict[str, Any]]) -> str:
    text = re.sub(r"`([^`]+)`", r"<code>\1</code>", text)
    text = re.sub(r"\*\*([^*]+)\*\*", r"<strong>\1</strong>", text)
    text = re.sub(r"(^|[\s(])\*([^*\n]+)\*", r"\1<em>\2</em>", text)
    return _CITE_RE.sub(lambda m: _link(citations.get(m.group(1)), m.group(1)), text)


def to_html(text: str, citations: list[dict[str, Any]]) -> str:
    by_id = {str(c["doc_id"]): c for c in citations}
    out: list[str] = []
    liste: str | None = None

    def close() -> None:
        nonlocal liste
        if liste:
            out.append(f"</{liste}>")
            liste = None

    for raw in html.escape(text, quote=False).split("\n"):
        line = raw.strip()
        if not line:
            close()
        elif m := re.match(r"^#{1,6}\s+(.*)$", line):
            close()
            out.append(f"<h4>{_inline(m[1], by_id)}</h4>")
        elif m := re.match(r"^[-*+]\s+(.*)$", line):
            if liste != "ul":
                close()
                out.append("<ul>")
                liste = "ul"
            out.append(f"<li>{_inline(m[1], by_id)}</li>")
        elif m := re.match(r"^\d+[.)]\s+(.*)$", line):
            if liste != "ol":
                close()
                out.append("<ol>")
                liste = "ol"
            out.append(f"<li>{_inline(m[1], by_id)}</li>")
        else:
            close()
            out.append(f"<p>{_inline(line, by_id)}</p>")
    close()

    sources = _sources(citations)
    if sources:
        items = "".join(
            f"<li>{_link(c, str(c['doc_id']))} {html.escape(label)}</li>" for c, label in sources
        )
        out.append(f"<p><em>Sources</em></p><ul>{items}</ul>")
    return "".join(out)


def to_text(text: str, citations: list[dict[str, Any]]) -> str:
    """Version texte (champ `body`), lue par les clients sans HTML et les notifications."""
    sources = _sources(citations)
    if not sources:
        return text
    lines = [f"[{c['doc_id']}] {label}" for c, label in sources]
    return text + "\n\nSources :\n" + "\n".join(lines)


# ------------------------------------------------------------ resumes de mails
# Les emojis tiennent le role des icones de la liste de mails de l'interface web.

ACTION_EMOJIS = {
    "repondre": "↩️",
    "payer": "💳",
    "confirmer": "📅",
    "document": "📝",
    "verifier": "🛡️",
    "traiter": "⚠️",
    "lire": "👀",
    "rien": "✅",
}
URGENCY_EMOJIS = {5: "🔴", 4: "🟠", 3: "🟡"}
CATEGORY_LABELS = {"publicite": "publicité", "securite": "sécurité"}


def _sender(author: str | None) -> str:
    """"Jean Dupont <jean@x.fr>" -> "Jean Dupont" ; l'adresse seule a defaut de nom."""
    author = (author or "").strip()
    name = author.split("<", 1)[0].strip().strip('"')
    return name or author.strip("<>") or "expéditeur inconnu"


def _action(mail: dict[str, Any]) -> tuple[str, str]:
    """Emoji et phrase de l'action attendue, meme logique que le badge de la liste web."""
    kind = mail.get("action_type") or triage.fallback_action_type(
        mail.get("category"), mail.get("urgency")
    )
    phrase = (mail.get("action") or "").strip()
    # Le modele ecrit "rien" quand il n'y a rien a faire : le libelle le dit mieux.
    if not phrase or phrase.lower().rstrip(".") == "rien":
        phrase = triage.ACTION_LABELS.get(kind, "")
    return ACTION_EMOJIS.get(kind, "⚠️"), phrase


def _mail_link(mail: dict[str, Any], label: str) -> str:
    url = mail.get("url") or ""
    if _SAFE_URL.match(url):
        return f'<a href="{html.escape(url)}">{html.escape(label)}</a>'
    return html.escape(label)


def _mark(mail: dict[str, Any]) -> str:
    return URGENCY_EMOJIS.get(int(mail.get("urgency") or 1), "📩")


def _statut(mail: dict[str, Any]) -> str:
    urgency = int(mail.get("urgency") or 1)
    category = mail.get("category") or "autre"
    return (
        f"urgence {urgency}/5 ({triage.URGENCY_LABELS.get(urgency, '?')})"
        f" · {CATEGORY_LABELS.get(category, category)}"
    )


def mail_notice(mail: dict[str, Any]) -> tuple[str, str]:
    """Resume d'un mail trie : (texte, HTML)."""
    titre = mail.get("title") or "(sans objet)"
    emoji, action = _action(mail)
    mark, statut = _mark(mail), _statut(mail)
    sender = _sender(mail.get("author"))
    summary = (mail.get("summary") or "").strip()

    lines = [f"{mark} {titre}", f"De : {sender}", summary, f"{emoji} {action}", statut]
    text = "\n".join(line for line in [*lines, mail.get("url") or ""] if line)

    head = f"<strong>{_mail_link(mail, titre)}</strong><br>De : {html.escape(sender)}"
    parts = [f"<p>{mark} {head}</p>"]
    if summary:
        parts.append(f"<p>{html.escape(summary)}</p>")
    parts.append(f"<p>{emoji} <strong>{html.escape(action)}</strong></p>")
    parts.append(f"<p><em>{html.escape(statut)}</em></p>")
    return text, "".join(parts)


def mail_digest(mails: list[dict[str, Any]]) -> tuple[str, str]:
    """Plusieurs mails arrives d'un coup (reveil du PC) : un seul message, une ligne chacun."""
    head = f"📬 {len(mails)} nouveaux mails"
    lines, items = [], []
    for mail in mails:
        titre = mail.get("title") or "(sans objet)"
        emoji, action = _action(mail)
        sender = _sender(mail.get("author"))
        lines.append(f"{_mark(mail)} {titre} — {sender} : {emoji} {action}")
        items.append(
            f"<li>{_mark(mail)} <strong>{_mail_link(mail, titre)}</strong>"
            f" — {html.escape(sender)} : {emoji} {html.escape(action)}</li>"
        )
    return head + "\n" + "\n".join(lines), f"<p><strong>{head}</strong></p><ul>{''.join(items)}</ul>"


def notice_markdown(mails: list[dict[str, Any]]) -> str:
    """Le meme resume pour le chat de l'interface web (markdown minimal de app.js).

    Pas de lien ici : le mail est joint comme source, et la page en fait un lien qui
    l'ouvre dans l'onglet Mails.
    """
    if len(mails) == 1:
        mail = mails[0]
        emoji, action = _action(mail)
        lines = [
            f"{_mark(mail)} **{mail.get('title') or '(sans objet)'}** · {_sender(mail.get('author'))}",
            (mail.get("summary") or "").strip(),
            f"{emoji} {action}",
            f"*{_statut(mail)}*",
        ]
        return "\n".join(line for line in lines if line)
    items = []
    for mail in mails:
        emoji, action = _action(mail)
        items.append(
            f"- {_mark(mail)} **{mail.get('title') or '(sans objet)'}**"
            f" · {_sender(mail.get('author'))} : {emoji} {action}"
        )
    return "\n".join([f"📬 **{len(mails)} nouveaux mails**", *items])


def _sources(citations: list[dict[str, Any]]) -> list[tuple[dict[str, Any], str]]:
    out = []
    for c in citations:
        if not c.get("titre"):
            continue
        parts = [SOURCE_LABELS.get(c.get("source", ""), c.get("source", "")), c["titre"], c.get("date")]
        out.append((c, " · ".join(p for p in parts if p)))
    return out
