# SPDX-License-Identifier: GPL-3.0-or-later
from django import template
from django.utils.translation import gettext_lazy as _

register = template.Library()

STATUS = {
    "ACTIVE": _("in corso"), "SUSPENDED": _("sospesa"), "TO_COMPLETE": _("dettagli mancanti"), "CLOSED": _("chiusa"),
}
MODE = {"WORK": _("lavoro"), "TRAVEL": _("viaggio"), "PAUSE": _("pausa")}
TODO = {"open": _("aperta"), "planned": _("pianificata"), "overdue": _("in ritardo"), "done": _("fatta"), "ignored": _("ignorata")}


@register.filter
def status(entry):
    if entry.status == "ACTIVE" and entry.mode:
        return MODE.get(entry.mode, entry.mode)
    return STATUS.get(entry.status, entry.status)


@register.filter
def kind(value):
    return MODE.get(value, value)


@register.filter
def todo_state(value):
    return TODO.get(value, value)


@register.filter
def eur(cents):
    if cents in (None, ""):
        return ""
    return f"{cents / 100:,.2f} €".replace(",", "X").replace(".", ",").replace("X", ".")


@register.filter
def color(value):
    """#rrggbb of a 0xRRGGBB master-data colour."""
    return f"#{value & 0xFFFFFF:06x}" if isinstance(value, int) else ""


@register.filter
def minutes_hm(m):
    return "" if m is None else f"{m // 60:02d}:{m % 60:02d}"
