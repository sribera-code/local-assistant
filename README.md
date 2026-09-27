# Assistant local

Un assistant qui lit tes mails Gmail, ton agenda Google et tes fichiers locaux, et répond
à tes questions via un LLM qui tourne sur ta machine (Ollama). Interface web locale, avec
tri automatique des mails entrants et notifications Windows.

## Ce qui reste local, et ce qui ne peut pas l'être

| Étape | Où ça se passe |
|---|---|
| Récupération des mails et de l'agenda | **Google** — tes mails sont chez eux, il faut bien les demander (API en lecture seule, jeton stocké sur le disque) |
| Extraction, découpage, embeddings | ta machine |
| Index, base de données | `data/assistant.db`, sur ton disque |
| LLM (questions, résumés, tri) | ta machine, via Ollama |
| Interface web | `127.0.0.1`, rien n'est exposé sur le réseau |
| Chat depuis le téléphone (optionnel) | ton serveur Matrix, **chiffré de bout en bout** : il transporte les messages sans pouvoir les lire (voir [Matrix](#matrix--lassistant-sur-ton-téléphone)) |

Aucune clé d'API tierce, aucun contenu de mail ou de fichier envoyé à un service externe —
à une exception près, si tu l'actives : les réponses envoyées sur ton téléphone via Matrix,
chiffrées avant de quitter le PC.
Les portées OAuth demandées sont `gmail.readonly` et `calendar.readonly` : l'assistant ne
peut ni envoyer, ni supprimer, ni modifier quoi que ce soit dans ton compte.

## Modèles

Les valeurs ci-dessous ont été mesurées sur la machine cible (RTX 4070 Laptop, **8 Go de
VRAM**, 32 Go de RAM, i9-13900HX) :

| Rôle | Modèle | Poids | Mesure |
|---|---|---|---|
| Chat + tri des mails | `qwen3.5:4b` | 3,4 Go | 2,7 s par question (modèle déjà chargé), 1,9 s par mail trié |
| Embeddings | `qwen3-embedding:0.6b` | 639 Mo | 1024 dimensions |

Les deux ensemble occupent **~7,2 Go sur 8 Go** de VRAM, contexte compris. C'est
volontairement juste : il ne reste pas la place pour un troisième modèle, d'où le choix
d'utiliser le même modèle pour le chat et pour le tri.

Alternatives testées :

- `qwen3.5:2b` en modèle de tri : deux fois plus rapide (1,0 s/mail) mais se trompe plus
  souvent de catégorie. À envisager si tu reçois beaucoup de mails.
- `qwen3.5:9b` (6,6 Go) : meilleures synthèses, mais ne tient pas en VRAM avec le modèle
  d'embeddings. Ollama déchargerait l'un pour l'autre à chaque question. Jouable en
  baissant `num_ctx` à ~8192, au prix d'un rechargement de quelques secondes par question.
- `gemma4` : sa plus petite variante réelle est 12B (7,2 Go en QAT), ce qui sature les 8 Go,
  et Gemma reste moins fiable que Qwen sur l'appel d'outils — or tout l'assistant repose
  là-dessus.

**Le piège sur 8 Go**, c'est le contexte, pas le poids du modèle : Qwen 3.5 annonce 256k
tokens. Si `num_ctx` n'est pas borné (32768 par défaut ici), le cache KV déborde en RAM et
le débit s'écroule.

## Installation

```powershell
# 1. Modèles (une fois)
ollama pull qwen3.5:4b
ollama pull qwen3-embedding:0.6b

# 2. Environnement Python (3.12 à 3.14)
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e .

# 3. Configuration
copy config.example.yaml config.yaml
#    puis renseigne files.roots avec les dossiers à indexer

# 4. Vérification
.\.venv\Scripts\python.exe -m assistant.cli doctor
```

`doctor` contrôle la config, la présence d'Ollama et des modèles, les accès Google, la base,
et calcule un embedding de test pour vérifier que sa dimension correspond à `embed_dim`.

### Accès Google (une fois)

1. [Crée un projet](https://console.cloud.google.com/projectcreate) (gratuit)
2. *APIs & Services > Enabled APIs* : active **Gmail API** puis **Google Calendar API**
3. *OAuth consent screen* : type « External », ajoute ton adresse comme utilisateur de test
4. *Credentials > Create credentials > OAuth client ID*, type **Desktop app**
5. Enregistre le JSON téléchargé dans `~/.local-assistant/secrets/client_secret.json`
   (soit `C:\Users\<toi>\.local-assistant\secrets\` — **hors du repo**, voir plus bas)
6. `python -m assistant.cli auth` → le navigateur s'ouvre, tu autorises, le jeton est écrit
   dans `~/.local-assistant/secrets/token.json`

### « Erreur 403 : access_denied » à l'autorisation

Message : *« Accès bloqué : … n'a pas terminé la procédure de validation de Google »*.

Tant que l'application est en statut **Testing**, Google n'autorise que les comptes
explicitement listés. Va dans **Google Auth Platform > Audience**, section **Test users**,
et ajoute ton adresse Gmail. Puis relance `.\assistant auth`.

### Le jeton expire tous les 7 jours (limite Google)

En statut **Testing** avec un scope restreint comme `gmail.readonly`, Google **révoque le
jeton de rafraîchissement au bout de 7 jours**. Ce n'est pas un bug du programme : il faut
relancer `.\assistant auth` chaque semaine.

Le code gère ce cas proprement : une synchronisation n'ouvre jamais de navigateur
d'elle-même, elle signale simplement qu'une reconnexion est nécessaire (dans le bandeau
d'état de l'interface et dans la sortie de `sync`). Seule la commande `auth` est
interactive.

Pour y échapper, passe l'application en **In production** (*Audience > Publish app*). Un
écran « application non vérifiée » apparaîtra à la connexion — normal pour un usage
personnel, tu passes par *Paramètres avancés* — et le jeton cesse d'expirer. La
vérification complète par Google n'est nécessaire que pour distribuer l'application à
d'autres personnes.

### Pourquoi les secrets sont hors du repo

Les deux fichiers n'ont pas la même sensibilité :

- `client_secret.json` (client OAuth de type « Desktop app ») n'est pas un secret au sens
  strict : Google documente qu'il ne peut pas l'être, puisqu'il est distribué dans
  l'application. Seul, il ne donne accès à aucune donnée.
- **`token.json` est le fichier qui compte** : il contient le jeton de rafraîchissement, donc
  un accès en lecture à tes mails et à ton agenda jusqu'à révocation dans
  [les autorisations de ton compte Google](https://myaccount.google.com/permissions).

Les garder dans `~/.local-assistant/secrets/` plutôt que dans le dépôt évite trois choses
concrètes :

1. un `git add -A` malencontreux (le `.gitignore` aide, mais ne protège pas un dépôt reclonné
   ou un fichier forcé) ;
2. leur indexation par l'assistant lui-même : `files.extensions` inclut `.json`, donc un
   dossier de secrets situé sous un `files.roots` finirait embedé dans la base et servi au
   LLM ;
3. leur perte si tu supprimes ou reclones le dépôt.

`.claude/settings.json` ajoute une couche côté Claude Code : des règles `deny` sur ces chemins
et un hook `PreToolUse` qui bloque aussi les contournements par shell (`cat`, `type`,
`Get-Content`). C'est cette dernière couche qui fait la garantie — un simple changement
d'emplacement ne suffirait pas, un agent pouvant lire n'importe où sur le disque.

## Utilisation

Depuis la racine du projet, `assistant.cmd` appelle le Python du venv — pas besoin d'activer
l'environnement. Fonctionne dans cmd.exe comme dans PowerShell :

```
.\assistant auth                    # connecte le compte Google (une fois)
.\assistant sync                    # synchronise, indexe, trie les nouveaux mails
.\assistant serve                   # interface web sur http://127.0.0.1:8765
.\assistant ask "qui attend une réponse de moi ?"
.\assistant stats                   # contenu de l'index
.\assistant doctor                  # diagnostic
.\assistant triage                  # trie les mails pas encore classés (--force : tous)
.\assistant reset-index             # vide chunks + embeddings, garde les documents
.\assistant matrix-login            # connecte le compte Matrix du bot (une fois)
.\assistant matrix                  # bot Matrix + synchro de fond, sans l'interface web
.\assistant matrix-trust @toi:matrix.org   # accepte ta nouvelle identité Matrix
```

> `python -m assistant.cli ...` avec le Python système échoue (`No module named 'typer'`) :
> les dépendances sont dans `.venv`, pas globales. Utilise `.\assistant`, ou
> `.venv\Scripts\python.exe -m assistant.cli ...`.

`serve` lance aussi une boucle de fond qui relève les mails toutes les
`gmail.poll_seconds` (120 s par défaut), l'agenda et les fichiers toutes les
`calendar.poll_seconds`, trie les nouveaux mails et affiche une notification Windows au-delà
de `triage.notify_min_urgency`.

## Matrix : l'assistant sur ton téléphone

Tu écris à l'assistant depuis **Element** (Element X ou Element classique, Android ou iOS),
comme à un contact. Le PC fait tourner un bot Matrix (bibliothèque `matrix-nio`) qui reçoit
la question, la passe au même agent que l'interface web, et répond dans la conversation.

- **Aucun port ouvert sur le PC** : le bot se connecte lui-même au serveur Matrix et attend
  les messages (synchro longue). L'interface web reste sur `127.0.0.1`.
- **Chiffré de bout en bout** (Olm/Megolm, via vodozemac) : le serveur Matrix ne peut pas
  lire les questions ni les réponses. Il voit en revanche qui parle à qui, quand, et la
  taille des messages.
- **Même historique** : chaque discussion Matrix apparaît comme un onglet « Matrix · … »
  dans le chat de l'interface web, résumés de mails compris. La page vérifie toutes les
  5 s ce qui a changé (nouvel onglet, nouveaux messages, nouveaux mails, compteurs) et
  ne recharge que cela.
- Le PC doit être allumé. Une question envoyée pendant qu'il était éteint reçoit sa
  réponse au démarrage, si elle date de moins d'une heure.

### Mise en place

1. **Crée un compte dédié au bot**, distinct du tien — par exemple sur matrix.org depuis
   [app.element.io](https://app.element.io), dans une fenêtre privée. Note son identifiant
   (`@mon-assistant:matrix.org`) et son mot de passe. Déconnecte-toi ensuite : le bot doit
   être la seule session de ce compte.
2. Dans `config.yaml` :
   ```yaml
   matrix:
     enabled: true                        # démarre le bot avec `serve`
     user_id: "@mon-assistant:matrix.org" # le compte du bot
     allowed_users: ["@toi:matrix.org"]   # TON compte : le seul à qui il répond
   ```
3. `.\assistant matrix-login` : demande le mot de passe du bot (jamais enregistré),
   connecte l'appareil et lui donne une identité de signature croisée (voir plus bas).
   - Sur **matrix.org** (et tout serveur qui délègue la connexion à MAS), si Element a déjà
     créé une identité pour ce compte à l'étape 1, le serveur exige d'approuver son
     remplacement dans le navigateur. La commande affiche alors l'URL : ouvre-la
     **connecté avec le compte du bot**, approuve, et relance `matrix-login` dans les
     10 minutes.
4. `.\assistant serve` (ou `.\assistant matrix` sans l'interface web).
5. Sur le téléphone, dans Element : **nouvelle discussion** avec `@mon-assistant:matrix.org`.
   Le bot accepte l'invitation — il refuse toutes celles qui ne viennent pas
   d'`allowed_users` — et tu peux poser tes questions.

`!nouveau` dans la discussion repart d'une conversation vierge (le modèle ne voit que les
6 derniers messages de la conversation en cours).

### Résumé de chaque nouveau mail

Chaque mail qui arrive dans la boîte de réception est envoyé dans la discussion une fois
trié : objet (lien vers Gmail), expéditeur, résumé, **action attendue** et statut.

```
🟠 Facture de novembre
De : EDF
Votre facture de novembre (87,40 EUR) sera prélevée le 5 décembre.
💳 Vérifier le montant avant le prélèvement du 5 décembre
urgence 4/5 (important) · facture
```

- **Réponds à un résumé** (appui long > Répondre dans Element) pour poser une question sur
  ce mail : « que dois-je répondre ? », « c'est une arnaque ? ». Le modèle le lit d'office,
  comme le bouton « Que dois-je en faire ? » de l'interface web.
- Ne sont envoyés que les mails **non lus**, de la boîte de réception, reçus depuis moins de
  24 h : un mail déjà lu dans Gmail n'est plus une nouvelle. Au premier lancement, les mails
  déjà présents ne sont pas envoyés.
- Plus de 3 mails d'un coup (réveil du PC après une nuit) : un seul message récapitulatif.
- Les résumés s'affichent aussi dans l'onglet de la discussion dans l'interface web, avec le
  mail en source cliquable. Ils ne sont pas transmis au modèle comme contexte : ils
  évinceraient les vraies questions des 6 messages qu'il voit.
- `matrix.mail_ping_min_urgency` règle ce qui fait **sonner** le téléphone : en dessous, le
  résumé arrive en silence (message de type « notice », que les règles de notification par
  défaut de Matrix ne signalent pas). À 3, seuls les mails qui demandent une action sonnent.
- Les résumés vont dans la dernière discussion où tu as écrit au bot. Pour les nuits, le
  mode « Ne pas déranger » du téléphone ou les réglages de notification de la discussion
  dans Element font l'affaire.
- Le bot lit le résultat du tri dans la base : il faut que la synchro tourne, ce que font
  `serve` et `.\assistant matrix`. `mail_notices: false` désactive l'envoi.

### Pourquoi le bot signe son propre appareil

Element applique désormais
[MSC4153](https://github.com/matrix-org/matrix-spec-proposals/blob/main/proposals/4153-invisible-crypto.md)
(« exclude insecure devices », par défaut à partir d'octobre 2026) : un appareil que son
propriétaire n'a pas signé avec son identité (*cross-signing*) **ne reçoit plus les clés** des
conversations, et ses messages s'affichent sans contenu. `matrix-nio` ne gère pas la
signature croisée ; un bot `matrix-nio` brut deviendrait donc sourd et muet.

`assistant/matrix/crosssign.py` comble ce manque : il crée les trois clés de l'identité du
bot (maîtresse, auto-signature, signature d'utilisateurs), les publie, et signe l'appareil du
bot avec. Leurs graines sont gardées dans la session, pour signer le nouvel appareil lors d'un
futur `matrix-login` sans changer d'identité.

Element peut proposer de « vérifier » le bot par emojis : ce n'est pas nécessaire, et le bot
ne sait pas y répondre.

### Qui peut lire les réponses

Seuls les appareils de **ton** compte signés par **ton** identité, et cette identité est
**épinglée** au premier message : le bot retient ta clé maîtresse et refuse ensuite toute
autre. Concrètement :

- un appareil ajouté à ton compte à ton insu (serveur compromis, mot de passe volé) n'est pas
  signé par ton identité : il ne reçoit pas les clés, et ce qu'il envoie est ignoré ;
- une session Element que tu n'as pas vérifiée est traitée de même — vérifie-la depuis une
  autre session, ou avec ta clé de récupération ;
- si ton identité change (tu l'as réinitialisée, ou quelqu'un l'a fait), le bot ne répond
  plus et affiche une notification Windows. Si c'est bien toi :
  `.\assistant matrix-trust @toi:matrix.org`, et la nouvelle est épinglée au message suivant.

Le bot ne répond que dans un salon chiffré où il est seul avec toi.

### Fichiers

`~/.local-assistant/matrix/` contient `session.json` (jeton d'accès, graines de l'identité du
bot, identités épinglées) et `store/nio.db` (clés de chiffrement, protégées par une clé
aléatoire propre à l'installation). **Aussi sensible que `token.json`** : ce dossier permet de
lire et d'écrire au nom du bot. Il est couvert par les mêmes protections (hors du dépôt, règles
de `.claude/settings.json`). Pour tout révoquer : supprime la session du bot dans Element
(*Paramètres > Sessions* du compte du bot), puis le dossier.

## Comment le tri fonctionne

Chaque mail entrant passe par le modèle avec une sortie JSON contrainte par schéma
(`format` d'Ollama, donc pas de réponse hors format) et ressort avec :

- une **catégorie** parmi 10 (publicité, newsletter, facture, administratif, rendez-vous,
  sécurité, professionnel, personnel, notification, autre) ;
- deux booléens, **`action_requise`** et **`echeance_proche`** ;
- une **urgence de 1 à 5** ;
- un **résumé** d'une phrase et l'**action** attendue ;
- un **type d'action** parmi 8 (répondre, payer, confirmer, document, vérifier, traiter,
  lire, rien), que la liste de mails affiche sous forme d'icône.

L'ordre des champs du schéma n'est pas décoratif : Ollama génère le JSON clé par clé, donc
placer les deux booléens **avant** `urgence` force le modèle à trancher deux questions
concrètes avant de poser une note. C'est un raisonnement guidé qui ne coûte rien. Le type
d'action, lui, vient **en dernier** : le modèle vient d'écrire l'action en toutes lettres,
il n'a plus qu'à la ranger dans une case. L'inverse produisait des actions réduites au mot
de l'énumération (« payer » au lieu de « payer la facture de 87,40 € avant le 5 décembre »).

L'urgence produite par le modèle est ensuite **replafonnée par du code**, pas par de la
confiance :

| Situation | Plafond |
|---|---|
| catégorie publicité / newsletter / notification | 1 |
| `action_requise` faux | 2 |
| action requise mais pas d'échéance proche (hors sécurité) | 3 |

Sans ces plafonds, le modèle classait **93 % d'une boîte réelle en urgence 4** — un signal
de priorité inutilisable. Avec, la même population tombe à 5 % en urgence 4 et 84 % en
urgence 2, pour 14 % de mails demandant réellement une action.

Le type d'action subit la même correction par le code que l'urgence : une catégorie
passive force « rien », l'absence d'action requise force « lire », et une action requise
que le modèle n'a pas su nommer retombe sur « traiter ». L'icône et l'étiquette d'urgence
racontent ainsi toujours la même chose.

Deux autres garde-fous vivent dans le prompt : « URGENT », « dernière chance » et « !!! »
dans un mail commercial ne remontent pas l'urgence, et la catégorie « sécurité » est
réservée aux messages d'un fournisseur au sujet de l'accès au compte. Sans eux, le modèle
classait une promotion en « sécurité, urgence 5 ».

## Dossiers

Gmail n'a pas de dossiers : un mail **est** dans la boîte de réception tant qu'il porte le
label `INBOX`, et l'archiver revient à le lui retirer. L'interface en déduit quatre onglets
— Réception, Archivés, Envoyés, Tous — à partir des labels (`assistant/ingest/gmail.py`,
`folder_of`).

Ce qui est rapatrié dépend de `gmail.query` :

| `gmail.query` | Ce qui est indexé |
|---|---|
| `-in:spam -in:trash` (défaut) | toute la boîte : réception, archives, envoyés |
| `in:inbox` | la boîte de réception seule ; les onglets Archivés et Envoyés restent vides |

**Changer cette valeur déclenche une resynchronisation complète** au `sync` suivant. Sans
cela, le curseur incrémental ne ramènerait que les mails postérieurs et tout l'historique
nouvellement couvert resterait invisible. La requête utilisée est mémorisée dans la base
(`meta.gmail_query`) et comparée à chaque synchro ; une base qui ne porte pas encore ce
marqueur est traitée comme un changement, puisqu'elle a forcément été remplie avec une
autre requête. La profondeur du rattrapage est celle de `gmail.initial_backfill`.

Deux conséquences de l'élargissement, traitées dans le code :

- **Le tri reste limité à la boîte de réception.** Trier les archives et les envoyés
  coûterait des heures de GPU pour classer des messages déjà traités — et un mail qu'on a
  écrit soi-même n'attend aucune action. Les mails hors réception n'ont donc ni urgence ni
  icône d'action. L'outil `mails_recents` du LLM est borné de la même façon ; `rechercher`,
  lui, voit tout l'historique indexé, ce qui est justement l'intérêt d'indexer les archives.
  `assistant triage --force` rejoue le tri des mails déjà classés, pour que d'anciens
  verdicts rattrapent un nouveau champ du schéma.
- **L'archivage est rattrapé par la réconciliation.** La synchro incrémentale ne redemande
  jamais un vieux mail : sans cela, un mail archivé il y a un mois resterait affiché dans la
  boîte de réception. `reconcile()` compare donc à chaque passe trois listes d'identifiants
  (présents, non lus, dans la boîte) et met à jour les labels. C'est peu coûteux : aucun
  contenu n'est téléchargé.

## Recherche

Recherche hybride sur le même index :

- **FTS5** pour les mots-clés — trouve un numéro de facture, un nom propre, une référence
  (avec `remove_diacritics`, donc « reunion » trouve « réunion ») ;
- **sqlite-vec** pour le sens — trouve « mon rendez-vous chez le dentiste » dans un mail qui
  parle de « consultation dentaire » ;
- fusion **RRF** des deux classements, puis un seul extrait par document.

Le LLM n'interroge pas la base directement : il appelle les outils `rechercher`, `agenda`,
`mails_recents` et `lire_document`, et chaque document qu'un outil lui montre devient une
citation cliquable `[doc_id]` dans l'interface.

## Architecture

```
assistant/
  cli.py            commandes : doctor, auth, sync, index, triage, ask, stats, serve, matrix
  config.py         chargement de config.yaml (dataclasses, clés inconnues refusées)
  db.py             SQLite : documents, chunks, FTS5, vec0 + connexions par thread
  llm.py            client Ollama : chat, appel d'outils, JSON contraint, embeddings
  indexer.py        découpage par paragraphes + embeddings par lots de 16
  search.py         recherche hybride FTS5 + vectorielle, fusion RRF
  tools.py          les 4 outils exposés au LLM
  agent.py          boucle d'appel d'outils (4 tours max) + citations
  triage.py         classification des mails entrants
  notify.py         toasts Windows, avec heures silencieuses
  pipeline.py       orchestration + boucle de synchro de fond
  matrix/
    bot.py          bot Matrix : connexion, confiance, file de messages, conversations
    crosssign.py    signature croisée (absente de matrix-nio) et vérification des appareils
    render.py       réponse -> HTML pour Element (même échappement que l'interface)
  ingest/
    google_auth.py  OAuth lecture seule
    gmail.py        mails : MIME, HTML→texte, découpe des citations de fil
    gcal.py         évènements (récurrences développées)
    files.py        parcours de dossiers + extraction pdf/docx/xlsx/csv/texte
  web/              FastAPI + HTMX (htmx servi depuis le disque, donc hors ligne)
```

## Réglages utiles

Dans `config.yaml` :

- `files.exclude_dirs` contient `Repositories` et `AppData` : les dépôts de code et les
  dossiers système sont ignorés. Retire-les si tu veux indexer du code.
- `triage.vip` : les expéditeurs qui y figurent passent toujours au minimum en urgence 4.
- `triage.quiet_hours` : plage sans notification (22h→8h par défaut).
- `gmail.query` : ce que la synchro rapatrie (voir « Dossiers » plus haut).
- `gmail.initial_backfill` : nombre de mails récupérés à la première synchro — et à celle
  qui suit un changement de `gmail.query`. Les autres sont incrémentales (curseur sur la
  date interne Gmail).
- `retrieval.top_k` : nombre d'extraits envoyés au modèle par recherche.

## Limites connues

- Les pièces jointes sont listées par leur nom mais leur contenu n'est pas indexé (seuls les
  fichiers des dossiers `files.roots` le sont).
- Pas de synchro Gmail par push : la boucle interroge l'API à intervalle fixe. Le temps réel
  demanderait un webhook Pub/Sub, donc un endpoint public — contraire au principe du projet.
- Les fichiers sont réindexés d'après leur taille et leur date de modification. Une
  modification qui ne change ni l'une ni l'autre passe inaperçue.
- L'historique de conversation envoyé au modèle est plat (6 derniers messages), sans
  résumé au-delà. Seules les sources de la dernière réponse lui sont relues (tronquées) :
  « détaille » marche, un renvoi plus lointain (« et le deuxième ? ») reste fragile avec un 4B.
- Le bot Matrix ne traite que du texte : ni pièces jointes, ni messages vocaux, et la
  réponse arrive d'un bloc (pas d'affichage au fil de l'eau).
