# SPDX-License-Identifier: GPL-3.0-or-later
from django import template
from django.utils.html import format_html, format_html_join
from django.utils.safestring import mark_safe
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


# dots of a list row; the entry sheet shows every tag (issue #133)
DOTS = 4


def _hex(value):
    return f"#{value & 0xFFFFFF:06x}" if isinstance(value, int) else ""


@register.simple_tag
def tag_dots(tags, colors):
    """Up to four coloured dots and "+N"; a tag without colour (free, or none chosen) is grey."""
    tags = list(tags or [])
    if not tags:
        return ""
    dots = format_html_join("", '<i class="dot" style="--tag: {}"></i>', ((_hex(colors.get(t)) or "var(--tag-none)",) for t in tags[:DOTS]))
    more = format_html('<span class="more">+{}</span>', len(tags) - DOTS) if len(tags) > DOTS else ""
    return format_html('<span class="dots" title="{}">{}{}</span>', " ".join(tags), dots, more)


def _ink(value):
    """Dark or light text on a colour, by its luminance."""
    r, g, b = (value >> 16) & 255, (value >> 8) & 255, value & 255
    return "#1a1c1e" if 0.299 * r + 0.587 * g + 0.114 * b > 150 else "#ffffff"


@register.filter
def ink(value):
    """Text colour readable on a 0xRRGGBB background."""
    return _ink(value) if isinstance(value, int) else "#ffffff"


@register.simple_tag
def tag_chips(tags, colors):
    """Every tag as a coloured chip; free and colourless ones in the neutral chip."""
    out = []
    for t in tags or []:
        c = colors.get(t)
        if isinstance(c, int):
            out.append(format_html('<span class="tag" style="background: {}; color: {}">{}</span>', _hex(c), _ink(c), t))
        else:
            out.append(format_html('<span class="tag none">{}</span>', t))
    return mark_safe(" ".join(out))


@register.simple_tag
def icon(name, label=""):
    """An icon of web/_icons.html (the app's drawables); decorative unless labelled."""
    if label:
        return format_html('<svg class="i" role="img" aria-label="{}"><use href="#i-{}"/></svg>', label, name)
    return format_html('<svg class="i" aria-hidden="true"><use href="#i-{}"/></svg>', name)


@register.simple_tag(takes_context=True)
def nav_on(context, *names):
    """"on" when the page is one of the named views (namespace:name for apps)."""
    match = getattr(context.get("request"), "resolver_match", None)
    return "on" if match and match.view_name in names else ""
