"""Recuperation des mails Gmail (lecture seule) vers la base locale."""

from __future__ import annotations

import base64
import hashlib
import html
import json
import re
import sqlite3
import time
from typing import Any, Iterator

from .. import db
from ..config import Config
from .google_auth import execute_with_retry, gmail_service

CURSOR = "gmail_last_internal_ms"
ADDRESS_META_KEY = "gmail_address"
QUERY_META_KEY = "gmail_query"

# Dossier d'un mail, deduit de ses labels Gmail. Gmail n'a pas de dossiers : un mail
# EST dans la boite de reception tant qu'il porte le label INBOX, et l'archiver
# revient a le lui retirer. L'ordre de ce test compte : un mail qu'on s'est envoye a
# soi-meme porte SENT *et* INBOX, et sa place est bien dans la boite de reception.
_FOLDER_BY_LABEL = (("TRASH", "trash"), ("SPAM", "spam"), ("DRAFT", "draft"),
                    ("INBOX", "inbox"), ("SENT", "sent"))


def folder_of(labels: list[str] | None) -> str:
    """'inbox', 'sent', 'draft', 'spam', 'trash' ou 'archive'."""
    presents = set(labels or [])
    for label, dossier in _FOLDER_BY_LABEL:
        if label in presents:
            return dossier
    return "archive"

# Pause entre deux `messages.get`. Google autorise 250 unites de quota par seconde
# et par utilisateur, un `get` en coute 5 : on se cale bien en dessous, ce qui evite
# la plupart des erreurs de debit sans allonger sensiblement une synchro.
PAUSE_BETWEEN_GETS = 0.05

_TAG_RE = re.compile(r"<(script|style)[^>]*>.*?</\1>|<[^>]+>", re.S | re.I)
_BLANK_RE = re.compile(r"\n{3,}")
# Lignes de citation d'un mail precedent : on les coupe pour ne pas reindexer
# dix fois le meme fil de discussion.
_QUOTE_RE = re.compile(
    r"\n(?:>|Le .{0,60} a ecrit ?:|On .{0,60} wrote:|-{2,} ?Message d'origine|"
    r"-{2,} ?Original Message|De ?: .{0,80}\nEnvoye ?:)",
    re.I,
)


# Balises de fin de bloc : on les remplace par un saut de ligne AVANT de retirer les
# balises, sinon tout un mail HTML devient un seul paragraphe illisible.
_BLOCK_RE = re.compile(
    r"<\s*(?:br|/p|/div|/tr|/li|/h[1-6]|/table|/blockquote|/ul|/ol)\b[^>]*>", re.I
)
_LI_RE = re.compile(r"<\s*li\b[^>]*>", re.I)


# Caracteres invisibles dont les mails HTML se servent pour la mise en page ou pour
# remplir l'apercu de la boite de reception (&zwnj;, &#847;, trait d'union conditionnel...).
_INVISIBLE_RE = re.compile("[\u00ad\u034f\u061c\u115f\u1160\u17b4\u17b5\u180e\u200b-\u200f\u2060-\u2064\ufeff]")
# Espaces "exotiques" (insecable, fine, cadratin...) : de simples espaces pour nous.
_SPACES_RE = re.compile("[ \t\f\v\u00a0\u1680\u2000-\u200a\u202f\u205f\u3000]+")


def normalize_text(text: str) -> str:
    """Texte de mail propre : une ligne vide au plus entre deux paragraphes.

    Les parties text/plain arrivent avec des fins de ligne Windows et des espaces
    insecables : une ligne " \\r" ou "\\u00a0" n'est pas vide pour une regex naive,
    et un mail peut alors afficher des dizaines de lignes blanches d'affilee.
    """
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = _INVISIBLE_RE.sub("", text)
    text = _SPACES_RE.sub(" ", text)
    text = "\n".join(line.strip() for line in text.split("\n"))
    return _BLANK_RE.sub("\n\n", text).strip()


_HTML_START_RE = re.compile(r"\s*<(?:!doctype|html|head|body|meta|table|div)\b", re.I)
_HTML_TAG_RE = re.compile(r"</?(?:html|body|table|tr|td|div|span|p|br|a|img|font)\b[^>]*>", re.I)


def looks_like_html(text: str) -> bool:
    """Vrai si un texte "brut" est en realite du HTML.

    Certains expediteurs (boutiques, newsletters) mettent le code HTML dans la
    partie text/plain : affiche tel quel, le mail n'est qu'une page de balises.
    """
    return bool(_HTML_START_RE.match(text)) or len(_HTML_TAG_RE.findall(text)) >= 8


def html_to_text(raw: str) -> str:
    # Commentaires (dont les blocs conditionnels Outlook <!--[if mso]>...) et <head>
    # (titre, meta, feuilles de style) ne contiennent rien de lisible.
    text = re.sub(r"<!--.*?-->", " ", raw, flags=re.S)
    text = re.sub(r"<head\b.*?</head>", " ", text, flags=re.S | re.I)
    text = re.sub(r"<(script|style)[^>]*>.*?</\1>", " ", text, flags=re.S | re.I)
    text = _BLOCK_RE.sub("\n", text)
    text = _LI_RE.sub("\n- ", text)
    text = _TAG_RE.sub(" ", text)
    text = html.unescape(text)
    return normalize_text(text)


def clean_body(text: str, *, max_chars: int = 20_000) -> str:
    text = normalize_text(text)
    cut = _QUOTE_RE.split(text, maxsplit=1)[0].strip()
    # Un mail dont il ne reste presque rien apres decoupe : on garde l'original.
    if len(cut) < 80 < len(text):
        cut = text
    return cut[:max_chars]


def _decode(data: str) -> str:
    return base64.urlsafe_b64decode(data.encode("ascii")).decode("utf-8", errors="replace")


def _walk_parts(part: dict[str, Any]) -> Iterator[dict[str, Any]]:
    yield part
    for sub in part.get("parts") or []:
        yield from _walk_parts(sub)


def extract_body(payload: dict[str, Any]) -> tuple[str, list[str]]:
    """Retourne (corps en texte brut, noms des pieces jointes)."""
    plain: list[str] = []
    html_parts: list[str] = []
    attachments: list[str] = []

    for part in _walk_parts(payload):
        mime = part.get("mimeType", "")
        body = part.get("body") or {}
        filename = part.get("filename")
        if filename:
            attachments.append(filename)
            continue
        data = body.get("data")
        if not data:
            continue
        if mime == "text/plain":
            plain.append(_decode(data))
        elif mime == "text/html":
            html_parts.append(_decode(data))

    text = "\n".join(plain)
    if not text.strip() or looks_like_html(text):
        text = "\n".join(html_parts) or text
    if looks_like_html(text):
        text = html_to_text(text)
    return clean_body(text), attachments


# Images integrees au mail (logos, bannieres en piece jointe "cid:") : au-dela de ce
# volume, on laisse les suivantes vides plutot que de gonfler la base.
MAX_INLINE_BYTES = 3_000_000


def message_html(service, msg: dict[str, Any]) -> str:
    """Version HTML d'un mail, images integrees comprises ('' s'il n'en a pas).

    Les images "cid:" sont des parties du mail lui-meme : on les inclut en data:
    pour qu'elles s'affichent sans rien demander a l'exterieur. Elles sont parfois
    stockees a part chez Gmail (attachmentId), d'ou l'appel a l'API.
    """
    payload = msg.get("payload") or {}
    html_parts: list[str] = []
    plain: list[str] = []
    inline: dict[str, dict[str, Any]] = {}
    for part in _walk_parts(payload):
        mime = part.get("mimeType", "")
        body = part.get("body") or {}
        cid = _headers(part).get("content-id", "").strip("<> ")
        if cid and mime.startswith("image/"):
            inline[cid] = part
        elif not part.get("filename") and body.get("data"):
            if mime == "text/html":
                html_parts.append(_decode(body["data"]))
            elif mime == "text/plain":
                plain.append(_decode(body["data"]))

    html_text = "\n".join(html_parts)
    if not html_text and looks_like_html("\n".join(plain)):
        html_text = "\n".join(plain)
    if not html_text:
        return ""

    budget = MAX_INLINE_BYTES
    for cid, part in inline.items():
        if f"cid:{cid}" not in html_text:
            continue
        body = part.get("body") or {}
        data = body.get("data")
        if not data and body.get("attachmentId") and (body.get("size") or 0) <= budget:
            data = execute_with_retry(
                service.users().messages().attachments()
                .get(userId="me", messageId=msg["id"], id=body["attachmentId"])
            ).get("data")
        if not data or len(data) > budget:
            continue
        budget -= len(data)
        brut = base64.urlsafe_b64decode(data.encode("ascii"))
        uri = f"data:{part['mimeType']};base64,{base64.b64encode(brut).decode('ascii')}"
        html_text = html_text.replace(f"cid:{cid}", uri)
    return html_text


def store_html(conn: sqlite3.Connection, doc_id: int, html_text: str) -> None:
    conn.execute(
        "INSERT INTO mail_html(doc_id, html) VALUES(?, ?)"
        " ON CONFLICT(doc_id) DO UPDATE SET html = excluded.html",
        (doc_id, html_text),
    )


def fetch_html(conn: sqlite3.Connection, cfg: Config, doc_id: int) -> str:
    """Recupere a la demande le HTML d'un mail indexe avant que la synchro le garde."""
    row = conn.execute(
        "SELECT external_id FROM documents WHERE id = ? AND source = 'mail'", (doc_id,)
    ).fetchone()
    if row is None:
        return ""
    service = gmail_service(cfg.paths.client_secret_path, cfg.paths.token_path)
    msg = execute_with_retry(
        service.users().messages().get(userId="me", id=row["external_id"], format="full")
    )
    html_text = message_html(service, msg)
    store_html(conn, doc_id, html_text)
    conn.commit()
    return html_text


def _headers(payload: dict[str, Any]) -> dict[str, str]:
    return {h["name"].lower(): h.get("value", "") for h in payload.get("headers") or []}


def message_to_document(msg: dict[str, Any]) -> dict[str, Any]:
    payload = msg.get("payload") or {}
    head = _headers(payload)
    body, attachments = extract_body(payload)
    labels = msg.get("labelIds") or []
    subject = head.get("subject") or "(sans objet)"
    sender = head.get("from") or ""
    internal_ms = int(msg.get("internalDate") or 0)

    # Le texte indexe porte l'entete : une recherche "mail de Marie" doit pouvoir matcher.
    indexed = (
        f"Mail de : {sender}\n"
        f"A : {head.get('to', '')}\n"
        f"Objet : {subject}\n"
        f"Labels : {', '.join(labels)}\n"
        + (f"Pieces jointes : {', '.join(attachments)}\n" if attachments else "")
        + f"\n{body}"
    )

    return {
        "source": "mail",
        "external_id": msg["id"],
        "title": subject,
        "author": sender,
        "ts": internal_ms // 1000,
        "url": f"https://mail.google.com/mail/u/0/#all/{msg['id']}",
        "body": indexed,
        "meta": {
            "thread_id": msg.get("threadId"),
            "to": head.get("to", ""),
            "cc": head.get("cc", ""),
            "labels": labels,
            "folder": folder_of(labels),
            "unread": "UNREAD" in labels,
            "attachments": attachments,
            "snippet": html.unescape(msg.get("snippet") or ""),
            "internal_ms": internal_ms,
        },
        "content_hash": hashlib.sha256(
            (indexed + "|" + ",".join(sorted(labels))).encode("utf-8")
        ).hexdigest(),
    }


def _list_ids(service, query: str) -> set[str]:
    """Identifiants de tous les mails correspondant a la requete (sans leur contenu)."""
    ids: set[str] = set()
    page_token: str | None = None
    while True:
        resp = execute_with_retry(
            service.users()
            .messages()
            .list(
                userId="me",
                q=query,
                pageToken=page_token,
                maxResults=500,
                includeSpamTrash=False,
            )
        )
        ids.update(m["id"] for m in resp.get("messages", []))
        page_token = resp.get("nextPageToken")
        if not page_token:
            return ids


def reconcile(conn: sqlite3.Connection, service, cfg: Config) -> tuple[int, int]:
    """Aligne la base sur l'etat actuel de Gmail. Retourne (retires, mis_a_jour).

    La synchro incrementale ne demande que les NOUVEAUX mails : un mail supprime, lu
    ou archive depuis resterait sinon indexe tel quel pour toujours. Trois listes
    d'identifiants suffisent (pas de contenu telecharge), donc c'est peu couteux.

    L'archivage compte autant que le reste depuis que l'interface separe les dossiers :
    sans ce rattrapage, un mail archive il y a un mois resterait affiche dans la boite
    de reception, puisque rien ne le fait redescendre par la synchro incrementale.
    """
    # Les listes sont completes AVANT toute suppression : si l'API echoue en cours
    # de route, l'exception remonte et rien n'est efface a tort.
    presents = _list_ids(service, cfg.gmail.query)
    non_lus = _list_ids(service, f"({cfg.gmail.query}) is:unread")
    # Requete deja limitee a la boite de reception : inutile de la redemander.
    # Le test exclut "-in:inbox", qui dit exactement le contraire.
    inbox_only = re.search(r"(?<![-\w])in:inbox\b", cfg.gmail.query) is not None
    en_boite = presents if inbox_only else _list_ids(service, f"({cfg.gmail.query}) in:inbox")

    retires = maj = 0
    for row in conn.execute(
        "SELECT id, external_id, meta FROM documents WHERE source = 'mail'"
    ).fetchall():
        if row["external_id"] not in presents:
            db.delete_document(conn, row["id"])  # le tri associe part en cascade
            retires += 1
            continue
        meta = json.loads(row["meta"] or "{}")
        non_lu = row["external_id"] in non_lus
        labels = [lab for lab in (meta.get("labels") or []) if lab != "INBOX"]
        if row["external_id"] in en_boite:
            labels.append("INBOX")
        dossier = folder_of(labels)
        if (meta.get("unread"), meta.get("folder")) != (non_lu, dossier):
            meta["unread"] = non_lu
            meta["labels"] = labels
            meta["folder"] = dossier
            conn.execute(
                "UPDATE documents SET meta = ? WHERE id = ?",
                (json.dumps(meta, ensure_ascii=False), row["id"]),
            )
            maj += 1
    conn.commit()
    return retires, maj


def sync(conn: sqlite3.Connection, cfg: Config, *, full: bool = False) -> list[int]:
    """Synchronise les mails. Retourne les doc_id crees ou modifies."""
    service = gmail_service(cfg.paths.client_secret_path, cfg.paths.token_path)

    # Requete elargie depuis la derniere synchro (ajout des archives, des envoyes) :
    # le curseur ne ramenerait que les mails posterieurs, et tout l'historique
    # nouvellement couvert resterait invisible. On repart du debut, une seule fois.
    # Marqueur absent = base anterieure a son introduction : c'est le cas qui a le
    # PLUS besoin du rattrapage, puisqu'elle a ete remplie avec une autre requete.
    # Sur une base vierge, la synchro est de toute facon complete : forcer ne coute rien.
    if db.get_meta(conn, QUERY_META_KEY) != cfg.gmail.query:
        full = True

    query = cfg.gmail.query
    since_ms = None if full else db.get_cursor(conn, CURSOR)
    if since_ms:
        # `after:` de Gmail accepte un timestamp unix en secondes. On recule d'une
        # minute : l'horodatage Gmail et l'ordre d'arrivee ne sont pas strictement alignes.
        query = f"{query} after:{max(0, int(since_ms) // 1000 - 60)}"

    limit = None if since_ms else cfg.gmail.initial_backfill
    ids: list[str] = []
    page_token: str | None = None
    while True:
        resp = execute_with_retry(
            service.users()
            .messages()
            .list(
                userId="me",
                q=query,
                pageToken=page_token,
                maxResults=100,
                includeSpamTrash=False,
            )
        )
        ids.extend(m["id"] for m in resp.get("messages", []))
        page_token = resp.get("nextPageToken")
        if not page_token or (limit and len(ids) >= limit):
            break
    if limit:
        ids = ids[:limit]

    changed: list[int] = []
    newest_ms = int(since_ms) if since_ms else 0
    complete = False

    try:
        for msg_id in ids:
            msg = execute_with_retry(
                service.users().messages().get(userId="me", id=msg_id, format="full")
            )
            doc = message_to_document(msg)
            newest_ms = max(newest_ms, doc["meta"]["internal_ms"])
            doc_id, modified = db.upsert_document(conn, **doc)
            store_html(conn, doc_id, message_html(service, msg))
            # Commit a chaque mail : le verrou d'ecriture ne couvre jamais une requete
            # reseau, et une coupure ne fait perdre au plus qu'un mail.
            conn.commit()
            if modified:
                changed.append(doc_id)
            time.sleep(PAUSE_BETWEEN_GETS)
        complete = True
    finally:
        # On garde toujours ce qui a ete recupere. En revanche le curseur n'avance
        # QUE si la liste a ete traitee en entier : Gmail renvoie les mails du plus
        # recent au plus ancien, donc l'avancer apres un echec partiel ferait sauter
        # definitivement tous ceux qui restaient a lire. Sans avancee de curseur, la
        # prochaine synchro les redemande (l'upsert est idempotent).
        conn.commit()
        if complete and newest_ms:
            db.set_cursor(conn, CURSOR, str(newest_ms))
        if complete:
            # Memorise APRES coup : une passe interrompue doit etre rejouee en entier
            # au prochain tour, pas reprise a partir du curseur.
            db.set_meta(conn, QUERY_META_KEY, cfg.gmail.query)
            conn.commit()

    reconcile(conn, service, cfg)
    if not db.get_meta(conn, ADDRESS_META_KEY):
        # Adresse du compte : sert a ouvrir un mail dans le BON compte Gmail quand
        # plusieurs sont connectes dans le navigateur (/u/0/ vise le premier).
        profil = execute_with_retry(service.users().getProfile(userId="me"))
        db.set_meta(conn, ADDRESS_META_KEY, profil.get("emailAddress") or "")
        conn.commit()
    return changed
