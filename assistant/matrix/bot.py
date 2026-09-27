"""Bot Matrix : l'assistant repond dans Element, chiffre de bout en bout.

Le bot ouvre une connexion SORTANTE vers le serveur Matrix (synchro longue) : aucun
port n'est ouvert sur le PC, l'interface web reste sur 127.0.0.1. Le serveur ne voit
que des messages chiffres, mais il voit qui parle a qui, quand, et leur taille.

Qui a le droit de lire les reponses ? Seuls les appareils des `allowed_users` signes
par leur identite, et cette identite est epinglee au premier contact. Un appareil
ajoute par un tiers au compte (serveur compromis, mot de passe vole) n'est pas signe :
il ne recoit pas les cles et ses messages sont ignores.
"""

from __future__ import annotations

import asyncio
import json
import logging
import secrets
import threading
import time
from contextlib import asynccontextmanager
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, AsyncIterator

from nio import (
    AsyncClient,
    AsyncClientConfig,
    DiscoveryInfoResponse,
    InviteMemberEvent,
    LoginResponse,
    MatrixInvitedRoom,
    MatrixRoom,
    MegolmEvent,
    RoomMessageText,
    RoomSendError,
    SyncResponse,
)
from nio.exceptions import EncryptionError, OlmUnverifiedDeviceError
from nio.store import SqliteStore

from .. import agent, db, notify, triage
from ..config import Config
from ..llm import LLM, OllamaError
from . import crosssign, render

log = logging.getLogger("assistant.matrix")

# Un message recu pendant que le PC etait eteint recoit une reponse au demarrage,
# sauf s'il date de plus d'une heure : la question n'a sans doute plus d'objet.
MAX_AGE_S = 3600
# Les cles d'un message arrivent parfois une synchro apres lui.
KEY_WAIT_S = 15
TYPING_REFRESH_S = 20
RETRY_DELAY_S = 60
NEW_CONVERSATION_CMD = "!nouveau"
# Resumes de mails : frequence de relecture de la base, et nombre de mails arrives
# ensemble au-dela duquel ils partent en un seul message.
MAIL_POLL_S = 15
DIGEST_ABOVE = 3
MAIL_SINCE_KEY = "matrix_mail_since"
NOTICE_ROOM_KEY = "matrix_notice_room"


class MatrixError(RuntimeError):
    pass


def _reply_target(event: RoomMessageText) -> str | None:
    """event_id du message auquel celui-ci repond, s'il s'agit d'une reponse."""
    relates = (event.source.get("content") or {}).get("m.relates_to") or {}
    return (relates.get("m.in_reply_to") or {}).get("event_id")


def _strip_reply_fallback(body: str) -> str:
    """Retire la citation "> ..." que les anciens clients placent en tete d'une reponse."""
    lines = body.split("\n")
    start = 0
    while start < len(lines) and lines[start].startswith(">"):
        start += 1
    return "\n".join(lines[start:]).strip()


def setup_logging() -> None:
    """Affiche l'activite du bot dans la console (uvicorn ne configure que ses loggers)."""
    if not log.handlers:
        handler = logging.StreamHandler()
        handler.setFormatter(logging.Formatter("%(asctime)s %(message)s", "%H:%M:%S"))
        log.addHandler(handler)
        log.setLevel(logging.INFO)
        log.propagate = False


# ------------------------------------------------------------------ session


@dataclass
class Session:
    homeserver: str
    user_id: str
    device_id: str
    access_token: str
    # Chiffre les cles Olm/Megolm dans la base locale de matrix-nio.
    pickle_key: str
    # Graines des cles de signature croisee du bot (voir crosssign).
    cross_signing: dict[str, str] = field(default_factory=dict)
    # Utilisateur -> cle maitresse vue au premier contact.
    pinned: dict[str, str] = field(default_factory=dict)


def _session_file(cfg: Config) -> Path:
    return cfg.paths.matrix_path / "session.json"


def load_session(cfg: Config) -> Session | None:
    path = _session_file(cfg)
    if not path.exists():
        return None
    return Session(**json.loads(path.read_text(encoding="utf-8")))


def save_session(cfg: Config, session: Session) -> None:
    path = _session_file(cfg)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(asdict(session), indent=2), encoding="utf-8")
    tmp.replace(path)  # jamais de fichier a moitie ecrit, qui ferait perdre le jeton


def _client(cfg: Config, homeserver: str, pickle_key: str, device_id: str = "") -> AsyncClient:
    store = cfg.paths.matrix_path / "store"
    store.mkdir(parents=True, exist_ok=True)
    return AsyncClient(
        homeserver,
        cfg.matrix.user_id,
        device_id=device_id,
        store_path=str(store),
        config=AsyncClientConfig(
            # Le stockage par defaut nomme ses fichiers "@bot:serveur_APPAREIL..." : sous
            # Windows, ce ":" designe un flux NTFS cache et l'ecriture des etats de
            # confiance echoue. SqliteStore garde tout, confiance comprise, dans un seul
            # fichier au nom fixe (il distingue les appareils en interne).
            store=SqliteStore,
            store_name="nio.db",
            encryption_enabled=True,
            store_sync_tokens=True,
            pickle_key=pickle_key,
        ),
    )


def forget_identity(cfg: Config, user_id: str) -> bool:
    """Accepte la nouvelle identite d'un interlocuteur (elle sera epinglee au prochain message)."""
    session = load_session(cfg)
    if session is None or session.pinned.pop(user_id, None) is None:
        return False
    save_session(cfg, session)
    return True


# -------------------------------------------------------------------- login


async def _resolve_homeserver(cfg: Config) -> str:
    """URL de l'API client, via .well-known : matrix.org la sert ailleurs que sur matrix.org."""
    base = cfg.matrix.homeserver or "https://" + cfg.matrix.user_id.split(":", 1)[-1]
    probe = AsyncClient(base)
    try:
        resp = await probe.discovery_info()
    finally:
        await probe.close()
    return resp.homeserver_url.rstrip("/") if isinstance(resp, DiscoveryInfoResponse) else base


async def login(cfg: Config, password: str) -> Session:
    """Connecte le compte du bot, publie ses cles et signe son appareil.

    Seule etape qui demande le mot de passe ; il n'est jamais enregistre.
    """
    if not cfg.matrix.user_id.startswith("@") or ":" not in cfg.matrix.user_id:
        raise MatrixError("matrix.user_id doit etre de la forme @nom:serveur dans config.yaml")

    old = load_session(cfg)
    homeserver = await _resolve_homeserver(cfg)
    # Meme cle d'une connexion a l'autre : tous les appareils partagent nio.db.
    pickle_key = old.pickle_key if old else secrets.token_urlsafe(32)
    client = _client(cfg, homeserver, pickle_key)
    try:
        resp = await client.login(password, device_name=cfg.matrix.device_name)
        if not isinstance(resp, LoginResponse):
            raise MatrixError(f"connexion refusee par {homeserver} : {resp.message}")

        session = Session(
            homeserver=homeserver,
            user_id=resp.user_id,
            device_id=resp.device_id,
            access_token=resp.access_token,
            pickle_key=pickle_key,
            pinned=old.pinned if old else {},
        )
        # Enregistree tout de suite : si la suite echoue, relancer la commande
        # remplacera proprement cet appareil au lieu d'en laisser un orphelin.
        save_session(cfg, session)

        if client.should_upload_keys:
            await client.keys_upload()
        session.cross_signing = await crosssign.ensure_identity(
            client, old.cross_signing if old else None, password=password
        )
        save_session(cfg, session)

        if old and old.device_id != session.device_id:
            # Deconnecte l'appareil precedent du bot, qui ne servirait plus. Sans
            # importance s'il l'etait deja.
            try:
                await client.send(
                    "POST",
                    "/_matrix/client/v3/logout",
                    "{}",
                    headers={"Authorization": f"Bearer {old.access_token}"},
                )
            except Exception as exc:
                log.warning("Matrix : ancien appareil %s non deconnecte : %s", old.device_id, exc)
        return session
    finally:
        await client.close()


# ---------------------------------------------------------------------- bot


class Bot:
    def __init__(self, cfg: Config, llm: LLM, session: Session) -> None:
        self.cfg = cfg
        self.llm = llm
        self.session = session
        self.allowed = set(cfg.matrix.allowed_users)
        self.client = _client(cfg, session.homeserver, session.pickle_key, session.device_id)
        self.client.restore_login(session.user_id, session.device_id, session.access_token)
        self.queue: asyncio.Queue[tuple[MatrixRoom, RoomMessageText | MegolmEvent]] = asyncio.Queue()
        # Utilisateur -> {device_id: cle ed25519} des appareils autorises a lire.
        self.trusted: dict[str, dict[str, str]] = {}
        self._warned: set[str] = set()
        self._synced = asyncio.Event()

    async def run(self) -> None:
        c = self.client
        if c.should_upload_keys:
            await c.keys_upload()
        self.session.cross_signing = await crosssign.ensure_identity(c, self.session.cross_signing)
        save_session(self.cfg, self.session)

        c.add_event_callback(self._on_invite, InviteMemberEvent)
        c.add_event_callback(self._on_message, (RoomMessageText, MegolmEvent))
        c.add_response_callback(self._on_sync, SyncResponse)
        tasks = [asyncio.create_task(self._worker())]
        if self.cfg.matrix.mail_notices:
            tasks.append(asyncio.create_task(self._mail_loop()))
        log.info("Matrix : %s connecte (appareil %s)", c.user_id, c.device_id)
        try:
            await c.sync_forever(timeout=30_000, full_state=True)
        finally:
            for task in tasks:
                task.cancel()
            await c.close()

    async def _on_sync(self, response: SyncResponse) -> None:
        self._synced.set()

    # ------------------------------------------------------------ evenements
    # Les callbacks s'executent dans la boucle de synchro : ils ne font que trier et
    # mettre en file. La reponse du modele (plusieurs secondes) se fait a cote.

    async def _on_invite(self, room: MatrixInvitedRoom, event: InviteMemberEvent) -> None:
        if event.membership != "invite" or event.state_key != self.client.user_id:
            return
        if event.sender in self.allowed:
            await self.client.join(room.room_id)
            log.info("Matrix : invitation de %s acceptee", event.sender)
        else:
            await self.client.room_leave(room.room_id)
            log.warning("Matrix : invitation de %s refusee (hors allowed_users)", event.sender)

    async def _on_message(self, room: MatrixRoom, event: RoomMessageText | MegolmEvent) -> None:
        if event.sender == self.client.user_id or event.sender not in self.allowed:
            return
        if time.time() * 1000 - event.server_timestamp > MAX_AGE_S * 1000:
            return
        await self.queue.put((room, event))

    async def _worker(self) -> None:
        while True:
            room, event = await self.queue.get()
            try:
                await self._handle(room, event)
            except Exception:
                log.exception("Matrix : echec du traitement d'un message")

    # ------------------------------------------------------------- traitement

    async def _handle(self, room: MatrixRoom, event: RoomMessageText | MegolmEvent) -> None:
        c = self.client
        await c.room_read_markers(room.room_id, event.event_id, event.event_id)

        if not room.encrypted:
            await self._send(room, "Ce salon n'est pas chiffré : je n'y réponds pas. "
                             "Ouvre une discussion privée chiffrée avec moi.", reply_to=event)
            return
        if set(room.users) - self.allowed - {c.user_id}:
            await self._send(room, "Je ne réponds que dans un salon où nous sommes seuls.",
                             reply_to=event)
            return

        if isinstance(event, MegolmEvent):
            decrypted = await self._decrypt_late(room, event)
            if not isinstance(decrypted, RoomMessageText):
                log.warning("Matrix : message de %s indechiffrable", event.sender)
                await self._send(room, "Je n'ai pas pu déchiffrer ce message : Element ne m'a pas"
                                 " transmis ses clés. Renvoie-le ; si ça persiste, vérifie que"
                                 " ta session Element est vérifiée.", reply_to=event)
                return
            event = decrypted

        await self._refresh_trust(room)
        if not self._from_trusted_device(event):
            self._warn_once(
                f"device:{event.sender_key}",
                f"message de {event.sender} ignore : il vient d'un appareil que son identite"
                " n'a pas signe (session Element non verifiee ?)",
            )
            return

        # Les resumes de mails iront dans la derniere discussion ou tu as ecrit.
        await asyncio.to_thread(self._remember_room, room.room_id)

        replied = _reply_target(event)
        question = _strip_reply_fallback(event.body) if replied else event.body.strip()
        if question == NEW_CONVERSATION_CMD:
            await asyncio.to_thread(self._new_conversation, room.room_id)
            await self._send(room, "Nouvelle conversation : je repars de zéro.", reply_to=event)
            return
        # Une reponse a un resume de mail porte sur ce mail, comme le bouton
        # « Que dois-je en faire ? » de l'interface web.
        focus = await asyncio.to_thread(self._notice_doc, replied) if replied else None

        async with self._typing(room.room_id):
            try:
                answer = await asyncio.to_thread(self._answer, room.room_id, question, focus)
            except OllamaError as exc:
                await self._send(room, f"Le modèle local ne répond pas : {exc}", reply_to=event)
                return
        await self._send(room, answer.text, answer.citations, reply_to=event)

    async def _decrypt_late(self, room: MatrixRoom, event: MegolmEvent) -> Any:
        for _ in range(KEY_WAIT_S):
            try:
                return self.client.olm.decrypt_megolm_event(event, room.room_id)
            except EncryptionError:
                await asyncio.sleep(1)
        return None

    # --------------------------------------------------------------- confiance

    async def _refresh_trust(self, room: MatrixRoom) -> None:
        """Marque chaque appareil du salon : verifie s'il est signe, bloque sinon.

        matrix-nio ne partage les cles qu'avec les appareils verifies, et refuse
        d'envoyer (OlmUnverifiedDeviceError) tant qu'un appareil n'est ni l'un ni l'autre.
        """
        c = self.client
        if not room.members_synced:
            await c.joined_members(room.room_id)
        if c.should_query_keys:
            await c.keys_query()

        data = await crosssign.query_keys(c, sorted(self.allowed))
        # Relu a chaque fois : `assistant matrix-trust` peut l'avoir modifie.
        stored = load_session(self.cfg)
        if stored is not None:
            self.session.pinned = stored.pinned
        for user in self.allowed:
            self.trusted[user] = self._check_identity(user, crosssign.read_identity(data, user))

        for user in room.users:
            for device in c.device_store.active_user_devices(user):
                if user == c.user_id and device.id == c.device_id:
                    continue
                if self.trusted.get(user, {}).get(device.id) == device.ed25519:
                    if not device.verified:
                        c.verify_device(device)
                elif not device.blacklisted:
                    c.blacklist_device(device)

    def _check_identity(self, user: str, identity: crosssign.Identity) -> dict[str, str]:
        pinned = self.session.pinned.get(user)
        if identity.master is None:
            self._warn_once(
                f"noid:{user}",
                f"{user} n'a pas d'identite de signature croisee : verifie ta session"
                " dans Element (Parametres > Securite).",
            )
            return {}
        if pinned is None:
            self.session.pinned[user] = identity.master
            save_session(self.cfg, self.session)
            log.info("Matrix : identite de %s epinglee (%s)", user, identity.master)
        elif pinned != identity.master:
            self._warn_once(
                f"changed:{user}:{identity.master}",
                f"l'identite Matrix de {user} a change. Je ne lui reponds plus. Si c'est"
                f" toi qui l'as reinitialisee : assistant matrix-trust {user}",
                toast=True,
            )
            return {}
        return identity.devices

    def _from_trusted_device(self, event: RoomMessageText) -> bool:
        if not event.decrypted or not event.sender_key:
            return False
        device = self.client.device_store.device_from_sender_key(event.sender, event.sender_key)
        return device is not None and device.verified

    def _warn_once(self, key: str, message: str, *, toast: bool = False) -> None:
        if key in self._warned:
            return
        self._warned.add(key)
        log.warning("Matrix : %s", message)
        if toast:
            notify.toast("Assistant - Matrix", message)

    # ----------------------------------------------------------------- envoi

    async def _send(
        self,
        room: MatrixRoom,
        text: str,
        citations: list[dict[str, Any]] | None = None,
        *,
        reply_to: RoomMessageText | MegolmEvent | None = None,
    ) -> str | None:
        citations = citations or []
        content: dict[str, Any] = {
            "msgtype": "m.text",
            "body": render.to_text(text, citations),
            "format": "org.matrix.custom.html",
            "formatted_body": render.to_html(text, citations),
        }
        if reply_to is not None:
            content["m.relates_to"] = {"m.in_reply_to": {"event_id": reply_to.event_id}}
        return await self._send_content(room, content)

    async def _send_content(self, room: MatrixRoom, content: dict[str, Any]) -> str | None:
        """Envoie un message ; retourne son event_id, None si le serveur l'a refuse."""
        for attempt in range(2):
            if room.encrypted:
                await self._refresh_trust(room)
            try:
                resp = await self.client.room_send(
                    room.room_id, "m.room.message", content, ignore_unverified_devices=False
                )
            except OlmUnverifiedDeviceError:
                # Un appareil est apparu entre la verification et l'envoi.
                if attempt:
                    raise
                continue
            if isinstance(resp, RoomSendError):
                log.warning("Matrix : envoi refuse : %s", resp.message)
                return None
            return resp.event_id
        return None

    @asynccontextmanager
    async def _typing(self, room_id: str) -> AsyncIterator[None]:
        async def keep() -> None:
            while True:
                await self.client.room_typing(room_id, True, timeout=(TYPING_REFRESH_S + 10) * 1000)
                await asyncio.sleep(TYPING_REFRESH_S)

        task = asyncio.create_task(keep())
        try:
            yield
        finally:
            task.cancel()
            await self.client.room_typing(room_id, False)

    # ------------------------------------------------- conversations (threads)
    # Chaque salon Matrix correspond a une conversation de l'interface web, qui y
    # apparait comme un onglet : l'historique est le meme des deux cotes.

    def _conn(self):
        return db.thread_connection(self.cfg.paths.db_path, embed_dim=self.cfg.ollama.embed_dim)

    def _conversation(self, room_id: str, first_text: str) -> int:
        """Conversation du salon ; `first_text` la titre si elle est creee maintenant."""
        conn = self._conn()
        key = f"matrix_room:{room_id}"
        stored = db.get_meta(conn, key)
        if stored and conn.execute(
            "SELECT 1 FROM conversations WHERE id = ?", (int(stored),)
        ).fetchone():
            return int(stored)
        # Pas encore de conversation, ou supprimee depuis l'interface web.
        titre = "Matrix · " + agent.title_from_question(first_text, max_chars=31)
        cid = agent.create_conversation(conn, titre)
        db.set_meta(conn, key, str(cid))
        conn.commit()
        return cid

    def _new_conversation(self, room_id: str) -> None:
        conn = self._conn()
        conn.execute("DELETE FROM meta WHERE key = ?", (f"matrix_room:{room_id}",))
        conn.commit()

    def _answer(
        self, room_id: str, question: str, focus_doc: int | None = None
    ) -> agent.AgentAnswer:
        conn = self._conn()
        cid = self._conversation(room_id, question)
        history = agent.model_history(conn, cid)
        agent.save_message(conn, "user", question, conversation_id=cid)
        answer = agent.ask(
            conn, self.llm, self.cfg, question, history=history, focus_doc=focus_doc
        )
        # L'onglet a pu etre ferme dans l'interface pendant que le modele repondait.
        if conn.execute("SELECT 1 FROM conversations WHERE id = ?", (cid,)).fetchone():
            agent.save_message(conn, "assistant", answer.text, answer.citations, conversation_id=cid)
        return answer

    # ------------------------------------------------------- resumes de mails
    # Le tri (synchro de fond) remplit mail_triage ; le bot se contente de relire la
    # base. Il marche donc quel que soit le processus qui synchronise.

    async def _mail_loop(self) -> None:
        await self._synced.wait()  # sans premiere synchro, le bot ne connait aucun salon
        while True:
            try:
                await self._send_mail_notices()
            except Exception:
                log.exception("Matrix : echec de l'envoi des resumes de mails")
            await asyncio.sleep(MAIL_POLL_S)

    async def _send_mail_notices(self) -> None:
        mails, remembered = await asyncio.to_thread(self._pending_mails)
        if not mails:
            return
        room = self._notice_room(remembered)
        if room is None:
            self._warn_once(
                "noroom",
                "aucune discussion privee avec le bot : ecris-lui une premiere fois pour"
                " recevoir les resumes de mails",
            )
            return
        await self._refresh_trust(room)
        if not any(self.trusted.get(user) for user in room.users if user in self.allowed):
            return  # aucun appareil de confiance pour les lire : on reessaiera plus tard

        # Au reveil du PC, plusieurs mails arrivent d'un coup : un seul message.
        batches = [mails] if len(mails) > DIGEST_ABOVE else [[mail] for mail in mails]
        for batch in batches:
            text, html = render.mail_digest(batch) if len(batch) > 1 else render.mail_notice(batch[0])
            ping = max(m["urgency"] for m in batch) >= self.cfg.matrix.mail_ping_min_urgency
            content = {
                # Les regles de notification par defaut ne font pas sonner un m.notice :
                # le resume s'affiche, le telephone reste silencieux.
                "msgtype": "m.text" if ping else "m.notice",
                "body": text,
                "format": "org.matrix.custom.html",
                "formatted_body": html,
            }
            event_id = await self._send_content(room, content)
            if event_id:
                await asyncio.to_thread(self._record_notices, batch, room.room_id, event_id)

    def _pending_mails(self) -> tuple[list[dict[str, Any]], str | None]:
        conn = self._conn()
        since = db.get_meta(conn, MAIL_SINCE_KEY)
        if since is None:
            # Premiere mise en route : les mails deja presents ne sont pas envoyes.
            since = str(int(time.time()))
            db.set_meta(conn, MAIL_SINCE_KEY, since)
            conn.commit()
        # Meme fenetre que les notifications Windows : un mail rattrape apres une longue
        # coupure n'a plus rien d'une nouvelle.
        oldest = max(int(since), int(time.time()) - triage.NOTIFY_MAX_AGE_HOURS * 3600)
        rows = conn.execute(
            "SELECT d.id, d.title, d.author, d.url, d.ts, t.category, t.urgency, t.action_type,"
            "       t.summary, t.action"
            " FROM mail_triage t JOIN documents d ON d.id = t.doc_id"
            " LEFT JOIN matrix_mail_notices n ON n.doc_id = t.doc_id"
            " WHERE n.doc_id IS NULL AND d.ts >= ?"
            "   AND COALESCE(json_extract(d.meta, '$.folder'), 'inbox') = 'inbox'"
            # Deja lu ailleurs (Gmail sur le telephone) : plus rien a annoncer.
            "   AND json_extract(d.meta, '$.unread') = 1"
            " ORDER BY d.ts LIMIT 50",
            (oldest,),
        ).fetchall()
        return [dict(r) for r in rows], db.get_meta(conn, NOTICE_ROOM_KEY)

    def _record_notices(self, mails: list[dict[str, Any]], room_id: str, event_id: str) -> None:
        conn = self._conn()
        now = int(time.time())
        conn.executemany(
            "INSERT OR IGNORE INTO matrix_mail_notices(doc_id, room_id, event_id, sent_at)"
            " VALUES(?, ?, ?, ?)",
            [(m["id"], room_id, event_id, now) for m in mails],
        )
        conn.commit()
        # Aussi dans la conversation du salon, pour que l'onglet de l'interface web montre
        # la meme chose qu'Element. Le mail y est une source : un clic l'ouvre.
        cid = self._conversation(room_id, "Mails")
        citations = [
            {
                "doc_id": m["id"],
                "source": "mail",
                "titre": m["title"] or "(sans objet)",
                "date": datetime.fromtimestamp(m["ts"]).astimezone().strftime("%d/%m/%Y %H:%M")
                if m["ts"]
                else "",
                "url": m["url"],
            }
            for m in mails
        ]
        agent.save_message(
            conn, "notice", render.notice_markdown(mails), citations, conversation_id=cid
        )

    def _notice_doc(self, event_id: str) -> int | None:
        """Mail resume par ce message ; None pour un resume groupe (lequel choisir ?)."""
        rows = self._conn().execute(
            "SELECT doc_id FROM matrix_mail_notices WHERE event_id = ?", (event_id,)
        ).fetchall()
        return rows[0]["doc_id"] if len(rows) == 1 else None

    def _remember_room(self, room_id: str) -> None:
        conn = self._conn()
        if db.get_meta(conn, NOTICE_ROOM_KEY) != room_id:
            db.set_meta(conn, NOTICE_ROOM_KEY, room_id)
            conn.commit()

    def _is_private(self, room: MatrixRoom) -> bool:
        others = set(room.users) - {self.client.user_id}
        return bool(room.encrypted and others and others <= self.allowed)

    def _notice_room(self, remembered: str | None) -> MatrixRoom | None:
        """La discussion ou tu as ecrit au bot en dernier, sinon n'importe laquelle a deux."""
        rooms = self.client.rooms
        if remembered in rooms and self._is_private(rooms[remembered]):
            return rooms[remembered]
        return next((r for r in rooms.values() if self._is_private(r)), None)


# --------------------------------------------------------------- lancement


async def run_forever(cfg: Config, llm: LLM) -> None:
    """Fait tourner le bot et le relance apres une erreur (veille du PC, coupure reseau)."""
    while True:
        session = load_session(cfg)
        if session is None:
            raise MatrixError("aucune session Matrix : lance d'abord `assistant matrix-login`")
        try:
            await Bot(cfg, llm, session).run()
        except (MatrixError, crosssign.CrossSigningError):
            raise  # probleme de configuration : relancer ne changerait rien
        except Exception:
            log.exception("Matrix : erreur, nouvel essai dans %s s", RETRY_DELAY_S)
        await asyncio.sleep(RETRY_DELAY_S)


class MatrixThread(threading.Thread):
    """Le bot dans un thread a part, avec sa propre boucle asyncio, a cote de l'interface web."""

    def __init__(self, cfg: Config, llm: LLM) -> None:
        super().__init__(name="matrix-bot", daemon=True)
        self.cfg = cfg
        self.llm = llm
        self._loop: asyncio.AbstractEventLoop | None = None
        self._task: asyncio.Task[None] | None = None

    def run(self) -> None:
        self._loop = asyncio.new_event_loop()
        self._task = self._loop.create_task(run_forever(self.cfg, self.llm))
        try:
            self._loop.run_until_complete(self._task)
        except asyncio.CancelledError:
            pass
        except Exception as exc:
            log.error("Matrix : bot arrete : %s", exc)
        finally:
            self._loop.close()

    def stop(self) -> None:
        if self._loop and self._task and not self._loop.is_closed():
            self._loop.call_soon_threadsafe(self._task.cancel)
