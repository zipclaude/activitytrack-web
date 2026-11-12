# SPDX-License-Identifier: GPL-3.0-or-later
"""
The displayed states of an activity (ACTIVITY-MODEL §2.2) for the pages: one
table of icon, colour class and label shared by every page, the computation
from records.activity.display(), and the commands each state allows (§3).

The colour is the CSS class "st-<STATE>" of app.css; the icon a symbol of
web/_icons.html.
"""

from django.utils import timezone
from django.utils.translation import gettext_lazy as _, pgettext_lazy

from records import activity as A
from records.models import Segment

# state -> (icon, label); the order is the rules' one
UI = {
    A.DELETED: ("bin", _("Eliminata")),
    A.ABSENCE: ("dot", _("Assenza")),
    A.LATE: ("alert", _("In ritardo")),
    A.PLANNED: ("clock", _("Pianificata")),
    A.TODO: ("checkbox", pgettext_lazy("stato", "Da fare")),
    A.WORK: ("play", _("Lavoro")),
    A.TRAVEL: ("travel", _("Viaggio")),
    A.PAUSE: ("pause", _("Pausa")),
    A.SUSPENDED: ("suspend", _("Sospesa")),
    A.DONE: ("done", _("Fatta")),
    A.INCOMPLETE: ("stop", _("Incompleta")),
}

ABSENCE_LABELS = {
    "ABSENCE": _("Assenza"), "HOLIDAY": _("Ferie"), "LEAVE": _("Permesso"), "SICK": _("Malattia"),
}

PRIORITIES = (
    (0, _("Nessuna")), (1, _("Bassa")), (2, _("Media")), (3, _("Alta")), (4, _("Urgente")), (5, _("Emergenza")),
)

# the states in which an activity is "to do" (planned or not, late or not)
OPEN = (A.TODO, A.PLANNED, A.LATE)
RUNNING = (A.WORK, A.TRAVEL, A.PAUSE, A.SUSPENDED)


def missing_required(entry):
    """
    The plugins' required fields still empty (§2.4). The server knows no
    plugin descriptor (only the phone holding the plugins does), so it cannot
    tell: False, as the library does without descriptors.
    """
    return False


def annotate(entries, now=None, tz=None):
    """
    Sets .shown (the displayed state) and .shown_since on each entry and
    returns {uid: (state, since)}; one query for the segments, planned ones
    included.
    """
    entries = list(entries)
    now = now or timezone.now()
    tz = tz or timezone.get_current_timezone()
    segs = {}
    for s in Segment.objects.filter(entry__in=[e.uid for e in entries], deleted=False).order_by("start_utc"):
        segs.setdefault(s.entry_id, []).append(s)
    out = {}
    for e in entries:
        e.shown, e.shown_since = A.display(e, segs.get(e.uid, []), now, tz, missing_required(e))
        out[e.uid] = (e.shown, e.shown_since)
    return out


def label(state, entry=None):
    """The state's label; an absence says its kind (Ferie, Malattia...)."""
    if state == A.ABSENCE and entry is not None and entry.absence_kind:
        return ABSENCE_LABELS.get(entry.absence_kind, UI[state][1])
    return UI[state][1]


# the commands: key -> (label, icon, css class)
COMMANDS = {
    "start_work": (_("Avvia lavoro"), "play", "c-work"),
    "start_travel": (_("Viaggio"), "travel", "c-travel"),
    "tick": (_("Fatta"), "done", ""),
    "pause": (_("Pausa"), "pause", "c-pause"),
    "work": (_("Lavoro"), "play", "c-work"),
    "resume": (_("Riprendi"), "play", "c-work"),
    "travel": (_("Viaggio"), "travel", "c-travel"),
    "stop": (_("Stop"), "stop", "c-stop"),
    "reopen_work": (_("Riapri · Lavoro"), "play", ""),
    "reopen_travel": (_("Riapri · Viaggio"), "travel", ""),
    "untick": (_("Rimetti da fare"), "restore", ""),
    "delete": (_("Elimina"), "bin", "danger"),
    "restore": (_("Ripristina"), "restore", ""),
    "purge": (_("Elimina definitivamente"), "clear", "danger"),
}
# asked before they run
CONFIRM = {
    "reopen_work": _("Riaprire l'attività?"), "reopen_travel": _("Riaprire l'attività?"),
    "delete": _("Eliminare questa attività? Va nel cestino e si può ripristinare."),
    "purge": _("Eliminare definitivamente? Non si può annullare."),
}


def allowed(state, has_real_segments=True, previous=None):
    """
    The commands ACTIVITY-MODEL §3 allows in a displayed state, in the
    order of the buttons. There is no "suspend" button; [previous] is the
    kind before a pause (Riprendi goes back to it).
    """
    if state == A.DELETED:
        return ["restore", "purge"]
    if state == A.ABSENCE:
        return ["delete"]
    if state in OPEN:
        return ["start_work", "start_travel", "tick", "delete"]
    if state == A.WORK:
        return ["pause", "travel", "stop", "delete"]
    if state == A.TRAVEL:
        return ["work", "pause", "stop", "delete"]
    if state == A.PAUSE:
        other = "travel" if previous == A.WORK else "work"
        return ["resume", other, "stop", "delete"] if previous in (A.WORK, A.TRAVEL) else ["work", "travel", "stop", "delete"]
    if state == A.SUSPENDED:
        return ["work", "travel", "stop", "delete"]
    out = ["reopen_work", "reopen_travel"]
    return out + (["untick"] if not has_real_segments else []) + ["delete"]


def buttons(keys):
    """[(key, label, icon, css, confirm)] of command keys, for web/_actions.html."""
    return [(k, *COMMANDS[k], CONFIRM.get(k, "")) for k in keys]
