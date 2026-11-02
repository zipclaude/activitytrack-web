# SPDX-License-Identifier: GPL-3.0-or-later
"""Template context of every page."""

import json

from django.utils.functional import SimpleLazyObject
from django.utils.translation import gettext as _

from . import data


def tags(request):
    """name -> colour of the tags the user sees (issue #133), read when a page shows tags."""
    user = request.user
    return {"tag_colors": SimpleLazyObject(lambda: data.tag_colors(user) if user.is_authenticated else {})}


def version(request):
    """The server's version, in the menu and on the login page: what the beta's reports ask for."""
    from atweb import __version__

    return {"server_version": __version__}


def picker_texts(request):
    """The texts of the date and time pickers (pickers.js, issue #38)."""
    return {"picker_texts": json.dumps({
        "before": _("▲ Prima"), "after": _("Dopo ▼"), "exact": _("Orario esatto"), "ok": _("OK"), "cancel": _("Annulla"),
        "clear": _("Svuota"), "today": _("Oggi"), "now": _("Adesso"), "start": _("Inizio"), "end": _("Fine"),
        "pick_end": _("Tocca l'ultima casella"), "invalid": _("Data o ora non valida"), "date": _("Data"), "time": _("Ora"),
        "fmt_eu": _("gg/mm/aaaa"), "fmt_us": _("mm/gg/aaaa"), "fmt_iso": _("aaaa-mm-gg"), "fmt_t24": _("hh:mm"), "fmt_t12": _("h:mm AM"),
    }, ensure_ascii=False)}
