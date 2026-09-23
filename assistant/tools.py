"""Outils exposes au LLM : recherche, agenda, lecture de document, mails recents.

Chaque outil a un schema JSON (pour l'appel de fonction Ollama) et une implementation
qui ne touche qu'a la base locale.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta
from typing import Any, Callable

from . import db, search
from .config import Config
from .ingest import gcal
from .llm import LLM

ToolFn = Callable[..., dict[str, Any]]

SOURCE_LABELS = {"mail": "mail", "event": "evenement d'agenda", "file": "fichier local"}

TOOL_SCHEMAS: list[dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "rechercher",
            "description": (
                "Cherche dans les mails, les evenements d'agenda et les fichiers locaux de "
                "l'utilisateur. A utiliser pour toute question portant sur son contenu personnel."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "requete": {
                        "type": "string",
                        "description": "Les mots-cles ou la question, en langage naturel.",
                    },
                    "sources": {
                        "type": "array",
                        "items": {"type": "string", "enum": ["mail", "event", "file"]},
                        "description": "Limiter la recherche a certaines sources. Omettre pour tout chercher.",
                    },
                    "jours": {
                        "type": "integer",
                        "description": "Ne garder que les elements des N derniers jours.",
                    },
                    "expediteur": {
                        "type": "string",
                        "description": "Filtrer sur un expediteur ou un nom (recherche partielle).",
                    },
                },
                "required": ["requete"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "agenda",
            "description": "Liste les prochains evenements de l'agenda Google, par ordre chronologique.",
            "parameters": {
                "type": "object",
                "properties": {
                    "jours": {
                        "type": "integer",
                        "description": "Fenetre a regarder, en jours (7 par defaut).",
                    }
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "mails_recents",
            "description": "Liste les derniers mails recus, avec leur tri automatique (urgence, categorie).",
            "parameters": {
                "type": "object",
                "properties": {
                    "limite": {"type": "integer", "description": "Nombre de mails (20 par defaut)."},
                    "non_lus": {"type": "boolean", "description": "Ne remonter que les non lus."},
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "lire_document",
            "description": (
                "Lit un document en entier a partir de son doc_id, quand un extrait "
                "renvoye par `rechercher` ne suffit pas."
            ),
            "parameters": {
                "type": "object",
                "properties": {"doc_id": {"type": "integer"}},
                "required": ["doc_id"],
            },
        },
    },
]


def jour_relatif(ts: int | None) -> str:
    """"aujourd'hui", "demain", "dans 5 jours"...

    Un modele de 4B se trompe regulierement en comparant deux dates au format
    jj/mm/aaaa. Lui donner l'ecart deja calcule supprime la classe d'erreur.
    """
    if not ts:
        return "date inconnue"
    jour = datetime.fromtimestamp(ts).astimezone().date()
    ecart = (jour - datetime.now().astimezone().date()).days
    if ecart == 0:
        return "aujourd'hui"
    if ecart == 1:
        return "demain"
    if ecart == -1:
        return "hier"
    if ecart < 0:
        return f"il y a {-ecart} jours"
    return f"dans {ecart} jours"


class Toolbox:
    """Implementation des outils, liee a une connexion et une config."""

    def __init__(self, conn: sqlite3.Connection, llm: LLM, cfg: Config) -> None:
        self.conn = conn
        self.llm = llm
        self.cfg = cfg
        # Tout document renvoye au modele, quel que soit l'outil : c'est ce qui permet
        # a l'interface de resoudre un [42] cite par le modele en titre cliquable.
        self.seen_docs: dict[int, dict[str, Any]] = {}

    def _remember(self, doc_id: int) -> None:
        if doc_id in self.seen_docs:
            return
        row = db.get_document(self.conn, doc_id)
        if row is None:
            return
        self.seen_docs[doc_id] = {
            "doc_id": doc_id,
            "source": row["source"],
            "titre": row["title"] or "",
            "date": datetime.fromtimestamp(row["ts"]).astimezone().strftime("%d/%m/%Y %H:%M")
            if row["ts"]
            else "",
            "url": row["url"],
        }

    @property
    def registry(self) -> dict[str, ToolFn]:
        return {
            "rechercher": self.rechercher,
            "agenda": self.agenda,
            "mails_recents": self.mails_recents,
            "lire_document": self.lire_document,
        }

    def call(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        fn = self.registry.get(name)
        if fn is None:
            return {"erreur": f"Outil inconnu : {name}"}
        try:
            return fn(**arguments)
        except TypeError as exc:
            return {"erreur": f"Arguments invalides pour {name} : {exc}"}
        except Exception as exc:  # un outil qui plante ne doit pas tuer la conversation
            return {"erreur": f"{type(exc).__name__} : {exc}"}

    # ------------------------------------------------------------------ outils

    def rechercher(
        self,
        requete: str,
        sources: list[str] | None = None,
        jours: int | None = None,
        expediteur: str | None = None,
    ) -> dict[str, Any]:
        since = None
        if jours:
            since = int((datetime.now() - timedelta(days=jours)).timestamp())
        hits = search.search(
            self.conn,
            self.llm,
            self.cfg,
            requete,
            sources=sources,
            since_ts=since,
            author=expediteur,
        )
        for hit in hits:
            self._remember(hit.doc_id)
        if not hits:
            # Distinguer "cherche, rien trouve" de "cette source n'existe pas dans
            # l'index" : sans cette precision, le modele repond "je n'ai pas trouve
            # de devis dans vos fichiers" alors qu'aucun fichier n'est indexe.
            note = "Aucun resultat dans l'index local."
            vides = self._sources_vides(sources)
            if vides:
                note += (
                    f" Attention : aucun document de type {', '.join(vides)} n'est indexe"
                    " sur cette machine. Dis-le a l'utilisateur plutot que de laisser"
                    " croire que la recherche a porte dessus."
                )
            return {"resultats": [], "note": note}
        return {"resultats": [h.as_dict() for h in hits]}

    def _sources_vides(self, sources: list[str] | None) -> list[str]:
        peuplees = {
            row["source"]
            for row in self.conn.execute(
                "SELECT source, COUNT(*) n FROM documents GROUP BY source HAVING n > 0"
            )
        }
        demandees = sources or list(SOURCE_LABELS)
        return [SOURCE_LABELS[s] for s in demandees if s in SOURCE_LABELS and s not in peuplees]

    def agenda(self, jours: int = 7) -> dict[str, Any]:
        rows = gcal.upcoming(self.conn, days=jours)
        events = []
        for row in rows:
            meta = json.loads(row["meta"] or "{}")
            self._remember(row["id"])
            attendees = meta.get("attendees") or []
            events.append(
                {
                    "doc_id": row["id"],
                    "titre": row["title"],
                    # L'agenda d'origine distingue un vrai rendez-vous d'une entree
                    # d'abonnement (jours feries, fete des prenoms, calendrier sportif).
                    "agenda": meta.get("calendar_name") or "agenda principal",
                    "debut": datetime.fromtimestamp(row["ts"]).astimezone().strftime("%d/%m/%Y %H:%M")
                    if row["ts"]
                    else None,
                    "jour": jour_relatif(row["ts"]),
                    "journee_entiere": meta.get("all_day", False),
                    "lieu": meta.get("location") or None,
                    "participants": attendees,
                    "est_une_reunion": bool(attendees),
                }
            )
        return {"evenements": events, "fenetre_jours": jours}

    def mails_recents(self, limite: int = 20, non_lus: bool = False) -> dict[str, Any]:
        rows = search.recent_mails(self.conn, limit=min(limite, 50), unread_only=non_lus)
        mails = []
        for row in rows:
            meta = json.loads(row["meta"] or "{}")
            self._remember(row["id"])
            mails.append(
                {
                    "doc_id": row["id"],
                    "de": row["author"],
                    "objet": row["title"],
                    "date": datetime.fromtimestamp(row["ts"]).astimezone().strftime("%d/%m/%Y %H:%M")
                    if row["ts"]
                    else None,
                    "jour": jour_relatif(row["ts"]),
                    "non_lu": meta.get("unread", False),
                    "urgence": row["urgency"],
                    "categorie": row["category"],
                    "resume": row["summary"],
                    "extrait": meta.get("snippet", "")[:200],
                }
            )
        return {"mails": mails}

    def lire_document(self, doc_id: int) -> dict[str, Any]:
        row = db.get_document(self.conn, int(doc_id))
        if row is None:
            return {"erreur": f"Aucun document avec doc_id={doc_id}"}
        self._remember(int(doc_id))
        return {
            "doc_id": row["id"],
            "source": row["source"],
            "titre": row["title"],
            "auteur": row["author"],
            "date": datetime.fromtimestamp(row["ts"]).astimezone().strftime("%d/%m/%Y %H:%M")
            if row["ts"]
            else None,
            "contenu": (row["body"] or "")[:12_000],
        }
