"""Application FastAPI : trois onglets (Mails, Agenda, Fichiers) et un chat lateral.

Ecoute par defaut sur 127.0.0.1 : rien n'est expose sur le reseau.
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
import subprocess
import sys
from contextlib import asynccontextmanager
from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlsplit

from fastapi import FastAPI, Form, Header, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from markupsafe import Markup, escape

from .. import agent, db, pipeline, search, triage
from ..config import Config, get_config
from ..ingest import gcal, gmail
from ..ingest.gmail import normalize_text
from ..llm import LLM, OllamaError

HERE = Path(__file__).parent
templates = Jinja2Templates(directory=str(HERE / "templates"))


def _static(name: str) -> str:
    """URL d'un fichier statique, suffixee de sa date de modification.

    Sans ce suffixe, le navigateur peut garder l'ancien app.js ou app.css en cache
    apres une mise a jour : boutons sans effet, styles d'une version passee.
    """
    return f"/static/{name}?v={int((HERE / 'static' / name).stat().st_mtime)}"


templates.env.globals["static"] = _static

TABS = [("mails", "Mails", "/"), ("agenda", "Agenda", "/agenda"), ("fichiers", "Fichiers", "/fichiers")]

URGENCY_LABELS = {5: "critique", 4: "important", 3: "a traiter", 2: "a lire", 1: "rien a faire"}
URGENT_THRESHOLD = 4

# Onglets de la liste de mails. Gmail n'ayant pas de dossiers, chacun correspond a une
# condition sur le label du mail (voir gmail.folder_of) ; "all" ne filtre rien.
# Les brouillons et le spam ne sont volontairement pas exposes : ils n'apparaissent
# que dans "Tous", et seulement si la requete de synchro les rapatrie.
MAIL_FOLDERS = [
    ("inbox", "Réception"),
    ("archive", "Archivés"),
    ("sent", "Envoyés"),
    ("all", "Tous"),
]
DEFAULT_FOLDER = "inbox"

# Icone et libelle de l'action conseillee par le tri (triage.ACTION_TYPES).
ACTION_BADGES = {
    "repondre": ("i-reply", "Répondre"),
    "payer": ("i-card", "Payer"),
    "confirmer": ("i-cal-check", "Confirmer"),
    "document": ("i-pen", "Fournir un document"),
    "verifier": ("i-shield", "Vérifier le compte"),
    "traiter": ("i-alert", "À traiter"),
    "lire": ("i-eye", "À lire"),
    "rien": ("i-check", "Rien à faire"),
}

JOURS = ["lun.", "mar.", "mer.", "jeu.", "ven.", "sam.", "dim."]
MOIS = [
    "janvier", "février", "mars", "avril", "mai", "juin", "juillet",
    "août", "septembre", "octobre", "novembre", "décembre",
]
MOIS_COURTS = [
    "janv.", "févr.", "mars", "avr.", "mai", "juin", "juil.",
    "août", "sept.", "oct.", "nov.", "déc.",
]
# Grille horaire de l'agenda : hauteur d'une heure (px) et plage affichee par defaut,
# elargie automatiquement si un evenement de la semaine en deborde.
HEURE_PX = 52
PLAGE_DEFAUT = (7, 21)
# Seules des couleurs hexadecimales passent dans un attribut style : la valeur vient
# de l'API Google, mais on ne l'injecte pas dans du CSS sans l'avoir verifiee.
_HEX_COLOR = re.compile(r"^#[0-9a-fA-F]{3,8}$")


class State:
    cfg: Config
    llm: LLM
    syncer: pipeline.BackgroundSync | None = None


state = State()


@asynccontextmanager
async def lifespan(app: FastAPI):
    state.cfg = get_config()
    state.llm = LLM(state.cfg.ollama)
    # La premiere connexion applique le schema ; les suivantes sont par thread.
    db.connect(state.cfg.paths.db_path, embed_dim=state.cfg.ollama.embed_dim)
    if state.cfg.web.background_sync:
        state.syncer = pipeline.BackgroundSync(state.llm, state.cfg)
        state.syncer.start()
    try:
        yield
    finally:
        if state.syncer:
            state.syncer.stop()
        state.llm.close()


app = FastAPI(title="Assistant local", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=str(HERE / "static")), name="static")


def conn() -> sqlite3.Connection:
    return db.thread_connection(state.cfg.paths.db_path, embed_dim=state.cfg.ollama.embed_dim)


def _local(ts: int | None) -> datetime | None:
    return datetime.fromtimestamp(ts).astimezone() if ts else None


def _quand_court(dt: datetime | None) -> str:
    """Date lisible d'un coup d'oeil : 14:05, hier, lun., 12 sept., 12/09/2025."""
    if dt is None:
        return ""
    aujourd_hui = datetime.now().astimezone().date()
    ecart = (aujourd_hui - dt.date()).days
    if ecart == 0:
        return dt.strftime("%H:%M")
    if ecart == 1:
        return "hier"
    if 1 < ecart < 7:
        return JOURS[dt.weekday()]
    if dt.year == aujourd_hui.year:
        return f"{dt.day} {MOIS_COURTS[dt.month - 1]}"
    return dt.strftime("%d/%m/%Y")


def _avatar(nom: str) -> dict[str, Any]:
    """Initiales et teinte stable (meme personne = meme couleur) pour un avatar."""
    mots = [m for m in re.split(r"[\s._@-]+", nom) if m and m[0].isalnum()]
    initiales = "".join(m[0] for m in mots[:2]).upper() or "?"
    teinte = sum(ord(c) * (i + 1) for i, c in enumerate(nom.lower())) % 360
    return {"initiales": initiales, "teinte": teinte}


def _body_text(body: str | None) -> str:
    """Le texte indexe commence par un bloc d'entete ; le volet de lecture n'en veut pas."""
    body = body or ""
    return body.split("\n\n", 1)[1].strip() if "\n\n" in body else body


# Une URL, avec les crochets ou chevrons qui l'entourent souvent dans la version texte
# d'un mail ("Voir en ligne [https://...]"). Seuls http et https deviennent des liens.
_URL_RE = re.compile(r"[\[<]?(https?://[^\s<>\[\]\"']+)[\]>]?")


def _mail_html(texte: str) -> Markup:
    """Corps de mail pret a afficher : paragraphes, liens raccourcis a leur domaine.

    Tout le texte est echappe ; seules nos propres balises <p> et <a> sont ajoutees,
    et une URL n'entre dans un href qu'apres echappement (Markup.format).
    Les URL de suivi des newsletters font facilement 300 caracteres : affichees en
    entier, elles noient le message.
    """
    paragraphes = []
    for bloc in normalize_text(texte).split("\n\n"):
        morceaux, pos = [], 0
        for m in _URL_RE.finditer(bloc):
            url = m.group(1).rstrip(".,;:!?")
            morceaux.append(escape(bloc[pos : m.start()]))
            hote = (urlsplit(url).hostname or url).removeprefix("www.")
            morceaux.append(
                Markup('<a class="mail-link" href="{0}" target="_blank" rel="noreferrer noopener"'
                       ' title="{0}">{1}</a>').format(url, hote)
            )
            # Le crochet fermant part avec l'URL ; une ponctuation finale reste au texte.
            pos = m.end() if url == m.group(1) else m.start(1) + len(url)
        morceaux.append(escape(bloc[pos:]))
        paragraphes.append(Markup("<p>{}</p>").format(Markup("").join(morceaux)))
    return Markup("").join(paragraphes)


def _readable_body(body: str | None) -> str:
    """Corps du mail sans entete ; converti en texte s'il a ete indexe en HTML brut
    (mails recus avant la detection du HTML dans la partie texte)."""
    texte = _body_text(body)
    return gmail.html_to_text(texte) if gmail.looks_like_html(texte) else texte


# ------------------------------------------------------------- mails en HTML
#
# Le HTML d'un mail est du contenu hostile par defaut. Trois barrieres :
#  1. iframe sandbox SANS allow-scripts (et sans allow-forms) : rien ne s'execute ;
#  2. en-tete Content-Security-Policy : aucun script, aucune ressource externe
#     (sauf les images, et seulement si l'utilisateur le demande) ;
#  3. nettoyage des balises qui chargent ou naviguent malgre tout (meta refresh,
#     link, base...).
# Les images distantes sont bloquees par defaut : les charger previent l'expediteur
# que le mail a ete ouvert (pixels de suivi) et lui donne l'adresse IP.

_MAIL_STRIP_RE = re.compile(
    r"<(script|iframe|object|embed|noscript)\b.*?</\1\s*>"
    r"|<(?:script|iframe|object|embed|link|base|meta\s[^>]*http-equiv)[^>]*>",
    re.S | re.I,
)
_ON_ATTR_RE = re.compile(r"""\son[a-z]+\s*=\s*(?:"[^"]*"|'[^']*'|[^\s>]+)""", re.I)
_REMOTE_IMG_RE = re.compile(
    r"""(?:src|background|poster)\s*=\s*["']?\s*(?:https?:)?//|url\(\s*["']?\s*(?:https?:)?//""", re.I
)
_HEAD_RE = re.compile(r"<head\b[^>]*>", re.I)

# Mise en page minimale : les mails sont concus pour un fond blanc.
_MAIL_FRAME_HEAD = """<base target="_blank">
<style>
  html { background: #fff; color: #202124; color-scheme: light; }
  /* Beaucoup de newsletters fixent html/body a 100 % : le cadre doit suivre le contenu. */
  html, body { height: auto !important; min-height: 0 !important; }
  body { margin: 0; padding: 16px 20px; font: 14px/1.5 "Segoe UI", Arial, sans-serif; overflow-wrap: anywhere; }
  img { max-width: 100%; height: auto; }
</style>"""


def _mail_csp(images: bool) -> str:
    img = "data: https: http:" if images else "data:"
    return (
        f"default-src 'none'; style-src 'unsafe-inline'; img-src {img}; font-src data:;"
        " form-action 'none'; frame-ancestors 'self'"
    )


def _mail_frame_document(html_text: str) -> str:
    html_text = _MAIL_STRIP_RE.sub("", html_text)
    html_text = _ON_ATTR_RE.sub("", html_text)
    if _HEAD_RE.search(html_text):
        return _HEAD_RE.sub(lambda m: m.group(0) + _MAIL_FRAME_HEAD, html_text, count=1)
    return f"<!DOCTYPE html><html><head>{_MAIL_FRAME_HEAD}</head><body>{html_text}</body></html>"


def _mail_html_source(doc_id: int, *, fetch: bool) -> str:
    """HTML stocke du mail ; a defaut, recupere une fois aupres de Gmail."""
    row = conn().execute("SELECT html FROM mail_html WHERE doc_id = ?", (doc_id,)).fetchone()
    if row is not None or not fetch:
        return row["html"] if row else ""
    try:
        return gmail.fetch_html(conn(), state.cfg, doc_id)
    except Exception:  # hors ligne, jeton expire... : on affiche la version texte
        return ""


def _image_consent(doc_id: int, sender_email: str) -> str | None:
    """'sender' ou 'mail' si les images distantes de ce mail sont autorisees."""
    cles = {f"mail:{doc_id}": "mail"}
    if sender_email:
        cles[f"sender:{sender_email.lower()}"] = "sender"
    rows = conn().execute(
        f"SELECT key FROM image_consent WHERE key IN ({','.join('?' * len(cles))})", list(cles)
    ).fetchall()
    portees = {cles[r["key"]] for r in rows}
    return "sender" if "sender" in portees else ("mail" if portees else None)


def _sender_email(doc_id: int) -> str:
    row = conn().execute(
        "SELECT author FROM documents WHERE id = ? AND source = 'mail'", (doc_id,)
    ).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="Mail inconnu")
    m = re.search(r"<([^>]+)>", row["author"] or "")
    return (m.group(1) if m else (row["author"] or "")).strip().lower()


def _gmail_url(url: str | None) -> str | None:
    """Ouvre le mail dans le bon compte : /u/0/ vise le premier compte connecte."""
    adresse = db.get_meta(conn(), gmail.ADDRESS_META_KEY)
    if url and adresse and "/mail/u/0/" in url:
        return url.replace("/mail/u/0/", f"/mail/?authuser={quote(adresse)}")
    return url


# ------------------------------------------------------------------------ mails


def _folder(value: str | None) -> str:
    """Onglet demande, ramene a une valeur connue (une URL peut contenir n'importe quoi)."""
    return value if value in {cle for cle, _ in MAIL_FOLDERS} else DEFAULT_FOLDER


# COALESCE : les mails indexes quand la synchro se limitait a `in:inbox` n'ont pas de
# champ `folder`. Ils venaient tous de la boite de reception ; la prochaine
# reconciliation (gmail.reconcile) l'ecrit pour de bon.
_FOLDER_SQL = "COALESCE(json_extract(d.meta, '$.folder'), 'inbox')"


def _action_badge(row: sqlite3.Row) -> dict[str, str] | None:
    """Icone resumant l'action conseillee ; None si le mail n'a pas ete trie.

    Les mails archives ou envoyes ne passent pas par le modele : leur colonne est
    vide, et la liste n'affiche alors aucune icone plutot qu'une icone inventee.
    """
    if row["urgency"] is None:
        return None
    type_action = row["action_type"] or triage.fallback_action_type(
        row["category"], row["urgency"]
    )
    icone, libelle = ACTION_BADGES.get(type_action, ACTION_BADGES["traiter"])
    detail = (row["action"] or "").strip()
    return {
        "type": type_action,
        "icon": icone,
        "label": libelle,
        # Infobulle : "Répondre" seul ne dit pas a quoi ni a qui.
        "title": f"{libelle} — {detail}" if detail and detail.lower() != "rien" else libelle,
    }


def _mail_counts() -> dict[str, int]:
    """Nombre de mails par onglet, pour la pastille de chaque dossier."""
    counts = {cle: 0 for cle, _ in MAIL_FOLDERS}
    for row in conn().execute(
        "SELECT COALESCE(json_extract(meta, '$.folder'), 'inbox') AS dossier, COUNT(*) AS n"
        " FROM documents WHERE source = 'mail' GROUP BY dossier"
    ):
        counts["all"] += row["n"]
        if row["dossier"] in counts:
            counts[row["dossier"]] += row["n"]
    return counts


def _mail_rows(
    limit: int = 60, *, urgent: bool = False, folder: str = DEFAULT_FOLDER
) -> list[dict[str, Any]]:
    # Le dossier et le filtre "important" sont appliques en SQL, pas apres coup sur
    # les N plus recents : les mails urgents sont rares et disperses dans tout
    # l'historique, et les archives sont desormais bien plus nombreuses que la boite.
    sql = (
        "SELECT d.id, d.title, d.author, d.ts, d.url, d.meta,"
        "       t.urgency, t.category, t.action_type, t.summary, t.action"
        " FROM documents d LEFT JOIN mail_triage t ON t.doc_id = d.id"
        " WHERE d.source = 'mail'"
    )
    params: list[Any] = []
    if folder != "all":
        sql += f" AND {_FOLDER_SQL} = ?"
        params.append(folder)
    if urgent:
        sql += f" AND t.urgency >= {URGENT_THRESHOLD}"
    sql += " ORDER BY d.ts DESC LIMIT ?"
    params.append(limit)
    out = []
    for row in db.iter_rows(conn(), sql, params):
        meta = json.loads(row["meta"] or "{}")
        auteur = _display_name(row["author"] or "")
        out.append(
            {
                "doc_id": row["id"],
                "title": row["title"] or "(sans objet)",
                "author": auteur,
                "avatar": _avatar(auteur),
                "when": _local(row["ts"]),
                "when_short": _quand_court(_local(row["ts"])),
                "unread": meta.get("unread", False),
                "urgency": row["urgency"],
                "urgency_label": URGENCY_LABELS.get(row["urgency"] or 0, "non trie"),
                "category": row["category"],
                "act": _action_badge(row),
                "summary": row["summary"] or meta.get("snippet", "")[:160],
                "attachments": meta.get("attachments") or [],
            }
        )
    return out


def _display_name(sender: str) -> str:
    """'Marie Dupont <marie@x.fr>' -> 'Marie Dupont' ; sinon l'adresse telle quelle."""
    m = re.match(r'\s*"?([^"<]+?)"?\s*<[^>]+>', sender)
    return m.group(1).strip() if m else sender.strip()


def _mail_detail(doc_id: int) -> dict[str, Any] | None:
    row = conn().execute(
        "SELECT d.*, t.urgency, t.category, t.action_type, t.summary, t.action"
        " FROM documents d LEFT JOIN mail_triage t ON t.doc_id = d.id"
        " WHERE d.id = ? AND d.source = 'mail'",
        (doc_id,),
    ).fetchone()
    if row is None:
        return None
    meta = json.loads(row["meta"] or "{}")
    expediteur = row["author"] or ""
    nom = _display_name(expediteur)
    adresse = re.search(r"<([^>]+)>", expediteur)
    source_html = _mail_html_source(row["id"], fetch=True)
    consent = _image_consent(row["id"], _sender_email(row["id"]))
    return {
        "has_html": bool(source_html),
        "remote_images": bool(_REMOTE_IMG_RE.search(source_html)),
        "images_ok": consent is not None,
        "images_scope": consent,
        "doc_id": row["id"],
        "title": row["title"] or "(sans objet)",
        "author": expediteur,
        "author_name": nom,
        "author_email": adresse.group(1) if adresse else "",
        "avatar": _avatar(nom),
        "to": meta.get("to", ""),
        "cc": meta.get("cc", ""),
        "when": _local(row["ts"]),
        "url": _gmail_url(row["url"]),
        "unread": meta.get("unread", False),
        "attachments": meta.get("attachments") or [],
        "urgency": row["urgency"],
        "urgency_label": URGENCY_LABELS.get(row["urgency"] or 0, "non trie"),
        "category": row["category"],
        "act": _action_badge(row),
        "summary": row["summary"],
        "action": row["action"],
        "folder": meta.get("folder", "inbox"),
        "body": _mail_html(_readable_body(row["body"])),
    }


# ----------------------------------------------------------------------- agenda


def _safe_color(value: str | None) -> str:
    return value if value and _HEX_COLOR.match(value) else ""


def _week(monday: date) -> dict[str, Any]:
    """Semaine prete a dessiner : evenements positionnes sur une grille horaire."""
    start = datetime.combine(monday, time.min).astimezone()
    end = start + timedelta(days=7)
    colors = gcal.calendar_colors(conn())
    maintenant = datetime.now().astimezone()

    days = [
        {
            "date": monday + timedelta(days=i),
            "dow": JOURS[i],
            "is_today": monday + timedelta(days=i) == maintenant.date(),
            "all_day": [],
            "timed": [],
        }
        for i in range(7)
    ]
    for row in gcal.between(conn(), int(start.timestamp()), int(end.timestamp())):
        meta = json.loads(row["meta"] or "{}")
        debut = _local(row["ts"])
        if debut is None:
            continue
        idx = (debut.date() - monday).days
        if not 0 <= idx < 7:
            continue
        fin = _local(meta.get("end_ts")) or debut + timedelta(hours=1)
        ev = {
            "title": row["title"] or "(sans titre)",
            "start": debut.strftime("%H:%M"),
            "end": fin.strftime("%H:%M"),
            "calendar": meta.get("calendar_name") or "",
            "color": _safe_color(colors.get(meta.get("calendar_id", ""))),
            "location": meta.get("location") or "",
            "attendees": len(meta.get("attendees") or []),
            "url": row["url"],
        }
        if meta.get("all_day"):
            days[idx]["all_day"].append(ev)
            continue
        ev["m0"] = debut.hour * 60 + debut.minute
        # Un evenement qui deborde sur le lendemain s'arrete a minuit dans cette colonne.
        ev["m1"] = 24 * 60 if fin.date() > debut.date() else fin.hour * 60 + fin.minute
        ev["m1"] = max(ev["m1"], ev["m0"] + 20)  # lisible meme s'il dure 5 minutes
        days[idx]["timed"].append(ev)

    # Plage horaire : 7h-21h, elargie si un evenement de la semaine en sort.
    minutes = [m for d in days for e in d["timed"] for m in (e["m0"], e["m1"])]
    h0 = min([PLAGE_DEFAUT[0]] + [m // 60 for m in minutes])
    h1 = max([PLAGE_DEFAUT[1]] + [-(-m // 60) for m in minutes])

    for day in days:
        _disposer(day["timed"])
        for ev in day["timed"]:
            ev["top"] = round((ev["m0"] - h0 * 60) / 60 * HEURE_PX)
            ev["height"] = round((ev["m1"] - ev["m0"]) / 60 * HEURE_PX) - 2
            ev["compact"] = ev["m1"] - ev["m0"] < 45

    now_top = None
    if any(d["is_today"] for d in days):
        m = maintenant.hour * 60 + maintenant.minute
        if h0 * 60 <= m <= h1 * 60:
            now_top = round((m - h0 * 60) / 60 * HEURE_PX)

    return {
        "days": days,
        "hours": [f"{h:02d}:00" for h in range(h0, h1)],
        "grid_height": (h1 - h0) * HEURE_PX,
        "hour_px": HEURE_PX,
        "now_top": now_top,
        # Heure a laquelle faire defiler la grille au chargement : un peu avant le
        # premier evenement de la semaine, ou 8h.
        "scroll_to": max(0, (min([e["m0"] for d in days for e in d["timed"]] or [8 * 60]) // 60 - 1 - h0))
        * HEURE_PX,
        "has_all_day": any(d["all_day"] for d in days),
    }


def _disposer(evenements: list[dict[str, Any]]) -> None:
    """Place cote a cote les evenements qui se chevauchent (comme Google Agenda).

    Les evenements sont groupes en paquets qui se chevauchent de proche en proche ;
    dans chaque paquet, chacun prend la premiere colonne libre, et tous se partagent
    la largeur selon le nombre de colonnes du paquet.
    """
    evenements.sort(key=lambda e: (e["m0"], -e["m1"]))
    paquet: list[dict[str, Any]] = []
    fin_paquet = -1

    def fermer() -> None:
        colonnes = max((e["col"] for e in paquet), default=0) + 1
        for e in paquet:
            e["left"] = round(e["col"] / colonnes * 100, 2)
            e["width"] = round(100 / colonnes, 2)

    for ev in evenements:
        if paquet and ev["m0"] >= fin_paquet:
            fermer()
            paquet, fin_paquet = [], -1
        occupees = {e["col"] for e in paquet if e["m1"] > ev["m0"]}
        ev["col"] = next(c for c in range(len(paquet) + 1) if c not in occupees)
        paquet.append(ev)
        fin_paquet = max(fin_paquet, ev["m1"])
    if paquet:
        fermer()


def _calendar_legend() -> list[dict[str, str]]:
    colors = gcal.calendar_colors(conn())
    seen: dict[str, str] = {}
    for row in conn().execute(
        "SELECT DISTINCT json_extract(meta, '$.calendar_id') cid,"
        "       json_extract(meta, '$.calendar_name') nom"
        " FROM documents WHERE source = 'event'"
    ):
        if row["cid"] and row["cid"] not in seen:
            seen[row["cid"]] = row["nom"] or "agenda principal"
    return [
        {"name": nom, "color": _safe_color(colors.get(cid))}
        for cid, nom in sorted(seen.items(), key=lambda kv: kv[1].casefold())
    ]


def _week_label(monday: date) -> str:
    sunday = monday + timedelta(days=6)
    if monday.month == sunday.month:
        return f"{monday.day} - {sunday.day} {MOIS[sunday.month - 1]} {sunday.year}"
    return (
        f"{monday.day} {MOIS[monday.month - 1]} - {sunday.day} {MOIS[sunday.month - 1]}"
        f" {sunday.year}"
    )


# --------------------------------------------------------------------- fichiers


def _file_tree() -> list[dict[str, Any]]:
    """Arbre des fichiers indexes, un noeud racine par dossier de files.roots."""
    roots = [Path(r).expanduser() for r in state.cfg.files.roots]
    arbres = {str(r): {"name": r.name or str(r), "path": str(r), "dirs": {}, "files": []} for r in roots}

    for row in conn().execute(
        "SELECT id, external_id, title, meta FROM documents WHERE source = 'file'"
    ):
        chemin = Path(row["external_id"])
        racine = next((r for r in roots if chemin.is_relative_to(r.resolve())), None)
        if racine is None:
            continue
        noeud = arbres[str(racine)]
        for partie in chemin.relative_to(racine.resolve()).parts[:-1]:
            noeud = noeud["dirs"].setdefault(
                partie, {"name": partie, "dirs": {}, "files": []}
            )
        meta = json.loads(row["meta"] or "{}")
        noeud["files"].append(
            {
                "doc_id": row["id"],
                "name": row["title"] or chemin.name,
                "ext": (meta.get("extension") or chemin.suffix).lstrip(".").lower(),
            }
        )

    def finalise(noeud: dict[str, Any]) -> dict[str, Any]:
        enfants = [finalise(d) for d in noeud["dirs"].values()]
        enfants.sort(key=lambda d: d["name"].casefold())
        noeud["files"].sort(key=lambda f: f["name"].casefold())
        noeud["children"] = enfants
        noeud["count"] = len(noeud["files"]) + sum(c["count"] for c in enfants)
        return noeud

    return [finalise(a) for a in arbres.values()]


def _file_detail(doc_id: int) -> dict[str, Any] | None:
    row = db.get_document(conn(), doc_id)
    if row is None or row["source"] != "file":
        return None
    meta = json.loads(row["meta"] or "{}")
    texte = _body_text(row["body"])
    chunks = conn().execute("SELECT COUNT(*) n FROM chunks WHERE doc_id = ?", (doc_id,)).fetchone()
    return {
        "doc_id": row["id"],
        "name": row["title"],
        "path": row["external_id"],
        "folder": meta.get("folder", ""),
        "ext": (meta.get("extension") or "").lstrip(".").lower(),
        "size_kb": round((meta.get("size_bytes") or 0) / 1024),
        "modified": _local(row["ts"]),
        "chunks": chunks["n"],
        "chars": len(texte),
        "extract": texte[:6000],
        "truncated": len(texte) > 6000,
    }


# ------------------------------------------------------------------ etat commun


def _status() -> dict[str, Any]:
    stats = db.stats(conn())
    last = state.syncer.last_run_ts if state.syncer else None
    missing: list[str] = []
    try:
        missing = state.llm.check_models()
        ollama_ok = True
    except OllamaError:
        ollama_ok = False
    return {
        "documents": stats["documents"],
        "chunks": stats["chunks"],
        "triaged": stats["triaged"],
        "last_sync": datetime.fromtimestamp(last).astimezone().strftime("%H:%M:%S") if last else None,
        "last_report": state.syncer.last_report.summary()
        if state.syncer and state.syncer.last_report
        else None,
        "ollama_ok": ollama_ok,
        "missing_models": missing,
        "chat_model": state.cfg.ollama.chat_model,
        "roots_configured": bool(state.cfg.files.roots),
        "background_sync": state.cfg.web.background_sync,
    }


# Nombre de messages affiches a l'ouverture d'une conversation, et nombre de
# messages precedents transmis au modele comme contexte.
HISTORY_SHOWN = 60
HISTORY_FOR_MODEL = 6


def _active_conversation(request: Request, conversations: list[dict[str, Any]]) -> int:
    """Conversation ouverte : celle du cookie si elle existe encore, sinon la plus
    recemment utilisee ; il y en a toujours au moins une."""
    ids = {c["id"] for c in conversations}
    cookie = request.cookies.get("conversation", "")
    if cookie.isdigit() and int(cookie) in ids:
        return int(cookie)
    if conversations:
        return max(conversations, key=lambda c: (c["updated_at"] or 0, c["id"]))["id"]
    return agent.create_conversation(conn())


def _chat_context(request: Request) -> dict[str, Any]:
    conversations = agent.list_conversations(conn())
    active = _active_conversation(request, conversations)
    if active not in {c["id"] for c in conversations}:
        conversations = agent.list_conversations(conn())
    return {
        "conversations": conversations,
        "active_conversation": active,
        "history": agent.load_history(conn(), limit=HISTORY_SHOWN, conversation_id=active),
    }


def _page(request: Request, template: str, tab: str, **ctx: Any) -> HTMLResponse:
    ctx.update(tab=tab, tabs=TABS, status=_status(), **_chat_context(request))
    return templates.TemplateResponse(request, template, ctx)


# ------------------------------------------------------------------------ pages


@app.get("/", response_class=HTMLResponse)
def page_mails(
    request: Request, mail: int | None = None, urgent: bool = False, folder: str | None = None
):
    folder = _folder(folder)
    # Les mails envoyes ne sont jamais tries : croiser l'onglet avec "Importants" n'y
    # renverrait qu'une liste vide, sans que rien n'explique pourquoi.
    urgent = urgent and folder != "sent"
    mails = _mail_rows(urgent=urgent, folder=folder)
    selected = mail or (mails[0]["doc_id"] if mails else None)
    return _page(
        request,
        "mails.html",
        "mails",
        mails=mails,
        urgent=urgent,
        folder=folder,
        folders=MAIL_FOLDERS,
        counts=_mail_counts(),
        selected=selected,
        detail=_mail_detail(selected) if selected else None,
    )


@app.get("/agenda", response_class=HTMLResponse)
def page_agenda(request: Request, semaine: str | None = None):
    try:
        ref = date.fromisoformat(semaine) if semaine else datetime.now().astimezone().date()
    except ValueError:
        ref = datetime.now().astimezone().date()
    monday = ref - timedelta(days=ref.weekday())
    return _page(
        request,
        "agenda.html",
        "agenda",
        week=_week(monday),
        week_label=_week_label(monday),
        prev_week=(monday - timedelta(days=7)).isoformat(),
        next_week=(monday + timedelta(days=7)).isoformat(),
        legend=_calendar_legend(),
    )


@app.get("/fichiers", response_class=HTMLResponse)
def page_fichiers(request: Request, f: int | None = None):
    return _page(
        request,
        "fichiers.html",
        "fichiers",
        tree=_file_tree(),
        selected=f,
        detail=_file_detail(f) if f else None,
    )


# -------------------------------------------------------------------- fragments


@app.get("/fragment/mails", response_class=HTMLResponse)
def fragment_mails(
    request: Request, urgent: bool = False, selected: str = "", folder: str | None = None
):
    # `selected` arrive vide quand aucun mail n'est ouvert : type str plutot qu'int,
    # sinon FastAPI renvoie une 422 et la liste cesse de se rafraichir sans bruit.
    folder = _folder(folder)
    urgent = urgent and folder != "sent"
    return templates.TemplateResponse(
        request,
        "_mails.html",
        {
            "mails": _mail_rows(urgent=urgent, folder=folder),
            "urgent": urgent,
            "folder": folder,
            "selected": int(selected) if selected.isdigit() else None,
        },
    )


@app.get("/fragment/mail/{doc_id}", response_class=HTMLResponse)
def fragment_mail(request: Request, doc_id: int):
    return templates.TemplateResponse(request, "_mail_detail.html", {"detail": _mail_detail(doc_id)})


@app.get("/mail/{doc_id}/html", response_class=HTMLResponse)
def mail_frame(doc_id: int, images: bool = False):
    """Document charge dans l'iframe isolee du volet de lecture."""
    source_html = _mail_html_source(doc_id, fetch=False)
    if not source_html:
        raise HTTPException(status_code=404, detail="Pas de version HTML")
    return HTMLResponse(
        _mail_frame_document(source_html),
        headers={
            "Content-Security-Policy": _mail_csp(images),
            "Referrer-Policy": "no-referrer",
            "X-Content-Type-Options": "nosniff",
            "Cache-Control": "no-store",
        },
    )


@app.post("/api/mail/{doc_id}/images")
def api_mail_images(
    doc_id: int,
    scope: str = Form(...),
    x_assistant: str | None = Header(default=None),
):
    """Memorise (scope=mail|sender) ou retire (scope=none) l'autorisation des images.

    Meme garde-fou que /api/open : une page tierce ne peut pas envoyer l'en-tete, donc
    ne peut pas autoriser a ton insu le chargement des pixels de suivi.
    """
    if x_assistant != "1":
        raise HTTPException(status_code=403, detail="En-tete X-Assistant manquant")
    adresse = _sender_email(doc_id)
    database = conn()
    if scope == "none":
        database.execute(
            "DELETE FROM image_consent WHERE key IN (?, ?)", (f"mail:{doc_id}", f"sender:{adresse}")
        )
    elif scope in ("mail", "sender"):
        cle = f"mail:{doc_id}" if scope == "mail" or not adresse else f"sender:{adresse}"
        database.execute(
            "INSERT OR IGNORE INTO image_consent(key, created_at) VALUES(?, ?)",
            (cle, int(datetime.now().timestamp())),
        )
    else:
        raise HTTPException(status_code=400, detail="scope invalide")
    database.commit()
    return {"ok": True}


@app.get("/fragment/file/{doc_id}", response_class=HTMLResponse)
def fragment_file(request: Request, doc_id: int):
    return templates.TemplateResponse(request, "_file_detail.html", {"detail": _file_detail(doc_id)})


@app.get("/fragment/status", response_class=HTMLResponse)
def fragment_status(request: Request):
    return templates.TemplateResponse(request, "_status.html", {"status": _status()})


# -------------------------------------------------------------------------- API


@app.post("/api/open/{doc_id}")
def api_open(doc_id: int, x_assistant: str | None = Header(default=None)):
    """Ouvre un fichier indexe avec l'application par defaut du systeme.

    Un navigateur refuse d'ouvrir un lien file:// depuis une page http : c'est donc le
    serveur local qui ouvre le fichier. Deux garde-fous : seul un document deja indexe
    peut etre ouvert (jamais un chemin arbitraire), et l'en-tete X-Assistant est exige.
    Une page web tierce ne peut pas l'envoyer vers 127.0.0.1 sans une autorisation
    CORS que ce serveur ne donne jamais : pas d'ouverture declenchee a distance.
    """
    if x_assistant != "1":
        raise HTTPException(status_code=403, detail="En-tete X-Assistant manquant")
    row = db.get_document(conn(), doc_id)
    if row is None or row["source"] != "file":
        raise HTTPException(status_code=404, detail="Fichier inconnu")
    chemin = Path(row["external_id"])
    if not chemin.is_file():
        raise HTTPException(status_code=404, detail="Le fichier n'existe plus sur le disque")
    if sys.platform == "win32":
        os.startfile(chemin)  # noqa: S606 - chemin issu de l'index, pas de l'utilisateur
    else:
        subprocess.Popen(["open" if sys.platform == "darwin" else "xdg-open", str(chemin)])
    return {"ok": True}


@app.post("/api/ask")
def api_ask(
    question: str = Form(...),
    doc_id: int | None = Form(None),
    conversation_id: int | None = Form(None),
):
    question = question.strip()
    if not question:
        raise HTTPException(status_code=400, detail="Question vide")

    database = conn()
    conv = _conversation_row(conversation_id) if conversation_id else None
    if conv is None:
        conversation_id = agent.create_conversation(database)
        conv = _conversation_row(conversation_id)
    # Chaque conversation a son propre contexte : le modele ne voit que ses messages.
    history = [
        {"role": h["role"], "content": h["content"]}
        for h in agent.load_history(database, limit=HISTORY_FOR_MODEL, conversation_id=conversation_id)
    ]
    titre = conv["title"]
    if titre == agent.NEW_CONVERSATION_TITLE and not history:
        titre = agent.title_from_question(question)
        database.execute("UPDATE conversations SET title = ? WHERE id = ?", (titre, conversation_id))
    agent.save_message(database, "user", question, conversation_id=conversation_id)
    try:
        answer = agent.ask(
            database, state.llm, state.cfg, question, history=history, focus_doc=doc_id
        )
    except OllamaError as exc:
        return JSONResponse({"error": str(exc)}, status_code=503)

    # L'onglet a pu etre ferme pendant que le modele repondait : rien a enregistrer.
    if _conversation_row(conversation_id) is not None:
        agent.save_message(
            database, "assistant", answer.text, answer.citations, conversation_id=conversation_id
        )
    return {
        "conversation_id": conversation_id,
        "titre": titre,
        "reponse": answer.text,
        "citations": answer.citations,
        "outils": answer.tool_trace,
    }


def _conversation_row(conversation_id: int | None) -> sqlite3.Row | None:
    return conn().execute(
        "SELECT id, title FROM conversations WHERE id = ?", (conversation_id,)
    ).fetchone()


@app.post("/api/conversations")
def api_new_conversation(x_assistant: str | None = Header(default=None)):
    if x_assistant != "1":
        raise HTTPException(status_code=403, detail="En-tete X-Assistant manquant")
    cid = agent.create_conversation(conn())
    return {"id": cid, "titre": agent.NEW_CONVERSATION_TITLE}


@app.delete("/api/conversations/{conversation_id}")
def api_delete_conversation(conversation_id: int, x_assistant: str | None = Header(default=None)):
    if x_assistant != "1":
        raise HTTPException(status_code=403, detail="En-tete X-Assistant manquant")
    agent.delete_conversation(conn(), conversation_id)
    return {"ok": True}


@app.get("/fragment/chat/{conversation_id}", response_class=HTMLResponse)
def fragment_chat(request: Request, conversation_id: int):
    """Messages d'une conversation, pour changer d'onglet sans recharger la page."""
    if _conversation_row(conversation_id) is None:
        raise HTTPException(status_code=404, detail="Conversation inconnue")
    return templates.TemplateResponse(
        request,
        "_chat.html",
        {"history": agent.load_history(conn(), limit=HISTORY_SHOWN, conversation_id=conversation_id)},
    )


@app.post("/api/sync")
def api_sync(full: bool = False):
    report = pipeline.sync_all(conn(), state.llm, state.cfg, full=full, notify_user=False)
    return {
        "resume": report.summary(),
        "modifies": report.changed,
        "chunks": report.indexed_chunks,
        "erreurs": report.errors,
        "ignore": report.skipped,
    }


@app.get("/api/search")
def api_search(q: str, sources: str | None = None, top_k: int = 8):
    src = [s for s in (sources or "").split(",") if s] or None
    try:
        hits = search.search(conn(), state.llm, state.cfg, q, sources=src, top_k=top_k)
    except OllamaError as exc:
        return JSONResponse({"error": str(exc)}, status_code=503)
    return {"resultats": [h.as_dict() for h in hits]}


@app.get("/api/doc/{doc_id}")
def api_doc(doc_id: int):
    row = db.get_document(conn(), doc_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Document introuvable")
    return {
        "doc_id": row["id"],
        "source": row["source"],
        "titre": row["title"],
        "auteur": row["author"],
        "date": _local(row["ts"]).isoformat() if row["ts"] else None,
        "url": row["url"],
        "meta": json.loads(row["meta"] or "{}"),
        "contenu": row["body"],
    }
