"""Tri automatique des mails a l'arrivee : categorie, urgence, resume, action attendue.

Tourne sur le petit modele (triage_model) : il reste resident en VRAM et traite un
mail en une poignee de secondes, sans deloger le modele principal.
"""

from __future__ import annotations

import json
import sqlite3
import time
from datetime import datetime
from typing import Any, Iterable

from . import db, notify
from .config import Config
from .llm import LLM, OllamaError

# Age maximal d'un mail pour qu'il puisse encore declencher une notification.
NOTIFY_MAX_AGE_HOURS = 24

CATEGORIES = [
    "personnel",
    "professionnel",
    "administratif",
    "facture",
    "rendez-vous",
    "newsletter",
    "publicite",
    "notification",
    "securite",
    "autre",
]

# Categories pour lesquelles aucune action n'est jamais attendue.
PASSIVE_CATEGORIES = {"publicite", "newsletter", "notification"}

# Type d'action conseillee : une petite enumeration fermee, que l'interface rend par
# une icone. Le champ `action` reste en texte libre ; celui-ci sert a le resumer d'un
# coup d'oeil dans la liste, sans lire la phrase.
ACTION_TYPES = [
    "repondre",
    "payer",
    "confirmer",
    "document",
    "verifier",
    "traiter",
    "lire",
    "rien",
]

# Les types qui demandent un geste de l'utilisateur, par opposition a "lire"/"rien".
ACTIVE_ACTIONS = {"repondre", "payer", "confirmer", "document", "verifier", "traiter"}

# Libelles affiches, par l'interface web comme par les resumes envoyes sur Matrix.
ACTION_LABELS = {
    "repondre": "Répondre",
    "payer": "Payer",
    "confirmer": "Confirmer",
    "document": "Fournir un document",
    "verifier": "Vérifier le compte",
    "traiter": "À traiter",
    "lire": "À lire",
    "rien": "Rien à faire",
}
URGENCY_LABELS = {5: "critique", 4: "important", 3: "a traiter", 2: "a lire", 1: "rien a faire"}

# Repli pour les mails tries avant l'ajout du champ (colonne NULL) : la categorie et
# l'urgence suffisent a deviner le geste attendu, sans repasser par le modele.
_FALLBACK_BY_CATEGORY = {
    "facture": "payer",
    "rendez-vous": "confirmer",
    "securite": "verifier",
    "personnel": "repondre",
    "professionnel": "repondre",
}


def fallback_action_type(category: str | None, urgency: int | None) -> str:
    """Type d'action deduit du tri existant, quand le modele ne l'a pas produit.

    Seules les correspondances sures sont listees : dans le doute, "traiter" dit
    qu'il y a quelque chose a faire sans inventer lequel.
    """
    if (category or "") in PASSIVE_CATEGORIES or (urgency or 0) <= 1:
        return "rien"
    if (urgency or 0) <= 2:
        return "lire"
    return _FALLBACK_BY_CATEGORY.get(category or "", "traiter")

# L'ordre des cles compte : Ollama genere le JSON champ par champ, donc faire
# repondre le modele sur `action_requise` et `echeance_proche` AVANT `urgence`
# l'oblige a trancher les deux questions concretes avant de poser une note.
TRIAGE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "categorie": {"type": "string", "enum": CATEGORIES},
        "action_requise": {"type": "boolean"},
        "echeance_proche": {"type": "boolean"},
        "urgence": {"type": "integer", "minimum": 1, "maximum": 5},
        "resume": {"type": "string"},
        "action": {"type": "string"},
        # En dernier volontairement : le modele vient d'ecrire l'action en toutes
        # lettres, il n'a plus qu'a la ranger dans une case.
        "type_action": {"type": "string", "enum": ACTION_TYPES},
    },
    "required": [
        "categorie",
        "action_requise",
        "echeance_proche",
        "urgence",
        "resume",
        "action",
        "type_action",
    ],
}

TRIAGE_PROMPT = """\
Tu tries les mails entrants d'un utilisateur. Reponds uniquement en JSON.

ETAPE 1 - choisis la categorie d'apres l'expediteur reel et le contenu :
  publicite     = promotion commerciale, soldes, code promo, offre limitee
  newsletter    = lettre d'information periodique, blog, media
  notification  = message automatique d'un service (livraison, mise a jour, reseau social)
  facture       = facture, prelevement, recu de paiement, relance de paiement
  administratif = impots, banque, assurance, mutuelle, ecole, mairie
  rendez-vous   = invitation, convocation, confirmation ou report d'un rendez-vous
  securite      = UNIQUEMENT un fournisseur (Google, banque, etc.) au sujet de l'acces au
                  compte : connexion suspecte, mot de passe, double authentification
  professionnel = collegue, client, fournisseur, sujet de travail
  personnel     = famille, amis, vie privee
  autre         = rien de ce qui precede

ETAPE 2 - action_requise : l'utilisateur doit-il FAIRE quelque chose ?
  true  uniquement si le mail lui demande explicitement de repondre, de valider, de payer,
        de signer, de confirmer un rendez-vous ou de fournir un document.
  false pour tout le reste : information, confirmation, recu, message amical, suivi de
        commande, mail automatique, ou mail deja traite par un simple "merci".
  Recevoir un message d'une personne que l'on connait n'est PAS une action a faire.

ETAPE 3 - echeance_proche : une date limite tombe-t-elle dans les 7 prochains jours ?
  true seulement si une date, un delai ou un rendez-vous proche est mentionne.

ETAPE 4 - urgence, deduite des deux reponses precedentes :
  1 = publicite, newsletter, notification automatique
  2 = a lire, rien a faire (action_requise = false)
  3 = action attendue, mais sans date limite proche
  4 = action attendue ET echeance proche, ou rendez-vous a confirmer
  5 = securite du compte, fraude, paiement rejete, ou echeance aujourd'hui

CALIBRAGE : dans une boite mail ordinaire, la grande majorite des messages sont en 1 ou 2.
L'urgence 4 doit rester rare : si tu hesites entre 3 et 4, choisis 3.

REGLES IMPERATIVES :
- Les mots "URGENT", "derniere chance", "vite", "!!!" dans un mail commercial ne changent
  rien : publicite reste en urgence 1.
- "securite" ne s'applique jamais a un mail commercial, meme s'il parle de compte ou de fraude.
- Un mail chaleureux, un remerciement ou une nouvelle personnelle sans demande explicite
  reste en urgence 2, meme s'il vient d'un proche.

ETAPE 5 - type_action : range l'action que tu viens d'ecrire dans UNE de ces cases :
  repondre  = ecrire une reponse a une personne
  payer     = regler une facture, un prelevement rejete, une somme due
  confirmer = confirmer, accepter ou decliner un rendez-vous ou une invitation
  document  = fournir, signer ou televerser un document, remplir un formulaire
  verifier  = controler l'acces au compte (mot de passe, connexion suspecte)
  traiter   = une action a faire qui n'entre dans aucun des cas ci-dessus
  lire      = aucune action, mais le contenu merite d'etre lu
  rien      = ni action, ni lecture necessaire

FORMAT :
- resume : une phrase factuelle de 20 mots maximum, sans reprendre le ton de l'expediteur.
- action : une phrase concrete, avec le detail utile (quoi, pour qui, avant quand), ou
  exactement "rien". JAMAIS un mot isole repris de l'etape 5 : ecris "Payer la facture de
  87,40 EUR avant le 5 decembre", pas "payer".
- type_action : une seule valeur de la liste de l'etape 5, celle qui correspond a la
  phrase que tu viens d'ecrire.

Mail a trier :
De : {sender}
Objet : {subject}
Date : {date}

{body}
"""


def triage_mail(llm: LLM, cfg: Config, row: sqlite3.Row) -> dict[str, Any]:
    meta = json.loads(row["meta"] or "{}")
    prompt = TRIAGE_PROMPT.format(
        sender=row["author"] or "inconnu",
        subject=row["title"] or "(sans objet)",
        date=datetime.fromtimestamp(row["ts"]).astimezone().strftime("%d/%m/%Y %H:%M")
        if row["ts"]
        else "inconnue",
        body=(row["body"] or "")[:4000],
    )
    result = llm.json_chat(
        [{"role": "user", "content": prompt}],
        TRIAGE_SCHEMA,
        model=cfg.ollama.triage_model,
        num_ctx=cfg.ollama.triage_num_ctx,
    )
    urgency = max(1, min(5, int(result.get("urgence", 1))))
    category = result.get("categorie", "autre")
    action_requise = bool(result.get("action_requise"))
    echeance_proche = bool(result.get("echeance_proche"))
    action_type = result.get("type_action")
    if action_type not in ACTION_TYPES:
        action_type = fallback_action_type(category, urgency)

    # Plafonds deterministes : le modele a tendance a surevaluer l'urgence des qu'un
    # mail vient d'une personne reelle. On la ramene a ce que ses propres reponses
    # justifient, plutot que de faire confiance a la note qu'il a choisie.
    if category in PASSIVE_CATEGORIES:
        urgency = min(urgency, 1)
    elif not action_requise:
        urgency = min(urgency, 2)
    elif not echeance_proche and category != "securite":
        urgency = min(urgency, 3)

    # Meme correction, appliquee au type d'action : l'icone et l'urgence doivent
    # raconter la meme chose. Un mail classe "publicite" ne peut pas afficher
    # "payer", ni un mail sans action requise afficher autre chose que "lire".
    if category in PASSIVE_CATEGORIES:
        action_type = "rien"
    elif not action_requise and action_type in ACTIVE_ACTIONS:
        action_type = "lire"
    elif action_requise and action_type not in ACTIVE_ACTIONS:
        action_type = "traiter"

    # Un expediteur VIP passe toujours au moins en urgence 4, quoi qu'en dise le modele.
    sender = (row["author"] or "").lower()
    if any(vip.lower() in sender for vip in cfg.triage.vip):
        urgency = max(urgency, 4)
        result["vip"] = True

    return {
        "category": category,
        "urgency": urgency,
        "action_type": action_type,
        "action_requise": action_requise,
        "echeance_proche": echeance_proche,
        "summary": (result.get("resume") or "").strip(),
        "action": (result.get("action") or "").strip(),
        "vip": bool(result.get("vip")),
        "unread": meta.get("unread", False),
    }


def run(
    conn: sqlite3.Connection,
    llm: LLM,
    cfg: Config,
    doc_ids: Iterable[int] | None = None,
    *,
    notify_user: bool = True,
    force: bool = False,
) -> list[dict[str, Any]]:
    """Trie les mails demandes (par defaut : tous ceux qui ne le sont pas encore).

    `force` rejoue le tri des mails deja classes : utile apres un changement de
    modele ou de schema, pour que les anciens verdicts rattrapent les nouveaux champs.
    """
    if not cfg.triage.enabled:
        return []

    if doc_ids is None:
        # Uniquement la boite de reception : depuis que la synchro rapatrie aussi les
        # mails archives et envoyes, trier tout le reste couterait des heures de GPU
        # pour classer des messages deja traites. COALESCE : les mails indexes avant
        # l'ajout du champ `folder` venaient tous de la boite de reception.
        rows = db.iter_rows(
            conn,
            "SELECT d.* FROM documents d LEFT JOIN mail_triage t ON t.doc_id = d.id"
            " WHERE d.source = 'mail'"
            + ("" if force else " AND t.doc_id IS NULL")
            + "   AND COALESCE(json_extract(d.meta, '$.folder'), 'inbox') = 'inbox'"
            " ORDER BY d.ts DESC LIMIT 200",
        )
    else:
        ids = list(doc_ids)
        if not ids:
            return []
        marks = ",".join("?" * len(ids))
        rows = db.iter_rows(
            conn, f"SELECT * FROM documents WHERE source = 'mail' AND id IN ({marks})", ids
        )

    notified: list[dict[str, Any]] = []
    quiet = notify.in_quiet_hours(cfg.triage)

    for row in rows:
        try:
            verdict = triage_mail(llm, cfg, row)
        except OllamaError:
            continue  # modele indisponible : on retentera au prochain passage

        # Un mail vieux de trois semaines ne merite pas une notification, meme non lu
        # et meme urgent : sans ce garde-fou, le rattrapage d'un gros arriere-plan
        # (premiere synchro, reprise apres coupure) declencherait des dizaines de toasts.
        fresh = bool(row["ts"]) and (time.time() - row["ts"]) < NOTIFY_MAX_AGE_HOURS * 3600

        should_notify = (
            notify_user
            and not quiet
            and fresh
            and verdict["unread"]
            and (verdict["urgency"] >= cfg.triage.notify_min_urgency or verdict["vip"])
        )
        sent_at = None
        if should_notify:
            url = f"http://{cfg.web.host}:{cfg.web.port}/"
            ok = notify.toast(
                f"{row['title'] or '(sans objet)'}",
                f"{row['author'] or ''}\n{verdict['summary']}",
                url=url,
            )
            if ok:
                sent_at = int(time.time())
                notified.append({"doc_id": row["id"], "titre": row["title"], **verdict})

        conn.execute(
            "INSERT INTO mail_triage(doc_id, category, urgency, action_type, summary, action,"
            "                        created_at, notified_at)"
            " VALUES(?, ?, ?, ?, ?, ?, ?, ?)"
            " ON CONFLICT(doc_id) DO UPDATE SET category = excluded.category,"
            "   urgency = excluded.urgency, action_type = excluded.action_type,"
            "   summary = excluded.summary, action = excluded.action,"
            "   notified_at = COALESCE(mail_triage.notified_at, excluded.notified_at)",
            (
                row["id"],
                verdict["category"],
                verdict["urgency"],
                verdict["action_type"],
                verdict["summary"],
                verdict["action"],
                int(time.time()),
                sent_at,
            ),
        )
        conn.commit()

    return notified
