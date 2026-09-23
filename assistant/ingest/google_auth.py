"""Authentification Google en lecture seule (OAuth "application installee").

Le jeton est ecrit hors du depot (voir paths.google_token) et ne sert qu'a interroger
Google depuis cette machine. Aucun contenu de mail ou d'agenda n'est envoye ailleurs.
"""

from __future__ import annotations

import json
import random
import time
from pathlib import Path
from typing import Any

from google.auth.exceptions import RefreshError
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

# Lecture seule, volontairement : l'assistant ne peut ni envoyer, ni supprimer,
# ni modifier quoi que ce soit dans ton compte.
SCOPES = [
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/calendar.readonly",
]


class ReauthRequired(RuntimeError):
    """Le jeton n'est plus valide et seul l'utilisateur peut en obtenir un nouveau."""


SETUP_HELP = """\
Identifiants OAuth introuvables : {path}

A faire une seule fois, dans la console Google (gratuit) :
  1. https://console.cloud.google.com/projectcreate  -> cree un projet (nom libre)
  2. "APIs & Services" > "Enabled APIs" > "+ ENABLE APIS AND SERVICES"
     -> active "Gmail API" puis "Google Calendar API"
  3. "Google Auth Platform" > "Audience"  (anciennement "OAuth consent screen")
     -> type "External"
     -> IMPORTANT : dans "Test users", ajoute TON adresse Gmail.
        Sans cela, Google refuse l'autorisation avec "Erreur 403 : access_denied".
  4. "Google Auth Platform" > "Clients" > "+ CREATE CLIENT"
     -> type "Desktop app"
  5. Telecharge le JSON et enregistre-le ici : {path}

Puis relance :  assistant auth
"""

NO_TOKEN_HELP = """\
Aucun compte Google n'est connecte.

Lance :  assistant auth
"""

REAUTH_HELP = """\
Le jeton Google n'est plus valide : Google l'a revoque ou il a expire.

Cause la plus frequente : tant que l'application reste en statut "Testing" dans la
console Google, Google revoque les jetons au bout de 7 jours. C'est une limite de
Google, pas du programme.

Relance simplement :  assistant auth

Pour ne plus avoir a le refaire, passe l'application en "In production"
("Google Auth Platform" > "Audience" > "Publish app"). Un ecran d'avertissement
"application non verifiee" apparaitra a la connexion : c'est normal pour un usage
personnel, et le jeton cesse d'expirer.
"""


def _save(creds: Credentials, token_path: Path) -> None:
    token_path.parent.mkdir(parents=True, exist_ok=True)
    token_path.write_text(creds.to_json(), encoding="utf-8")


def get_credentials(
    client_secret: Path, token_path: Path, *, interactive: bool = False
) -> Credentials:
    """Charge le jeton local, le rafraichit, ou relance le consentement.

    `interactive` n'est vrai que pour la commande `assistant auth` : une synchro de
    fond ne doit jamais ouvrir un navigateur sans prevenir.
    """
    creds: Credentials | None = None
    if token_path.exists():
        try:
            creds = Credentials.from_authorized_user_file(str(token_path), SCOPES)
        except ValueError:
            creds = None  # fichier illisible, ou scopes differents de ceux demandes
    had_token = creds is not None

    if creds and creds.valid:
        return creds

    if creds and creds.refresh_token:
        try:
            creds.refresh(Request())
            _save(creds, token_path)
            return creds
        except RefreshError:
            # Jeton revoque par Google (statut "Testing" : 7 jours) ou par l'utilisateur.
            creds = None

    if not client_secret.exists():
        raise FileNotFoundError(SETUP_HELP.format(path=client_secret))

    if not interactive:
        # Distinguer "jamais connecte" de "Google a revoque le jeton" : les deux se
        # reglent avec `assistant auth`, mais la cause a expliquer n'est pas la meme.
        raise ReauthRequired(REAUTH_HELP if had_token else NO_TOKEN_HELP)

    flow = InstalledAppFlow.from_client_secrets_file(str(client_secret), SCOPES)
    # Le navigateur s'ouvre ; la redirection revient sur un serveur local temporaire.
    creds = flow.run_local_server(
        port=0,
        prompt="consent",
        authorization_prompt_message=(
            "Autorise l'acces en lecture dans le navigateur qui vient de s'ouvrir..."
        ),
        success_message="Compte connecte. Tu peux fermer cet onglet et revenir au terminal.",
    )
    _save(creds, token_path)
    return creds


# --------------------------------------------------- robustesse des appels API

# Google limite le debit par utilisateur. Un `messages.get` par mail, en boucle
# serree, depasse la limite au bout de quelques dizaines de requetes : on reessaie
# avec un delai qui double a chaque tentative, comme Google le recommande.
_RETRY_STATUS = {429, 500, 502, 503, 504}
_RETRY_REASONS = {"ratelimitexceeded", "userratelimitexceeded", "backenderror", "internalerror"}


def _error_reason(exc: HttpError) -> str:
    try:
        payload = json.loads(exc.content.decode("utf-8"))
        return (payload["error"]["errors"][0].get("reason") or "").lower()
    except Exception:
        return ""


def execute_with_retry(request: Any, *, max_attempts: int = 6, base_delay: float = 2.0) -> Any:
    """Execute une requete Google API en reessayant sur les erreurs temporaires."""
    for attempt in range(max_attempts):
        try:
            return request.execute()
        except HttpError as exc:
            status = getattr(exc.resp, "status", None)
            reason = _error_reason(exc)
            retryable = status in _RETRY_STATUS or (status == 403 and reason in _RETRY_REASONS)
            if not retryable or attempt == max_attempts - 1:
                raise
            # Delai exponentiel + bruit aleatoire, pour ne pas repartir tous en meme temps.
            time.sleep(base_delay * (2**attempt) + random.uniform(0, 1.0))
    raise RuntimeError("inatteignable")


def gmail_service(client_secret: Path, token_path: Path):
    creds = get_credentials(client_secret, token_path)
    return build("gmail", "v1", credentials=creds, cache_discovery=False)


def calendar_service(client_secret: Path, token_path: Path):
    creds = get_credentials(client_secret, token_path)
    return build("calendar", "v3", credentials=creds, cache_discovery=False)
