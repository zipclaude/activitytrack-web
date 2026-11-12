# SPDX-License-Identifier: GPL-3.0-or-later
"""
What the browser does, as changes through sync.engine.apply: the same
records, revisions and last-writer-wins rule as a phone's push, so every
phone gets the web's work at its next pull. The lifecycle is the activity
model's (the app's docs/ACTIVITY-MODEL.md §3): TODO, RUNNING (with at most
one open segment), CLOSED; deleting puts it in the bin (deleted_utc).
"""

import uuid
from datetime import datetime
from zoneinfo import ZoneInfo

from django.conf import settings as dj
from django.db import IntegrityError, transaction
from django.db.models import Q
from django.utils.translation import gettext as _

from records import activity as A
from records.models import ActivityKind, Entry, EntryStatus, Segment
from sync import engine

WORK, TRAVEL, PAUSE = "WORK", "TRAVEL", "PAUSE"
TODO, RUNNING, CLOSED = EntryStatus.TODO, EntryStatus.RUNNING, EntryStatus.CLOSED


class OpError(Exception):
    pass


def ms(dt):
    return int(dt.timestamp() * 1000)


def put(user, type_, uid, fields, central=False):
    """One upsert, every field stamped now; returns the stored record's fields."""
    now = engine.now_ms()
    change = {"type": type_, "uid": str(uid), "op": "upsert", "fields": fields, "times": {k: now for k in fields}}
    try:
        result = engine.apply(user, change, now=now, central=central)
    except engine.ChangeError as e:
        raise OpError(f"{e.code}: {e.detail}") from None
    if result["status"] == "conflict" and result["record"].get("deleted"):
        raise OpError("deleted")
    return result["record"]


def delete(user, type_, uid):
    try:
        engine.apply(user, {"type": type_, "uid": str(uid), "op": "delete", "time": engine.now_ms()})
    except engine.ChangeError as e:
        raise OpError(f"{e.code}: {e.detail}") from None


def new_uid():
    return str(uuid.uuid4())


def zone():
    return dj.TIME_ZONE


# ---- entries ----

def _open_segment(entry):
    return Segment.objects.filter(entry=entry, deleted=False, end_utc__isnull=True).order_by("-start_utc").first()


def _touch(user, entry, fields):
    fields = dict(fields)
    fields["updatedUtcMs"] = engine.now_ms()
    return put(user, "entry", entry.uid, fields)


def _close_open(user, entry, at):
    seg = _open_segment(entry)
    if seg:
        put(user, "segment", seg.uid, {"endUtcMs": max(ms(at), ms(seg.start_utc))})


def _open(user, entry, kind, at):
    put(user, "segment", new_uid(), {"entryUid": str(entry.uid), "kind": kind, "startUtcMs": ms(at), "zoneId": entry.zone_id, "source": "MANUAL"})


def complete_enough(entry):
    """What the app asks before closing: a client (or a company without clients) and a description."""
    return bool((entry.activity or "").strip()) and (entry.client_id is not None or entry.company_id is not None)


def concurrent_allowed(user):
    from policies.effective import effective
    import json

    text = effective(user)
    return True if not text else json.loads(text).get("features", {}).get("concurrentEntries", True)


def _drop_planned(user, entry):
    """The plan has served (§1.2): its segments go on start and tick."""
    for seg in Segment.objects.filter(entry=entry, deleted=False, planned=True):
        delete(user, "segment", seg.uid)


def _fields(company, client, role, activity):
    return {
        "activity": activity or None, "companyUid": str(company.uid) if company else None,
        "clientUid": str(client.uid) if client else None, "roleUid": str(role.uid) if role else None,
    }


def _new_activity(user, status, company, client, role, activity, **more):
    uid = new_uid()
    now = engine.now_ms()
    put(user, "entry", uid, {
        "kind": ActivityKind.ACTIVITY, "status": status, "mode": None, "zoneId": zone(),
        **_fields(company, client, role, activity), "createdUtcMs": now, "updatedUtcMs": now, **more,
    })
    return Entry.objects.get(uid=uid)


def _alone(user, entry, at):
    """
    Without concurrent activities what goes to work or travel suspends the user's others with an open
    segment, as a start does (the library's Work._step, app issue #132).
    """
    if concurrent_allowed(user):
        return
    others = Entry.objects.filter(owner=user, deleted=False, deleted_utc__isnull=True, status=RUNNING).exclude(mode=None)
    if entry is not None:
        others = others.exclude(uid=entry.uid)
    for other in others:
        suspend(user, other, at)


def start(user, *, company=None, client=None, role=None, activity="", todo=None, mode=WORK, at=None):
    """A new running activity, or [todo] (a TODO activity) started: TODO -> RUNNING."""
    at = at or datetime.now().astimezone()
    _alone(user, todo, at)
    if todo is not None:
        if todo.status != TODO:
            raise OpError("not to do")
        _drop_planned(user, todo)
        _touch(user, todo, {"status": RUNNING, "mode": mode, **_fields(company, client, role, activity)})
        entry = todo
    else:
        entry = _new_activity(user, RUNNING, company, client, role, activity, mode=mode)
    _open(user, entry, mode, at)
    entry.refresh_from_db()
    return entry


def record_past(user, *, start_at, end_at, company=None, client=None, role=None, activity="", todo=None):
    """A closed activity with the work done (the app's Lavoro · Passata), or [todo] closed with it."""
    if end_at <= start_at:
        raise OpError("end before start")
    if todo is not None:
        if todo.status != TODO:
            raise OpError("not to do")
        _drop_planned(user, todo)
        _touch(user, todo, {"status": CLOSED, "closedUtcMs": engine.now_ms(), **_fields(company, client, role, activity)})
        entry = todo
    else:
        entry = _new_activity(user, CLOSED, company, client, role, activity, closedUtcMs=engine.now_ms())
    put(user, "segment", new_uid(), {
        "entryUid": str(entry.uid), "kind": WORK, "startUtcMs": ms(start_at), "endUtcMs": ms(end_at), "zoneId": entry.zone_id, "source": "MANUAL",
    })
    entry.refresh_from_db()
    return entry


def _running(entry):
    if entry.status != RUNNING:
        raise OpError("not running")


def switch(user, entry, mode, at=None):
    """Pause, travel or back to work (resume when none is open): the open segment closes, one of [mode] opens."""
    at = at or datetime.now().astimezone()
    _running(entry)
    if mode in (WORK, TRAVEL):
        _alone(user, entry, at)
    _close_open(user, entry, at)
    _open(user, entry, mode, at)
    _touch(user, entry, {"mode": mode})


def suspend(user, entry, at=None):
    """The open segment closes, the activity stays running (no button in the app: another one started)."""
    at = at or datetime.now().astimezone()
    _running(entry)
    _close_open(user, entry, at)
    _touch(user, entry, {"mode": None})


def stop(user, entry, at=None):
    at = at or datetime.now().astimezone()
    _running(entry)
    _close_open(user, entry, at)
    _touch(user, entry, {"status": CLOSED, "mode": None, "closedUtcMs": engine.now_ms()})


def reopen(user, entry, mode=WORK, at=None):
    """CLOSED -> RUNNING with [mode] open; the gap since the end stays a gap."""
    at = at or datetime.now().astimezone()
    if entry.status != CLOSED or entry.kind != ActivityKind.ACTIVITY:
        raise OpError("not closed")
    _alone(user, entry, at)
    _open(user, entry, mode, at)
    _touch(user, entry, {"status": RUNNING, "mode": mode, "closedUtcMs": None})


def tick(user, entry):
    """A to-do done without work: TODO -> CLOSED, the plan's segments gone."""
    if entry.status != TODO:
        raise OpError("not to do")
    _drop_planned(user, entry)
    _touch(user, entry, {"status": CLOSED, "closedUtcMs": engine.now_ms()})


def to_bin(user, entry):
    """Into the bin ("eliminata"): restorable, hidden everywhere else."""
    _touch(user, entry, {"deletedUtcMs": engine.now_ms()})


def restore(user, entry):
    _touch(user, entry, {"deletedUtcMs": None})


def edit_entry(user, entry, fields):
    return _touch(user, entry, fields)


def overlapping(entry):
    """Two of the entry's segments overlap (an open one runs to infinity)."""
    segs = list(Segment.objects.filter(entry=entry, deleted=False, planned=False).order_by("start_utc"))
    return any(a.end_utc is None or a.end_utc > b.start_utc for a, b in zip(segs, segs[1:]))


def _review(user, entry):
    """The "to review" mark follows the segments, as the phones' (SYNC.md §11)."""
    entry.refresh_from_db()
    _touch(user, entry, {"needsReview": overlapping(entry)} if entry.needs_review or overlapping(entry) else {})


def edit_segment(user, segment, *, start_at, end_at, kind):
    if end_at is not None and end_at <= start_at:
        raise OpError("end before start")
    put(user, "segment", segment.uid, {"startUtcMs": ms(start_at), "endUtcMs": ms(end_at) if end_at else None, "kind": kind})
    _review(user, segment.entry)


def remove_segment(user, segment):
    delete(user, "segment", segment.uid)
    _review(user, segment.entry)


def add_segment(user, entry, *, start_at, end_at, kind):
    if end_at <= start_at:
        raise OpError("end before start")
    put(user, "segment", new_uid(), {"entryUid": str(entry.uid), "kind": kind, "startUtcMs": ms(start_at), "endUtcMs": ms(end_at), "zoneId": entry.zone_id, "source": "MANUAL"})
    _review(user, entry)


def add_expense(user, entry, *, cents, description):
    if cents <= 0 or not description.strip():
        raise OpError("amount and description")
    put(user, "expense", new_uid(), {"entryUid": str(entry.uid), "amountCents": cents, "description": description.strip(), "createdUtcMs": engine.now_ms()})
    _touch(user, entry, {})


# ---- to-dos: TODO activities ----

def save_todo(user, uid, fields, owner=None):
    """Creates or changes a TODO activity; a manager writes a team member's as theirs ([owner])."""
    user = owner or user
    now = engine.now_ms()
    fields = dict(fields, updatedUtcMs=now)
    if uid is None:
        uid = new_uid()
        fields = {"kind": ActivityKind.ACTIVITY, "status": TODO, "mode": None, "zoneId": zone(), "createdUtcMs": now, **fields}
    put(user, "entry", uid, fields)
    return uid


def untick(user, entry):
    """CLOSED without real segments -> TODO: a task ticked off by mistake (§3 untick)."""
    if entry.status != CLOSED or entry.kind != ActivityKind.ACTIVITY or real_segments(entry):
        raise OpError("not ticked")
    _touch(user, entry, {"status": TODO, "closedUtcMs": None})


def purge(user, entry):
    """Deleted for good: only an activity already in the bin (the web's bin page)."""
    if entry.deleted_utc is None:
        raise OpError("not in the bin")
    delete(user, "entry", entry.uid)


def real_segments(entry):
    segs = Segment.objects.filter(entry=entry, deleted=False, planned=False)
    return list(segs.order_by("start_utc"))


def previous_kind(entry):
    """The kind open before the pause now open (what "Riprendi" goes back to), else None."""
    before = [s for s in real_segments(entry) if s.end_utc is not None and s.kind in (WORK, TRAVEL)]
    return before[-1].kind if before else None


def travel_from_work(user, entry, completed, at=None):
    """
    The question of §3: "Hai completato l'attività lavorativa?" Yes: the activity
    stops and an anonymous one starts travelling at the same instant; no: it switches.
    """
    at = at or datetime.now().astimezone()
    if not completed:
        switch(user, entry, TRAVEL, at)
        return entry
    with transaction.atomic():
        stop(user, entry, at)
        return start(user, mode=TRAVEL, at=at)


def outbound(entry):
    """
    The travel §3.2 mirrors, when the activity has a return trip to offer: a TRAVEL
    segment followed later by a WORK one, and no TRAVEL after the last WORK; the
    last TRAVEL before the first WORK that follows a travel. None otherwise.
    """
    segs = real_segments(entry)
    works = [i for i, s in enumerate(segs) if s.kind == WORK]
    if not works or any(s.kind == TRAVEL for s in segs[works[-1]:]):
        return None
    travels = [i for i, s in enumerate(segs) if s.kind == TRAVEL and i < works[-1]]
    if not travels:
        return None
    first_work = next(i for i in works if i > travels[0])
    return [s for s in segs[:first_work] if s.kind == TRAVEL][-1]


def stop_with_return(user, entry, at=None):
    """
    §3.2 choice 3: close the open segment at [at], add a TRAVEL segment as long
    as the outbound one with its kilometres, and close the activity. The server
    stores no location samples, so the return has none.
    """
    at = at or datetime.now().astimezone()
    _running(entry)
    out = outbound(entry)
    if out is None or out.end_utc is None:
        raise OpError("no return trip")
    length = out.end_utc - out.start_utc
    with transaction.atomic():
        _close_open(user, entry, at)
        fields = {
            "entryUid": str(entry.uid), "kind": TRAVEL, "startUtcMs": ms(at),
            "endUtcMs": ms(at + length), "zoneId": entry.zone_id, "source": "MANUAL",
        }
        if out.km_estimated is not None:
            fields["kmEstimated"] = out.km_estimated
        if out.km_declared is not None:
            fields["kmDeclared"] = out.km_declared
        put(user, "segment", new_uid(), fields)
        _touch(user, entry, {"status": CLOSED, "mode": None, "closedUtcMs": engine.now_ms()})


def save_absence(user, uid, *, first, last, start_min=None, duration_min=None, absence_kind=A.ABSENCE, company=None, activity=None, notes=None,
                 owner=None):
    """
    An absence (§3.1): an ABSENCE activity with one segment a working day,
    with the times given or over the owner's working hours (days without
    them and days off skipped). Changing it writes its segments anew.
    """
    from records.models import DayOff, LunchBreak, WorkSchedule

    user = owner or user
    if last < first or (last - first).days >= 366:
        raise OpError("days")
    now = engine.now_ms()
    fields = {"absenceKind": absence_kind, "companyUid": str(company.uid) if company else None, "activity": activity, "notes": notes,
              "updatedUtcMs": now}
    if uid is None:
        uid = new_uid()
        fields.update({"kind": ActivityKind.ABSENCE, "status": CLOSED, "mode": None, "zoneId": zone(), "createdUtcMs": now})
    with transaction.atomic():
        put(user, "entry", uid, fields)
        entry = Entry.objects.get(uid=uid)
        for seg in Segment.objects.filter(entry=entry, deleted=False):
            delete(user, "segment", seg.uid)

        def rows(model):
            # the owner's and the central ones, as the migration reads them
            mine = model.objects.filter(org=user.org, deleted=False).filter(Q(owner=user) | Q(owner__isnull=True))
            return [(r.day_of_week, r.start_min, r.end_min) for r in mine]
        off = DayOff.objects.filter(org=user.org, deleted=False, date__range=(first, last)).filter(Q(owner=user) | Q(owner__isnull=True))
        off = off.values_list("date", flat=True)
        # one day with times is taken as chosen, as the app's AbsencePlanner (a permesso on a Saturday)
        spans = A.absence_segments(first, last, ZoneInfo(dj.TIME_ZONE), start_min, duration_min, rows(WorkSchedule), rows(LunchBreak), off,
                                   with_times_every_day=first == last)
        for _, _, a, b in spans:
            put(user, "segment", new_uid(), {
                "entryUid": str(uid), "kind": A.ABSENCE, "startUtcMs": ms(a), "endUtcMs": ms(b), "zoneId": entry.zone_id, "source": "MANUAL",
            })
        put(user, "entry", uid, {"closedUtcMs": max((ms(b) for *_, b in spans), default=now)})
    return uid


def _moved(rec, to, rev):
    """[rec] and its children change owner: the old owner's phones drop them ("gone")."""
    from sync.models import ScopeExit

    old = rec.owner_id
    type(rec).objects.filter(pk=rec.pk).update(owner=to, rev=rev)
    ScopeExit.objects.create(org=rec.org, user_id=old, type=rec.SYNC_TYPE, uid=rec.uid, rev=rev)
    for child, parent in engine._children(type(rec)):
        for c in child.objects.filter(org=rec.org, deleted=False, owner_id=old, **{parent: rec}):
            _moved(c, to, rev)


def assign(by, todo, to):
    """
    Gives a TODO activity to [to]: the new owner's phones pull it (and its
    planned segments, references...), the old owner's drop them ("gone",
    docs/SYNC.md §5); the server remembers who assigned it.
    """
    from records.models import ActivityAssignment

    if todo.status != TODO:
        raise OpError("not to do")
    try:
        with transaction.atomic():
            rev = engine.next_rev(todo.org)
            if todo.owner_id != to.pk:
                _moved(todo, to, rev)
                todo.refresh_from_db()
            if to.pk == by.pk:
                ActivityAssignment.objects.filter(entry=todo).delete()
            else:
                ActivityAssignment.objects.update_or_create(entry=todo, defaults={"by": by})
    except IntegrityError:
        # the external key is unique with the company for each owner: the new one has it already
        raise OpError(_("chi la riceve ha già un'attività con la stessa chiave esterna")) from None
