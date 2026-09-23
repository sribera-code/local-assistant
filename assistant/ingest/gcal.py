"""Recuperation de l'agenda Google (lecture seule) vers la base locale."""

from __future__ import annotations

import hashlib
import json
import sqlite3
import unicodedata
from datetime import datetime, timedelta, timezone
from typing import Any

from .. import db
from ..config import Config
from googleapiclient.errors import HttpError

from .google_auth import calendar_service, execute_with_retry


def _parse_when(when: dict[str, Any]) -> tuple[int | None, bool]:
    """Retourne (epoch, journee_entiere) pour un start/end d'evenement Google."""
    if "dateTime" in when:
        dt = datetime.fromisoformat(when["dateTime"])
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return int(dt.timestamp()), False
    if "date" in when:
        dt = datetime.fromisoformat(when["date"]).replace(tzinfo=timezone.utc)
        return int(dt.timestamp()), True
    return None, False


def _format_when(start_ts: int | None, end_ts: int | None, all_day: bool) -> str:
    if start_ts is None:
        return "date inconnue"
    start = datetime.fromtimestamp(start_ts).astimezone()
    if all_day:
        return start.strftime("%A %d %B %Y (journee entiere)")
    label = start.strftime("%A %d %B %Y de %H:%M")
    if end_ts:
        label += datetime.fromtimestamp(end_ts).astimezone().strftime(" a %H:%M")
    return label


def event_to_document(
    event: dict[str, Any], calendar_id: str, calendar_name: str = ""
) -> dict[str, Any] | None:
    if event.get("status") == "cancelled":
        return None

    start_ts, all_day = _parse_when(event.get("start") or {})
    end_ts, _ = _parse_when(event.get("end") or {})
    title = event.get("summary") or "(sans titre)"
    organizer = (event.get("organizer") or {}).get("email", "")
    attendees = [a.get("email", "") for a in event.get("attendees") or []]
    location = event.get("location") or ""
    description = event.get("description") or ""

    # Le nom de l'agenda fait partie du texte indexe : sans lui, une entree d'un
    # agenda d'abonnement (fete des prenoms, jours feries, calendrier sportif) est
    # indiscernable d'un vrai rendez-vous, et le modele invente une reunion.
    indexed = (
        f"Evenement d'agenda : {title}\n"
        + (f"Agenda : {calendar_name}\n" if calendar_name else "")
        + f"Quand : {_format_when(start_ts, end_ts, all_day)}\n"
        + (f"Ou : {location}\n" if location else "")
        + (f"Organisateur : {organizer}\n" if organizer else "")
        + (
            f"Participants : {', '.join(attendees)}\n"
            if attendees
            else "Aucun participant : ce n'est pas une reunion.\n"
        )
        + (f"Visio : {event.get('hangoutLink')}\n" if event.get("hangoutLink") else "")
        + (f"\n{description}" if description else "")
    )

    return {
        "source": "event",
        "external_id": f"{calendar_id}:{event['id']}",
        "title": title,
        "author": organizer,
        "ts": start_ts,
        "url": event.get("htmlLink"),
        "body": indexed,
        "meta": {
            "calendar_id": calendar_id,
            "calendar_name": calendar_name,
            "start_ts": start_ts,
            "end_ts": end_ts,
            "all_day": all_day,
            "location": location,
            "attendees": attendees,
            "hangout": event.get("hangoutLink"),
            "status": event.get("status"),
        },
        "content_hash": hashlib.sha256(
            (indexed + str(event.get("updated", ""))).encode("utf-8")
        ).hexdigest(),
    }


COLORS_META_KEY = "calendar_colors"


def resolve_calendars(
    service, configured: list[str], exclude: list[str]
) -> dict[str, tuple[str, str]]:
    """Retourne {identifiant d'agenda: (nom affiche, couleur)}.

    Le joker "*" prend tous les agendas accessibles au compte, y compris ceux ajoutes
    plus tard ; `exclude` ecarte les agendas d'abonnement sans interet. Nom et couleur
    sont recuperes ici parce qu'un evenement seul ne les porte pas.
    """
    catalogue: dict[str, tuple[str, str]] = {}
    page_token: str | None = None
    while True:
        resp = execute_with_retry(
            service.calendarList().list(pageToken=page_token, maxResults=250)
        )
        for item in resp.get("items", []):
            if item.get("deleted"):
                continue
            nom = item.get("summaryOverride") or item.get("summary") or ""
            catalogue[item["id"]] = (nom, item.get("backgroundColor") or "")
        page_token = resp.get("nextPageToken")
        if not page_token:
            break

    if "*" in configured:
        voulus = [cid for cid in catalogue if not _excluded(cid, catalogue[cid][0], exclude)]
        voulus += [c for c in configured if c != "*" and c not in catalogue]
    else:
        voulus = [
            c for c in configured if not _excluded(c, catalogue.get(c, ("", ""))[0], exclude)
        ]

    return {cid: catalogue.get(cid, ("", "")) for cid in voulus}


def calendar_colors(conn: sqlite3.Connection) -> dict[str, str]:
    """{identifiant d'agenda: couleur}, telle que Google l'affiche."""
    return json.loads(db.get_meta(conn, COLORS_META_KEY) or "{}")


def _normalise(texte: str) -> str:
    """Minuscules sans accents, pour que "Fetes des Prenoms" trouve "Fêtes des Prénoms"."""
    sans_accents = unicodedata.normalize("NFKD", texte or "")
    return "".join(c for c in sans_accents if not unicodedata.combining(c)).casefold()


def _excluded(calendar_id: str, calendar_name: str, patterns: list[str]) -> bool:
    """Un motif exclut un agenda s'il apparait dans son identifiant OU dans son nom.

    Les identifiants d'abonnement sont illisibles ; pouvoir ecrire le nom affiche
    rend config.yaml utilisable sans aller chercher un identifiant Google.
    """
    cible = f"{_normalise(calendar_id)} {_normalise(calendar_name)}"
    return any(_normalise(p) in cible for p in patterns if p.strip())


def sync(conn: sqlite3.Connection, cfg: Config, **_: object) -> list[int]:
    """Synchronise la fenetre d'agenda configuree. Retourne les doc_id modifies."""
    service = calendar_service(cfg.paths.client_secret_path, cfg.paths.token_path)

    now = datetime.now(timezone.utc)
    time_min = (now - timedelta(days=cfg.calendar.past_days)).isoformat()
    time_max = (now + timedelta(days=cfg.calendar.future_days)).isoformat()

    agendas = resolve_calendars(
        service, cfg.calendar.calendar_ids, cfg.calendar.exclude_patterns
    )

    # Les couleurs sont rangees a part, pas dans chaque evenement : les changer ne
    # doit pas forcer le recalcul des embeddings de centaines d'evenements.
    db.set_meta(
        conn, COLORS_META_KEY, json.dumps({cid: col for cid, (_, col) in agendas.items()})
    )
    conn.commit()

    changed: list[int] = []
    for calendar_id, (calendar_name, _) in agendas.items():
        page_token: str | None = None
        try:
            while True:
                resp = execute_with_retry(
                    service.events().list(
                        calendarId=calendar_id,
                        timeMin=time_min,
                        timeMax=time_max,
                        singleEvents=True,  # developpe les recurrences en occurrences
                        orderBy="startTime",
                        maxResults=250,
                        pageToken=page_token,
                    )
                )
                for event in resp.get("items", []):
                    doc = event_to_document(event, calendar_id, calendar_name)
                    if doc is None:
                        continue
                    doc_id, modified = db.upsert_document(conn, **doc)
                    if modified:
                        changed.append(doc_id)
                page_token = resp.get("nextPageToken")
                if not page_token:
                    break
        except HttpError:
            # Un agenda partage peut devenir illisible : les autres doivent passer quand meme.
            continue

    conn.commit()
    return changed


def between(conn: sqlite3.Connection, start_ts: int, end_ts: int) -> list[sqlite3.Row]:
    """Evenements qui commencent dans [start_ts, end_ts), par ordre chronologique."""
    return db.iter_rows(
        conn,
        "SELECT * FROM documents WHERE source = 'event' AND ts >= ? AND ts < ? ORDER BY ts ASC",
        (start_ts, end_ts),
    )


def upcoming(conn: sqlite3.Connection, *, days: int = 7, limit: int = 20) -> list[sqlite3.Row]:
    now = int(datetime.now(timezone.utc).timestamp())
    until = now + days * 86400
    return db.iter_rows(
        conn,
        "SELECT * FROM documents WHERE source = 'event' AND ts BETWEEN ? AND ?"
        " ORDER BY ts ASC LIMIT ?",
        (now - 3600, until, limit),
    )
