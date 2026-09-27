"""Notifications Windows (toasts). Silencieux si la dependance manque ou hors Windows."""

from __future__ import annotations

import sys
from datetime import datetime

from .config import TriageConfig


def in_quiet_hours(cfg: TriageConfig, now: datetime | None = None) -> bool:
    """Vrai si on est dans la plage silencieuse (peut traverser minuit)."""
    quiet = cfg.quiet_hours
    if not quiet:
        return False
    hour = (now or datetime.now()).hour
    start, end = int(quiet["start"]), int(quiet["end"])
    if start == end:
        return False
    if start < end:
        return start <= hour < end
    return hour >= start or hour < end  # ex. 22h -> 8h


def toast(title: str, body: str, *, url: str | None = None) -> bool:
    """Affiche une notification. Retourne False si l'affichage n'est pas possible."""
    if sys.platform != "win32":
        return False
    try:
        # notify, et surtout pas toast : toast() attend que la notification soit
        # cliquee ou fermee. Si Windows ne l'affiche pas (notifications de l'application
        # desactivees, « Ne pas deranger »), cette attente ne finit jamais, et bloque
        # avec elle la synchro de fond qui l'a appelee. notify() l'affiche et rend la
        # main ; le clic ouvre quand meme l'URL, par activation « protocol » de Windows.
        from win11toast import notify as _toast
    except ImportError:
        return False

    kwargs = {
        "title": title[:80],
        "body": body[:250],
        "app_id": "Assistant local",
        "duration": "short",
    }
    if url:
        # Un clic sur la notification ouvre l'interface locale.
        kwargs["on_click"] = url
    try:
        _toast(**kwargs)
        return True
    except Exception:
        return False
