"""Signature croisee (cross-signing) : celle du bot, et la verification de celle des autres.

matrix-nio ne la gere pas. Or Element applique MSC4153 (« exclude insecure devices ») :
un appareil que son proprietaire n'a pas signe ne recoit plus les cles des salons, et
ses messages s'affichent sans contenu. Un bot matrix-nio brut devient donc muet et sourd.

Ce module fait le strict necessaire :

- donner au compte du bot sa propre identite (cle maitresse -> cle d'auto-signature)
  et signer avec elle l'appareil du bot ;
- dire, pour un interlocuteur, quels appareils sont signes par son identite, pour ne
  partager les cles qu'avec ceux-la et n'ecouter qu'eux.

Les signatures suivent la spec Matrix : Ed25519 sur le JSON canonique de l'objet,
sans ses champs `signatures` et `unsigned`, encodee en base64 sans remplissage.
"""

from __future__ import annotations

import base64
import json
import os
from dataclasses import dataclass, field
from typing import Any

import vodozemac
from Crypto.PublicKey import ECC
from Crypto.Signature import eddsa
from nio import AsyncClient
from nio.api import Api

ROLES = ("master", "self_signing", "user_signing")


class CrossSigningError(RuntimeError):
    pass


def _b64(data: bytes) -> str:
    return base64.b64encode(data).decode().rstrip("=")


def _unb64(text: str) -> bytes:
    return base64.b64decode(text + "=" * (-len(text) % 4))


def _signable(obj: dict[str, Any]) -> bytes:
    return Api.to_canonical_json(
        {k: v for k, v in obj.items() if k not in ("signatures", "unsigned")}
    ).encode()


@dataclass(frozen=True)
class SigningKey:
    seed: bytes

    @classmethod
    def generate(cls) -> SigningKey:
        return cls(os.urandom(32))

    @classmethod
    def from_b64(cls, text: str) -> SigningKey:
        return cls(_unb64(text))

    @property
    def seed_b64(self) -> str:
        return _b64(self.seed)

    @property
    def public(self) -> str:
        key = ECC.construct(curve="Ed25519", seed=self.seed)
        return _b64(key.public_key().export_key(format="raw"))

    def sign_into(self, obj: dict[str, Any], user_id: str) -> None:
        """Ajoute la signature de cette cle a `obj`, a cote de celles deja presentes."""
        key = ECC.construct(curve="Ed25519", seed=self.seed)
        signature = _b64(eddsa.new(key, "rfc8032").sign(_signable(obj)))
        obj.setdefault("signatures", {}).setdefault(user_id, {})[f"ed25519:{self.public}"] = signature


def is_signed_by(
    obj: dict[str, Any], user_id: str, public: str, key_id: str | None = None
) -> bool:
    """Vrai si `obj` porte une signature valide de la cle `public`.

    Une cle de signature croisee signe sous l'identifiant `ed25519:<cle publique>`,
    un appareil sous `ed25519:<device_id>` : d'ou `key_id`.
    """
    signature = (obj.get("signatures") or {}).get(user_id, {}).get(key_id or f"ed25519:{public}")
    if not signature:
        return False
    try:
        vodozemac.Ed25519PublicKey.from_base64(public).verify_signature(
            _signable(obj), vodozemac.Ed25519Signature.from_base64(signature)
        )
    except (vodozemac.SignatureException, vodozemac.KeyException, ValueError):
        return False
    return True


def _key_of(obj: dict[str, Any] | None, user_id: str, usage: str) -> str | None:
    """Cle publique d'un objet de cle de signature croisee, s'il est bien forme."""
    if not obj or obj.get("user_id") != user_id or usage not in (obj.get("usage") or []):
        return None
    keys = list((obj.get("keys") or {}).values())
    return keys[0] if len(keys) == 1 else None


# ------------------------------------------------------------------ serveur


async def _api(client: AsyncClient, path: str, body: dict[str, Any]) -> tuple[int, dict[str, Any]]:
    resp = await client.send(
        "POST",
        f"/_matrix/client/v3{path}",
        json.dumps(body),
        headers={
            "Authorization": f"Bearer {client.access_token}",
            "Content-Type": "application/json",
        },
    )
    try:
        data = await resp.json(content_type=None)
    except ValueError:
        data = {}
    return resp.status, data if isinstance(data, dict) else {}


async def query_keys(client: AsyncClient, user_ids: list[str]) -> dict[str, Any]:
    """Cles d'appareils ET de signature croisee (nio ne garde que les premieres)."""
    status, data = await _api(client, "/keys/query", {"device_keys": {u: [] for u in user_ids}})
    if status != 200:
        raise CrossSigningError(f"keys/query a repondu {status} : {data.get('error', data)}")
    return data


# ------------------------------------------------------------ identite des autres


@dataclass
class Identity:
    """Ce que le serveur annonce d'un utilisateur, apres verification des signatures."""

    master: str | None
    # device_id -> cle ed25519, pour les seuls appareils signes par l'identite.
    devices: dict[str, str] = field(default_factory=dict)


def read_identity(data: dict[str, Any], user_id: str) -> Identity:
    master = _key_of((data.get("master_keys") or {}).get(user_id), user_id, "master")
    ssk_obj = (data.get("self_signing_keys") or {}).get(user_id)
    ssk = _key_of(ssk_obj, user_id, "self_signing")
    identity = Identity(master=master)
    if not master or not ssk or not is_signed_by(ssk_obj, user_id, master):
        return identity

    for device_id, device in ((data.get("device_keys") or {}).get(user_id) or {}).items():
        if device.get("user_id") != user_id or device.get("device_id") != device_id:
            continue
        ed25519 = (device.get("keys") or {}).get(f"ed25519:{device_id}")
        # La signature de l'appareil par lui-meme lie sa cle ed25519 a sa cle curve25519 ;
        # celle de l'identite lie l'appareil a son proprietaire. Il faut les deux.
        if (
            ed25519
            and is_signed_by(device, user_id, ed25519, f"ed25519:{device_id}")
            and is_signed_by(device, user_id, ssk)
        ):
            identity.devices[device_id] = ed25519
    return identity


# ------------------------------------------------------------ identite du bot


def _generate() -> dict[str, SigningKey]:
    return {role: SigningKey.generate() for role in ROLES}


def _cross_signing_body(user_id: str, keys: dict[str, SigningKey]) -> dict[str, Any]:
    def key_obj(role: str) -> dict[str, Any]:
        pub = keys[role].public
        return {"user_id": user_id, "usage": [role], "keys": {f"ed25519:{pub}": pub}}

    body = {f"{role}_key": key_obj(role) for role in ROLES}
    keys["master"].sign_into(body["self_signing_key"], user_id)
    keys["master"].sign_into(body["user_signing_key"], user_id)
    return body


def _uia_hint(data: dict[str, Any]) -> str:
    """Explication lisible d'un refus d'authentification interactive."""
    for flow in data.get("flows") or []:
        for stage in flow.get("stages") or []:
            if stage == "m.login.password":
                return "le mot de passe a ete refuse"
            if stage in ("m.oauth", "org.matrix.cross_signing_reset"):
                # Serveur delegue a MAS (matrix.org depuis 2025) : le remplacement de
                # l'identite doit etre approuve dans le navigateur, mot de passe ou non.
                # m.oauth est le nom stable (MSC4312), l'autre l'ancien.
                url = ((data.get("params") or {}).get(stage) or {}).get("url")
                where = f" : {url}" if url else " (parametres du compte, « Reinitialiser l'identite »)"
                return (
                    "le serveur exige d'approuver le remplacement de l'identite dans le"
                    f" navigateur{where}. Connecte-toi AVEC LE COMPTE DU BOT, approuve, puis"
                    " relance `assistant matrix-login` dans les 10 minutes"
                )
    return data.get("error") or "authentification refusee"


async def _upload_identity(
    client: AsyncClient, keys: dict[str, SigningKey], password: str | None
) -> None:
    body = _cross_signing_body(client.user_id, keys)
    status, data = await _api(client, "/keys/device_signing/upload", body)
    # Premiere identite du compte : pas d'authentification demandee (MSC3967).
    # La remplacer en exige une, le mot de passe si le serveur l'accepte.
    if status == 401 and password and data.get("session"):
        stages = {s for f in data.get("flows") or [] for s in f.get("stages") or []}
        if "m.login.password" in stages:
            body["auth"] = {
                "type": "m.login.password",
                "identifier": {"type": "m.id.user", "user": client.user_id},
                "password": password,
                "session": data["session"],
            }
            status, data = await _api(client, "/keys/device_signing/upload", body)
    if status != 200:
        raise CrossSigningError(f"impossible de publier l'identite du bot : {_uia_hint(data)}")


async def _sign_own_device(client: AsyncClient, ssk: SigningKey, data: dict[str, Any]) -> bool:
    me, device_id = client.user_id, client.device_id
    device = ((data.get("device_keys") or {}).get(me) or {}).get(device_id)
    if device is None:
        raise CrossSigningError(f"le serveur ne connait pas les cles de l'appareil {device_id}")
    # On ne signe que NOTRE cle : un serveur malveillant pourrait sinon nous faire
    # certifier un appareil qu'il controle.
    if device.get("keys", {}).get(f"ed25519:{device_id}") != client.olm.account.identity_keys["ed25519"]:
        raise CrossSigningError("les cles publiees pour cet appareil ne sont pas les siennes")
    if is_signed_by(device, me, ssk.public):
        return False

    signed = {k: v for k, v in device.items() if k not in ("signatures", "unsigned")}
    ssk.sign_into(signed, me)
    status, resp = await _api(client, "/keys/signatures/upload", {me: {device_id: signed}})
    if status != 200 or (resp.get("failures") or {}):
        raise CrossSigningError(f"signature de l'appareil refusee : {resp}")
    return True


async def ensure_identity(
    client: AsyncClient, stored: dict[str, str] | None, *, password: str | None = None
) -> dict[str, str]:
    """Garantit que l'appareil du bot est signe par l'identite du bot.

    `stored` : graines des cles deja creees (fichier de session). Si le serveur annonce
    une autre identite, ou aucune, une nouvelle est creee et publiee - ce qui demande
    `password` des qu'une identite existe deja. Sans mot de passe (demarrage du bot),
    on echoue plutot que de tourner avec un appareil qu'Element ignorerait.

    Retourne les graines a conserver.
    """
    me = client.user_id
    data = await query_keys(client, [me])
    keys = {role: SigningKey.from_b64(seed) for role, seed in (stored or {}).items()}
    on_server = read_identity(data, me).master
    ssk_on_server = _key_of((data.get("self_signing_keys") or {}).get(me), me, "self_signing")

    if not (
        set(keys) == set(ROLES)
        and on_server == keys["master"].public
        and ssk_on_server == keys["self_signing"].public
    ):
        if on_server is not None and password is None:
            raise CrossSigningError(
                "l'identite du compte du bot a change sur le serveur (connexion a ce compte"
                " depuis un autre client ?). Relance `assistant matrix-login`."
            )
        keys = _generate()
        await _upload_identity(client, keys, password)
        data = await query_keys(client, [me])

    await _sign_own_device(client, keys["self_signing"], data)
    return {role: key.seed_b64 for role, key in keys.items()}
