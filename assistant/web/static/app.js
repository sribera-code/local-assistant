// Interface de l'assistant local : chat, synchronisation, listes et fichiers.
// Tout passe par le serveur local (127.0.0.1) ; aucun appel externe.

const chat = () => document.getElementById("chat");

const ESCAPES = { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" };
const escapeHtml = (s) => String(s).replace(/[&<>"']/g, (c) => ESCAPES[c]);

// Rendu Markdown minimal et volontairement limite.
// Le texte est ECHAPPE EN ENTIER d'abord : rien de ce que produit le modele — ni le
// contenu de mail qu'il recopie — ne peut introduire de HTML. On ne reintroduit
// ensuite qu'un jeu ferme de balises de mise en forme.
// Seuls ces schemas d'URL peuvent devenir un href. Les liens viennent de notre
// propre base (Gmail, Agenda), mais un "javascript:" qui s'y glisserait
// s'executerait au clic : on verifie plutot que de supposer.
// Les fichiers locaux sont a part : un navigateur refuse d'ouvrir un lien file://
// depuis une page http, ils passent donc par /api/open (voir openFile).
const SCHEMAS_SURS = /^(https?:|mailto:)/i;

function renderMarkdown(text, citations) {
  // doc_id -> source, pour transformer les [42] du texte en liens cliquables.
  const sources = new Map();
  (citations || []).forEach((c) => sources.set(String(c.doc_id), c));

  const lienCitation = (id) => {
    const c = sources.get(id);
    if (c && c.source === "mail") {
      const titreM = escapeHtml(`${c.titre || ""}${c.date ? " — " + c.date : ""}`);
      return `<a class="ref" href="/?mail=${Number(c.doc_id)}" data-mail="${Number(c.doc_id)}" title="${titreM}">[${id}]</a>`;
    }
    if (c && c.source === "file") {
      const titreF = escapeHtml(c.titre || "");
      return `<a class="ref" href="#" data-open="${Number(c.doc_id)}" title="${titreF}">[${id}]</a>`;
    }
    if (!c || !c.url || !SCHEMAS_SURS.test(c.url)) {
      return `<span class="ref">[${id}]</span>`;
    }
    const titre = escapeHtml(`${c.titre || ""}${c.date ? " — " + c.date : ""}`);
    return (
      `<a class="ref" href="${escapeHtml(c.url)}" target="_blank" rel="noreferrer"` +
      ` title="${titre}">[${id}]</a>`
    );
  };

  const inline = (s) =>
    s
      .replace(/`([^`]+)`/g, "<code>$1</code>")
      .replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>")
      .replace(/(^|[\s(])\*([^*\n]+)\*/g, "$1<em>$2</em>")
      .replace(/\[(\d{1,6})\]/g, (_, id) => lienCitation(id));

  const out = [];
  let list = null;
  const closeList = () => {
    if (list) {
      out.push(`</${list}>`);
      list = null;
    }
  };

  for (const raw of escapeHtml(text).split("\n")) {
    const line = raw.trim();
    if (!line) {
      closeList();
      continue;
    }
    let m;
    if ((m = line.match(/^#{1,6}\s+(.*)$/))) {
      closeList();
      out.push(`<h4>${inline(m[1])}</h4>`);
    } else if ((m = line.match(/^[-*+]\s+(.*)$/))) {
      if (list !== "ul") {
        closeList();
        out.push("<ul>");
        list = "ul";
      }
      out.push(`<li>${inline(m[1])}</li>`);
    } else if ((m = line.match(/^\d+[.)]\s+(.*)$/))) {
      if (list !== "ol") {
        closeList();
        out.push("<ol>");
        list = "ol";
      }
      out.push(`<li>${inline(m[1])}</li>`);
    } else {
      closeList();
      out.push(`<p>${inline(line)}</p>`);
    }
  }
  closeList();
  return out.join("");
}

// Liste compacte des sources sous une reponse. Construite avec le DOM (textContent),
// jamais par concatenation de HTML : les titres viennent de mails et de fichiers.
function renderSources(citations) {
  const liste = (citations || []).filter((c) => c && c.titre);
  if (!liste.length) return null;
  const bloc = document.createElement("div");
  bloc.className = "sources";
  const label = document.createElement("span");
  label.className = "sources-label";
  label.textContent = "Sources";
  bloc.appendChild(label);
  liste.forEach((c) => {
    const lien = document.createElement("a");
    lien.className = `source source-${c.source}`;
    lien.textContent = c.titre;
    lien.title = [c.titre, c.date].filter(Boolean).join(" — ");
    if (c.source === "mail") {
      // Un mail cite s'ouvre dans l'onglet Mails, pas dans Gmail.
      lien.href = `/?mail=${Number(c.doc_id)}`;
      lien.dataset.mail = c.doc_id;
    } else if (c.source === "file") {
      lien.href = "#";
      lien.dataset.open = c.doc_id;
    } else if (c.url && SCHEMAS_SURS.test(c.url)) {
      lien.href = c.url;
      lien.target = "_blank";
      lien.rel = "noreferrer";
    }
    bloc.appendChild(lien);
  });
  return bloc;
}

function scrollChat() {
  const box = chat();
  box.scrollTop = box.scrollHeight;
}

function addMessage(role, text, citations) {
  const empty = chat().querySelector(".empty");
  if (empty) empty.remove();

  const wrap = document.createElement("div");
  wrap.className = `msg ${role}`;

  const bubble = document.createElement("div");
  bubble.className = "bubble";
  if (role === "assistant") {
    bubble.classList.add("md");
    bubble.innerHTML = renderMarkdown(text, citations);
  } else {
    // Question de l'utilisateur ou message d'erreur : affichage litteral.
    bubble.textContent = text;
  }
  wrap.appendChild(bubble);

  if (role === "assistant") {
    const sources = renderSources(citations);
    if (sources) wrap.appendChild(sources);
  }

  chat().appendChild(wrap);
  scrollChat();
  return wrap;
}

function addPending() {
  const wrap = document.createElement("div");
  wrap.className = "msg assistant pending";
  wrap.innerHTML = '<div class="thinking"><i></i><i></i><i></i><span>Recherche en cours</span></div>';
  chat().appendChild(wrap);
  scrollChat();
  return wrap;
}

async function sendQuestion(event, docId) {
  if (event) event.preventDefault();
  const input = document.getElementById("q");
  const question = input.value.trim();
  if (!question) return false;

  // La reponse appartient a la conversation ou la question a ete posee, meme si
  // l'utilisateur change d'onglet pendant que le modele reflechit.
  const conv = currentConversation();
  input.value = "";
  autoGrow(input);
  addMessage("user", question);
  addPending();
  setBusy(conv, true);
  const tab = tabOf(conv);
  if (tab) tab.dataset.count = String(Number(tab.dataset.count || 0) + 1);

  // N'affiche que si la conversation est toujours celle a l'ecran.
  const afficher = (...args) => {
    if (currentConversation() !== conv) {
      tabOf(conv)?.classList.add("fresh");
      return;
    }
    chat().querySelector(".msg.pending")?.remove();
    addMessage(...args);
  };

  try {
    const body = new FormData();
    body.append("question", question);
    body.append("conversation_id", String(conv));
    // Document sur lequel porte la question : le serveur le fait lire au modele.
    if (docId) body.append("doc_id", String(Number(docId)));
    const resp = await fetch("/api/ask", { method: "POST", body });
    const data = await resp.json();
    setBusy(conv, false);
    if (!resp.ok) {
      afficher("error", data.error || data.detail || `Erreur ${resp.status}`);
      return false;
    }
    if (data.titre) setTabTitle(conv, data.titre);
    afficher("assistant", data.reponse, data.citations);
  } catch (err) {
    setBusy(conv, false);
    afficher("error", `Impossible de joindre le serveur local : ${err}`);
  }
  return false;
}

// ------------------------------------------------------------ conversations

const occupees = new Set();

function currentConversation() {
  return Number(chat().dataset.conversation);
}

function tabOf(id) {
  return document.querySelector(`.conv-tab[data-id="${Number(id)}"]`);
}

function setBusy(id, busy) {
  if (busy) occupees.add(id);
  else occupees.delete(id);
  tabOf(id)?.classList.toggle("busy", busy);
}

function setTabTitle(id, titre) {
  const bouton = tabOf(id)?.querySelector(".conv-open");
  if (bouton) {
    bouton.textContent = titre;
    bouton.title = titre;
  }
}

function markActive(id) {
  document.querySelectorAll(".conv-tab").forEach((t) => {
    const actif = Number(t.dataset.id) === id;
    t.classList.toggle("active", actif);
    t.setAttribute("aria-selected", actif ? "true" : "false");
    if (actif) {
      t.classList.remove("fresh");
      t.scrollIntoView({ block: "nearest", inline: "nearest" });
    }
  });
  // Cookie lu par le serveur : la conversation reste ouverte d'une page a l'autre.
  document.cookie = `conversation=${id}; path=/; max-age=31536000; SameSite=Strict`;
}

// force : recharger meme si c'est deja la conversation affichee. Necessaire apres une
// creation : SQLite peut redonner a la nouvelle conversation le numero de celle qu'on
// vient de supprimer, et l'ancien contenu resterait a l'ecran.
async function openConversation(id, force = false) {
  id = Number(id);
  // Onglet en attente de confirmation : ce clic confirme la suppression.
  if (tabOf(id)?.classList.contains("confirming")) return closeConversation(id);
  if (!force && id === currentConversation() && chat().childElementCount) return markActive(id);
  try {
    const resp = await fetch(`/fragment/chat/${id}`);
    if (!resp.ok) throw new Error(`erreur ${resp.status}`);
    const box = chat();
    box.innerHTML = await resp.text();
    box.dataset.conversation = String(id);
    renderHistory(box);
    if (occupees.has(id)) addPending();
    markActive(id);
    scrollChat();
    document.getElementById("q").focus();
  } catch (err) {
    toast(`Impossible d'ouvrir la conversation : ${err.message}`, "error");
  }
}

function tabElement(id, titre) {
  const tab = document.createElement("div");
  tab.className = "conv-tab";
  tab.setAttribute("role", "tab");
  tab.dataset.id = String(id);
  tab.dataset.count = "0";
  const ouvrir = document.createElement("button");
  ouvrir.className = "conv-open";
  ouvrir.textContent = titre;
  ouvrir.title = titre;
  ouvrir.onclick = () => openConversation(id);
  const fermer = document.createElement("button");
  fermer.className = "conv-close";
  fermer.setAttribute("aria-label", "Fermer la conversation");
  fermer.title = "Fermer la conversation";
  fermer.innerHTML = '<svg class="ico"><use href="#i-x"/></svg>';
  fermer.onclick = () => closeConversation(id);
  tab.append(ouvrir, fermer);
  return tab;
}

async function newConversation() {
  // Une conversation vide existe deja : on y va plutot que d'en empiler une autre.
  const vide = [...document.querySelectorAll(".conv-tab")].find(
    (t) => t.dataset.count === "0" && !occupees.has(Number(t.dataset.id)),
  );
  if (vide) return openConversation(vide.dataset.id);
  try {
    const resp = await fetch("/api/conversations", { method: "POST", headers: { "X-Assistant": "1" } });
    if (!resp.ok) throw new Error(`erreur ${resp.status}`);
    const data = await resp.json();
    document.getElementById("conv-tabs").appendChild(tabElement(data.id, data.titre));
    await openConversation(data.id, true);
  } catch (err) {
    toast(`Impossible de créer la conversation : ${err.message}`, "error");
  }
}

// Confirmation dans l'onglet lui-meme, sans fenetre : la croix fait passer l'onglet
// en rouge (meme titre, donc meme largeur) ; un clic sur l'onglet confirme. Un clic
// ailleurs, Echap ou quelques secondes sans rien faire annulent.
const DELAI_CONFIRMATION = 4000;

function cancelConfirm(tab) {
  if (!tab || !tab.classList.contains("confirming")) return;
  clearTimeout(tab._minuteur);
  tab.classList.remove("confirming");
  tab.querySelector(".conv-open").title = tab.dataset.titre;
}

function askConfirm(tab) {
  document.querySelectorAll(".conv-tab.confirming").forEach(cancelConfirm);
  const bouton = tab.querySelector(".conv-open");
  tab.dataset.titre = bouton.title;
  tab.classList.add("confirming");
  bouton.title = "Cliquer pour supprimer cette conversation et ses messages";
  tab._minuteur = setTimeout(() => cancelConfirm(tab), DELAI_CONFIRMATION);
}

// Clic hors de l'onglet en attente, ou Echap : annulation.
document.addEventListener("click", (event) => {
  const enAttente = document.querySelector(".conv-tab.confirming");
  if (enAttente && !enAttente.contains(event.target)) cancelConfirm(enAttente);
}, true);
document.addEventListener("keydown", (event) => {
  if (event.key === "Escape") document.querySelectorAll(".conv-tab.confirming").forEach(cancelConfirm);
});

async function closeConversation(id) {
  id = Number(id);
  const tab = tabOf(id);
  if (!tab) return;
  // Conversation vide : rien a perdre, on ferme directement.
  if (Number(tab.dataset.count || 0) > 0 && !tab.classList.contains("confirming")) {
    askConfirm(tab);
    return;
  }
  clearTimeout(tab._minuteur);
  try {
    const resp = await fetch(`/api/conversations/${id}`, { method: "DELETE", headers: { "X-Assistant": "1" } });
    if (!resp.ok) throw new Error(`erreur ${resp.status}`);
  } catch (err) {
    toast(`Impossible de supprimer la conversation : ${err.message}`, "error");
    return;
  }
  occupees.delete(id);
  const voisin = tab.nextElementSibling || tab.previousElementSibling;
  tab.remove();
  if (id !== currentConversation()) return;
  if (voisin) openConversation(voisin.dataset.id);
  else newConversation();
}

function askExample(button) {
  document.getElementById("q").value = button.textContent.trim();
  sendQuestion();
}

// Boutons "Que dois-je en faire ?" (mail) et "Résumer ce document" (fichier).
function askAbout(button) {
  const { doc, kind, title } = button.dataset;
  const question =
    kind === "mail"
      ? `Que dois-je faire au sujet du mail « ${title} » ? Réponds concrètement.`
      : `Résume le document « ${title} » : de quoi parle-t-il et quels sont les points clés ?`;
  document.getElementById("q").value = question;
  sendQuestion(null, doc);
}

// La zone de saisie grandit avec le texte, jusqu'a une limite.
function autoGrow(el) {
  el.style.height = "auto";
  el.style.height = `${Math.min(el.scrollHeight, 160)}px`;
}

// ------------------------------------------------------------ synchronisation

// Petit message en bas de l'ecran, qui disparait seul.
let toastTimer = null;
function toast(message, kind = "") {
  const el = document.getElementById("toast");
  if (!el) return;
  el.textContent = message;
  el.className = `toast show ${kind}`;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => (el.className = "toast"), kind === "error" ? 7000 : 3500);
}

async function runSync(button) {
  if (button.classList.contains("spinning")) return;
  button.classList.add("spinning");
  toast("Synchronisation en cours…");
  try {
    const resp = await fetch("/api/sync", { method: "POST" });
    const data = await resp.json();
    if (!resp.ok) throw new Error(data.detail || `erreur ${resp.status}`);
    if (data.ignore) toast("Une synchronisation est déjà en cours.");
    else if (data.erreurs && data.erreurs.length) toast(`Synchronisation incomplète : ${data.erreurs[0]}`, "error");
    else toast(data.modifies ? `À jour : ${data.resume}` : "Tout est déjà à jour.", "ok");
    if (!window.htmx) return;
    // Rafraichit sans attendre le prochain cycle. L'evenement "refresh" passe par les
    // attributs htmx de l'element, donc le mail ouvert reste en surbrillance.
    document.querySelectorAll("#mails, #status").forEach((el) => window.htmx.trigger(el, "refresh"));
    // Agenda et arbre de fichiers n'ont pas de rafraichissement partiel : on recharge,
    // sauf si une question est en cours (sa reponse serait perdue).
    const pageStatique = !document.body.classList.contains("tab-mails");
    if (pageStatique && !document.querySelector(".msg.pending")) window.location.reload();
  } catch (err) {
    toast(`Échec de la synchronisation : ${err.message || err}`, "error");
  } finally {
    button.classList.remove("spinning");
  }
}

// ------------------------------------------------------------ listes et arbre

// Surbrillance de l'element ouvert dans une liste (mails) ou l'arbre (fichiers).
// L'identifiant est garde dans window[cle] pour survivre au rafraichissement periodique.
function selectItem(el, cle) {
  document.querySelectorAll(".selected[data-id]").forEach((x) => x.classList.remove("selected"));
  el.classList.add("selected");
  window[cle] = Number(el.dataset.id);
}

function toggleTree(button) {
  const dossiers = document.querySelectorAll(".tree details");
  const ouvrir = [...dossiers].some((d) => !d.open);
  dossiers.forEach((d) => (d.open = ouvrir));
  button.textContent = ouvrir ? "Tout replier" : "Tout déplier";
}

// ----------------------------------------------------------------- fichiers

async function openFile(docId, button) {
  try {
    const resp = await fetch(`/api/open/${Number(docId)}`, {
      method: "POST",
      // En-tete exige par le serveur : une page tierce ne peut pas l'envoyer.
      headers: { "X-Assistant": "1" },
    });
    if (!resp.ok) {
      const data = await resp.json().catch(() => ({}));
      throw new Error(data.detail || `erreur ${resp.status}`);
    }
    if (button) {
      button.classList.add("done");
      setTimeout(() => button.classList.remove("done"), 1500);
    }
    toast("Fichier ouvert dans son application.", "ok");
  } catch (err) {
    toast(`Impossible d'ouvrir le fichier : ${err.message}`, "error");
  }
}

// ------------------------------------------------------------ mails en HTML

// Autorise (scope "mail" ou "sender") ou retire ("none") les images distantes.
// Le choix est enregistre en base : inutile de le refaire a la prochaine lecture.
async function setImages(docId, scope) {
  const body = new FormData();
  body.append("scope", scope);
  try {
    const resp = await fetch(`/api/mail/${Number(docId)}/images`, {
      method: "POST",
      headers: { "X-Assistant": "1" },
      body,
    });
    if (!resp.ok) throw new Error(`erreur ${resp.status}`);
  } catch (err) {
    toast(`Impossible d'enregistrer ce choix : ${err.message}`, "error");
    return;
  }
  if (window.htmx) window.htmx.ajax("GET", `/fragment/mail/${Number(docId)}`, "#mail-detail");
  else window.location.reload();
}

function switchView(button, mode) {
  button.closest(".mail-reader").dataset.view = mode;
}

// Ouvre un mail cite par l'assistant. Sur l'onglet Mails, sans recharger la page.
function openMail(docId) {
  const id = Number(docId);
  if (!document.body.classList.contains("tab-mails") || !window.htmx) {
    window.location.href = `/?mail=${id}`;
    return;
  }
  window.htmx.ajax("GET", `/fragment/mail/${id}`, "#mail-detail");
  const ligne = document.querySelector(`.mail[data-id="${id}"]`);
  if (ligne) {
    selectItem(ligne, "selectedMail");
    ligne.scrollIntoView({ block: "nearest" });
  } else {
    window.selectedMail = id;
  }
  // Les autres parametres sont conserves : ouvrir un mail cite ne doit pas
  // renvoyer l'utilisateur sur la boite de reception non filtree.
  const url = new URL(window.location.href);
  url.searchParams.set("mail", id);
  history.replaceState(history.state, "", url.pathname + url.search);
}

// Liens porteurs de data-open (fichier) ou data-mail (mail), dans les reponses.
document.addEventListener("click", (event) => {
  if (event.ctrlKey || event.metaKey || event.shiftKey || event.button !== 0) return;
  const fichier = event.target.closest("[data-open]");
  const mail = event.target.closest("[data-mail]");
  if (fichier) {
    event.preventDefault();
    openFile(fichier.dataset.open);
  } else if (mail) {
    event.preventDefault();
    openMail(mail.dataset.mail);
  }
});

// ------------------------------------------------------------ redimensionnement

// Largeur minimale de chaque zone, et place toujours laissee au reste de l'ecran.
const BORNES = {
  chat: { min: 280, reste: 520 },
  list: { min: 220, reste: 320 },
};

function largeurActuelle(cle) {
  const el = document.querySelector(cle === "chat" ? ".chat-panel" : ".list-col");
  return el ? el.getBoundingClientRect().width : 0;
}

function appliquerLargeur(cle, px) {
  const total = cle === "chat" ? window.innerWidth : document.querySelector(".content").clientWidth;
  const b = BORNES[cle];
  const largeur = Math.round(Math.max(b.min, Math.min(px, total - b.reste)));
  document.documentElement.style.setProperty(`--${cle}-width`, `${largeur}px`);
  return largeur;
}

function memoriserLargeur(cle, px) {
  try {
    if (px === null) localStorage.removeItem(`largeur-${cle}`);
    else localStorage.setItem(`largeur-${cle}`, String(px));
  } catch (err) {
    /* stockage indisponible : la largeur ne vaut que pour cette page */
  }
}

function initResizers() {
  document.querySelectorAll(".resizer").forEach((poignee) => {
    const cle = poignee.dataset.resize;
    // La conversation est a droite : on l'elargit en tirant vers la GAUCHE.
    const largeurAuPoint = (x) =>
      cle === "chat" ? window.innerWidth - x : x - poignee.parentElement.getBoundingClientRect().left;

    poignee.addEventListener("pointerdown", (event) => {
      if (event.button !== 0) return;
      event.preventDefault();
      poignee.setPointerCapture(event.pointerId);
      poignee.classList.add("active");
      document.body.classList.add("resizing");
      // Rien n'est memorise sans deplacement : un double-clic (retour a la largeur
      // par defaut) ne doit pas etre suivi d'une sauvegarde de l'ancienne largeur.
      let derniere = null;
      const bouger = (ev) => (derniere = appliquerLargeur(cle, largeurAuPoint(ev.clientX)));
      const finir = () => {
        poignee.removeEventListener("pointermove", bouger);
        poignee.classList.remove("active");
        document.body.classList.remove("resizing");
        if (derniere !== null) memoriserLargeur(cle, derniere);
      };
      poignee.addEventListener("pointermove", bouger);
      poignee.addEventListener("lostpointercapture", finir, { once: true });
    });

    // Double-clic : retour a la largeur par defaut.
    poignee.addEventListener("dblclick", () => {
      document.documentElement.style.removeProperty(`--${cle}-width`);
      memoriserLargeur(cle, null);
    });

    // Clavier : fleches gauche/droite quand la poignee a le focus.
    poignee.addEventListener("keydown", (event) => {
      const pas = { ArrowLeft: -24, ArrowRight: 24 }[event.key];
      if (!pas) return;
      event.preventDefault();
      const px = appliquerLargeur(cle, largeurActuelle(cle) + (cle === "chat" ? -pas : pas));
      memoriserLargeur(cle, px);
    });
  });
}

// -------------------------------------------------------------- chargement

// L'historique arrive rendu par le serveur, en texte brut. On le repasse par le
// meme convertisseur pour une mise en forme identique. Partir de textContent
// garantit qu'aucun HTML n'est reinjecte.
function renderHistory(root) {
  root.querySelectorAll(".msg.assistant").forEach((msg) => {
    const el = msg.querySelector(".bubble");
    if (!el || el.classList.contains("md")) return;
    let citations = [];
    try {
      citations = JSON.parse(msg.dataset.citations || "[]");
    } catch (err) {
      citations = [];
    }
    el.classList.add("md");
    el.innerHTML = renderMarkdown(el.textContent.trim(), citations);
    const sources = renderSources(citations);
    if (sources) msg.appendChild(sources);
  });
}

document.addEventListener("DOMContentLoaded", () => {
  renderHistory(document);
  scrollChat();
  initResizers();
  document.querySelector(".conv-tab.active")?.scrollIntoView({ block: "nearest", inline: "nearest" });

  // Agenda : on arrive positionne juste avant le premier evenement de la semaine.
  const grille = document.getElementById("cal-scroll");
  if (grille) grille.scrollTop = Number(grille.dataset.scroll || 0);
});
