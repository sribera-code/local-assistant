"""Ligne de commande de l'assistant local."""

from __future__ import annotations

import webbrowser
from typing import Any, Optional

import typer
from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from . import agent, db, indexer, pipeline, triage
from .config import ROOT, get_config
from .llm import LLM, OllamaError

app = typer.Typer(
    add_completion=False,
    help="Assistant local : Gmail + Agenda Google + fichiers locaux, via Ollama.",
    no_args_is_help=True,
)
console = Console()


def _open() -> tuple[object, LLM, object]:
    cfg = get_config()
    conn = db.connect(cfg.paths.db_path, embed_dim=cfg.ollama.embed_dim)
    return cfg, LLM(cfg.ollama), conn


def _fail(message: str) -> None:
    console.print(f"[bold red]x[/] {message}")
    raise typer.Exit(code=1)


# ------------------------------------------------------------------- diagnostic


@app.command()
def doctor() -> None:
    """Verifie la configuration, Ollama, les modeles et les acces Google."""
    try:
        cfg = get_config()
    except (FileNotFoundError, ValueError) as exc:
        _fail(str(exc))
        return

    table = Table(show_header=False, box=None, padding=(0, 2))

    def line(label: str, ok: bool | None, detail: str = "") -> None:
        mark = "[green]ok[/]" if ok else ("[yellow]--[/]" if ok is None else "[red]x[/]")
        table.add_row(mark, label, detail)

    line("config.yaml", True, str(ROOT / "config.yaml"))

    llm = LLM(cfg.ollama)
    try:
        installed = llm.available_models()
        line("Ollama", True, f"{cfg.ollama.host} - {len(installed)} modeles installes")
        missing = llm.check_models()
        if missing:
            line("Modeles", False, "absents : " + ", ".join(missing))
            for name in missing:
                console.print(f"    [dim]ollama pull {name}[/]")
        else:
            line(
                "Modeles",
                True,
                f"chat={cfg.ollama.chat_model} tri={cfg.ollama.triage_model} "
                f"embed={cfg.ollama.embed_model}",
            )
    except OllamaError as exc:
        line("Ollama", False, str(exc).splitlines()[0])

    secret_ok = cfg.paths.client_secret_path.exists()
    line("Identifiants Google", secret_ok, str(cfg.paths.client_secret_path))
    token_ok = cfg.paths.token_path.exists()
    line("Compte connecte", token_ok if secret_ok else None, "lance `assistant auth`" if not token_ok else "")

    if cfg.files.roots:
        line("Dossiers locaux", True, ", ".join(cfg.files.roots))
    else:
        line("Dossiers locaux", None, "files.roots est vide dans config.yaml")

    if cfg.matrix.user_id:
        from .matrix.bot import load_session

        session = load_session(cfg)
        if session is None:
            line("Matrix", False, "lance `assistant matrix-login`")
        elif not cfg.matrix.allowed_users:
            line("Matrix", False, "matrix.allowed_users est vide : personne ne peut lui parler")
        else:
            etat = "demarre avec serve" if cfg.matrix.enabled else "`assistant matrix` pour le lancer"
            line("Matrix", True, f"{session.user_id} (appareil {session.device_id}) - {etat}")

    try:
        conn = db.connect(cfg.paths.db_path, embed_dim=cfg.ollama.embed_dim)
        stats = db.stats(conn)
        line("Base locale", True, f"{stats['documents']} - {stats['chunks']} chunks")
    except Exception as exc:
        line("Base locale", False, str(exc))

    console.print(Panel(table, title="Diagnostic", border_style="blue"))

    # Test de bout en bout des embeddings : c'est ce qui casse le plus souvent
    # (dimension differente de celle annoncee dans config.yaml).
    try:
        vector = llm.embed_one("test de dimension")
        console.print(f"[green]ok[/]  embeddings : dimension {len(vector)}")
    except OllamaError as exc:
        console.print(f"[red]x[/]  embeddings : {exc}")
    finally:
        llm.close()


@app.command()
def auth() -> None:
    """Connecte le compte Google (lecture seule) et enregistre le jeton en local."""
    from .ingest.google_auth import get_credentials

    cfg = get_config()
    try:
        # Seule commande autorisee a ouvrir un navigateur.
        get_credentials(cfg.paths.client_secret_path, cfg.paths.token_path, interactive=True)
    except FileNotFoundError as exc:
        _fail(str(exc))
        return
    console.print(f"[green]ok[/] jeton enregistre : {cfg.paths.token_path}")


# ------------------------------------------------------------------ ingestion


@app.command()
def sync(
    full: bool = typer.Option(False, "--full", help="Ignore les curseurs et resynchronise tout."),
    source: Optional[str] = typer.Option(
        None, "--source", help="Limiter a une source : mail, event ou file."
    ),
    no_triage: bool = typer.Option(False, "--no-triage", help="Ne pas trier les nouveaux mails."),
) -> None:
    """Synchronise les sources, indexe ce qui a change et trie les nouveaux mails."""
    cfg, llm, conn = _open()
    sources = (source,) if source else pipeline.SOURCES
    if source and source not in pipeline.SOURCES:
        _fail(f"source inconnue : {source} (attendu : {', '.join(pipeline.SOURCES)})")

    with console.status("synchronisation...") as status:
        report = pipeline.sync_all(
            conn,
            llm,
            cfg,
            sources=sources,
            full=full,
            do_triage=not no_triage,
            notify_user=False,
            progress=lambda msg: status.update(msg),
        )
    llm.close()

    console.print(f"[green]ok[/] {report.summary()}")
    for name, err in report.errors.items():
        console.print(f"[yellow]![/] {name} : {err}")
    for notif in report.notifications:
        console.print(f"  [bold]{notif['urgency']}/5[/] {notif['titre']}")


@app.command()
def index() -> None:
    """Calcule les embeddings manquants (documents jamais indexes ou modifies)."""
    cfg, llm, conn = _open()
    pending = indexer.pending_document_ids(conn)
    if not pending:
        console.print("[green]ok[/] index a jour")
        llm.close()
        return

    with typer.progressbar(length=len(pending), label=f"{len(pending)} documents") as bar:
        done = {"n": 0}

        def tick(position: int, total: int) -> None:
            bar.update(position - done["n"])
            done["n"] = position

        chunks = indexer.index_documents(conn, llm, cfg, pending, progress=tick)
    llm.close()
    console.print(f"[green]ok[/] {chunks} chunks indexes")


@app.command(name="triage")
def triage_cmd(
    notify: bool = typer.Option(False, "--notify", help="Afficher aussi les notifications Windows."),
    force: bool = typer.Option(False, "--force", help="Retrier aussi les mails deja classes."),
) -> None:
    """Trie les mails qui ne l'ont pas encore ete."""
    cfg, llm, conn = _open()
    with console.status("tri des mails..."):
        results = triage.run(conn, llm, cfg, None, notify_user=notify, force=force)
    llm.close()
    console.print(f"[green]ok[/] tri termine, {len(results)} notification(s)")


@app.command(name="reset-index")
def reset_index(
    yes: bool = typer.Option(False, "--yes", help="Ne pas demander de confirmation.")
) -> None:
    """Supprime chunks et embeddings (les documents sont conserves) pour tout rembedder."""
    cfg, llm, conn = _open()
    llm.close()
    if not yes and not typer.confirm("Supprimer tous les chunks et embeddings ?"):
        raise typer.Abort()
    pipeline.reset_index(conn, cfg)
    console.print("[green]ok[/] index vide. Lance `assistant index` pour le reconstruire.")


# ----------------------------------------------------------------------- usage


@app.command()
def ask(
    question: list[str] = typer.Argument(..., help="La question a poser."),
    think: bool = typer.Option(False, "--think", help="Activer le raisonnement explicite du modele."),
) -> None:
    """Pose une question en ligne de commande."""
    cfg, llm, conn = _open()
    text = " ".join(question)
    with console.status(f"[dim]{cfg.ollama.chat_model}[/] reflechit..."):
        try:
            answer = agent.ask(conn, llm, cfg, text, think=think)
        except OllamaError as exc:
            llm.close()
            _fail(str(exc))
            return
    llm.close()

    console.print()
    console.print(answer.text)
    if answer.citations:
        console.print()
        for cite in answer.citations:
            # Le crochet ouvrant est echappe pour rich, qui l'interpreterait sinon
            # comme une balise de style.
            console.print(
                f"[dim]\\[{cite['doc_id']}] {cite['source']} · {cite['titre']} · {cite['date']}[/]"
            )
    if answer.tool_trace:
        console.print(f"\n[dim]outils : {' -> '.join(t['outil'] for t in answer.tool_trace)}[/]")


@app.command()
def calendars() -> None:
    """Liste les agendas indexes, pour choisir ceux a exclure.

    Beaucoup d'agendas d'abonnement (fete des prenoms, lever du soleil, numero du
    jour) n'apportent rien et noient les vrais rendez-vous.
    """
    import json

    cfg = get_config()
    conn = db.connect(cfg.paths.db_path, embed_dim=cfg.ollama.embed_dim)

    agendas: dict[str, dict[str, Any]] = {}
    for row in db.iter_rows(
        conn, "SELECT meta FROM documents WHERE source = 'event'"
    ):
        meta = json.loads(row["meta"] or "{}")
        cid = meta.get("calendar_id") or "?"
        entry = agendas.setdefault(cid, {"nom": meta.get("calendar_name") or "", "n": 0})
        entry["n"] += 1

    if not agendas:
        console.print("[yellow]![/] aucun evenement indexe. Lance `assistant sync` d'abord.")
        return

    table = Table(box=None, padding=(0, 2))
    table.add_column("evenements", justify="right")
    table.add_column("agenda")
    table.add_column("identifiant", overflow="fold", style="dim")
    for cid, info in sorted(agendas.items(), key=lambda kv: -kv[1]["n"]):
        table.add_row(str(info["n"]), info["nom"] or "(sans nom)", cid)
    console.print(table)

    console.print(
        "\n[dim]Pour en exclure un, ajoute un fragment de son identifiant dans"
        " config.yaml :[/]\n"
        '  calendar:\n    exclude_patterns: ["#weeknum@", "un-fragment-d-identifiant"]\n'
        "[dim]puis : assistant sync --source event[/]"
    )


@app.command()
def stats() -> None:
    """Affiche le contenu de l'index local."""
    cfg = get_config()
    conn = db.connect(cfg.paths.db_path, embed_dim=cfg.ollama.embed_dim)
    data = db.stats(conn)
    labels = {"mail": "mails", "event": "evenements", "file": "fichiers"}

    table = Table(title="Index local", box=None, padding=(0, 2))
    table.add_column("source")
    table.add_column("documents", justify="right")
    for source, count in sorted(data["documents"].items()):
        table.add_row(labels.get(source, source), str(count))
    table.add_row("", "")
    table.add_row("chunks indexes", str(data["chunks"]))
    table.add_row("mails tries", str(data["triaged"]))
    console.print(table)

    pending = indexer.pending_document_ids(conn)
    if pending:
        console.print(f"[yellow]![/] {len(pending)} document(s) en attente d'indexation")


# ---------------------------------------------------------------------- matrix


@app.command(name="matrix-login")
def matrix_login() -> None:
    """Connecte le compte Matrix du bot et signe son appareil (une fois)."""
    import asyncio

    from .matrix.bot import MatrixError, login
    from .matrix.crosssign import CrossSigningError

    cfg = get_config()
    if not cfg.matrix.user_id:
        _fail("renseigne matrix.user_id (le compte du bot) dans config.yaml")
    # Demande a chaque fois, jamais enregistre : seul le jeton d'acces l'est.
    password = typer.prompt(f"Mot de passe de {cfg.matrix.user_id}", hide_input=True)
    try:
        session = asyncio.run(login(cfg, password))
    except (MatrixError, CrossSigningError) as exc:
        _fail(str(exc))
        return
    console.print(f"[green]ok[/] {session.user_id} connecte, appareil {session.device_id} signe")
    console.print(f"    session : {cfg.paths.matrix_path}")
    if not cfg.matrix.allowed_users:
        console.print("[yellow]![/] matrix.allowed_users est vide : ajoute ton propre compte")


@app.command(name="matrix")
def matrix_run() -> None:
    """Lance le bot Matrix et la synchro de fond, sans l'interface web."""
    import asyncio

    from .matrix.bot import MatrixError, run_forever, setup_logging
    from .matrix.crosssign import CrossSigningError

    cfg, llm, _ = _open()
    setup_logging()
    # Comme `serve` : sans synchro, aucun nouveau mail, donc aucun resume a envoyer.
    syncer = pipeline.BackgroundSync(llm, cfg) if cfg.web.background_sync else None
    if syncer:
        syncer.start()
    console.print("[green]->[/] bot Matrix lance (Ctrl+C pour arreter)")
    try:
        asyncio.run(run_forever(cfg, llm))
    except (MatrixError, CrossSigningError) as exc:
        _fail(str(exc))
    except KeyboardInterrupt:
        pass
    finally:
        if syncer:
            syncer.stop()
        llm.close()


@app.command(name="matrix-trust")
def matrix_trust(user_id: str = typer.Argument(..., help="Compte dont accepter la nouvelle identite.")) -> None:
    """Accepte la nouvelle identite Matrix d'un utilisateur (apres une reinitialisation)."""
    from .matrix.bot import forget_identity

    if forget_identity(get_config(), user_id):
        console.print(f"[green]ok[/] l'identite de {user_id} sera reprise a son prochain message")
    else:
        console.print(f"[yellow]![/] aucune identite enregistree pour {user_id}")


@app.command()
def serve(
    host: Optional[str] = typer.Option(None, help="Adresse d'ecoute."),
    port: Optional[int] = typer.Option(None, help="Port d'ecoute."),
    reload: bool = typer.Option(False, "--reload", help="Rechargement auto (developpement)."),
) -> None:
    """Lance l'interface web locale."""
    import uvicorn

    cfg = get_config()
    host = host or cfg.web.host
    port = port or cfg.web.port
    url = f"http://{host}:{port}/"

    console.print(f"[green]->[/] interface : [bold]{url}[/]  (Ctrl+C pour arreter)")
    if cfg.web.open_browser and not reload:
        webbrowser.open(url)

    uvicorn.run(
        "assistant.web.app:app",
        host=host,
        port=port,
        reload=reload,
        log_level="warning",
    )


if __name__ == "__main__":
    app()
