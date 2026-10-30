# SPDX-License-Identifier: GPL-3.0-or-later
"""
What the browser does, as changes through sync.engine.apply: the same
records, revisions and last-writer-wins rule as a phone's push, so every
phone gets the web's work at its next pull. The entry lifecycle mirrors the
app's (EntryStateMachine): ACTIVE with one open segment, SUSPENDED,
TO_COMPLETE, CLOSED.
"""

import uuid
from datetime import datetime

from django.conf import settings as dj

from records.models import Entry, EntryStatus, Segment
from sync import engine

WORK, TRAVEL, PAUSE = "WORK", "TRAVEL", "PAUSE"


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


def start(user, *, company=None, client=None, role=None, activity="", todo=None, mode=WORK, at=None):
    at = at or datetime.now().astimezone()
    if not concurrent_allowed(user):
        for other in Entry.objects.filter(owner=user, deleted=False, status=EntryStatus.ACTIVE):
            suspend(user, other, at)
    uid = new_uid()
    now = engine.now_ms()
    fields = {
        "status": EntryStatus.ACTIVE, "mode": mode, "zoneId": zone(), "activity": activity or None,
        "companyUid": str(company.uid) if company else None, "clientUid": str(client.uid) if client else None,
        "roleUid": str(role.uid) if role else None, "todoUid": str(todo.uid) if todo else None,
        "createdUtcMs": now, "updatedUtcMs": now,
    }
    put(user, "entry", uid, fields)
    entry = Entry.objects.get(uid=uid)
    _open(user, entry, mode, at)
    return entry


def record_past(user, *, start_at, end_at, company=None, client=None, role=None, activity="", todo=None):
    if end_at <= start_at:
        raise OpError("end before start")
    uid = new_uid()
    now = engine.now_ms()
    put(user, "entry", uid, {
        "status": EntryStatus.TO_COMPLETE, "mode": None, "zoneId": zone(), "activity": activity or None,
        "companyUid": str(company.uid) if company else None, "clientUid": str(client.uid) if client else None,
        "roleUid": str(role.uid) if role else None, "todoUid": str(todo.uid) if todo else None,
        "createdUtcMs": now, "updatedUtcMs": now,
    })
    entry = Entry.objects.get(uid=uid)
    put(user, "segment", new_uid(), {"entryUid": uid, "kind": WORK, "startUtcMs": ms(start_at), "endUtcMs": ms(end_at), "zoneId": entry.zone_id, "source": "MANUAL"})
    if complete_enough(entry):
        _touch(user, entry, {"status": EntryStatus.CLOSED})
    return entry


def switch(user, entry, mode, at=None):
    """Pause, travel or back to work: the open segment closes, one of [mode] opens."""
    at = at or datetime.now().astimezone()
    if entry.status not in (EntryStatus.ACTIVE, EntryStatus.SUSPENDED):
        raise OpError("not running")
    _close_open(user, entry, at)
    _open(user, entry, mode, at)
    _touch(user, entry, {"status": EntryStatus.ACTIVE, "mode": mode})


def suspend(user, entry, at=None):
    at = at or datetime.now().astimezone()
    _close_open(user, entry, at)
    _touch(user, entry, {"status": EntryStatus.SUSPENDED, "mode": None})


def stop(user, entry, at=None):
    at = at or datetime.now().astimezone()
    _close_open(user, entry, at)
    entry.refresh_from_db()
    status = EntryStatus.CLOSED if complete_enough(entry) else EntryStatus.TO_COMPLETE
    _touch(user, entry, {"status": status, "mode": None})


def complete(user, entry):
    entry.refresh_from_db()
    if not complete_enough(entry):
        raise OpError("details missing")
    _touch(user, entry, {"status": EntryStatus.CLOSED, "mode": None})


def reopen(user, entry):
    _touch(user, entry, {"status": EntryStatus.SUSPENDED, "mode": None})


def edit_entry(user, entry, fields):
    record = _touch(user, entry, fields)
    entry.refresh_from_db()
    if entry.status == EntryStatus.TO_COMPLETE and complete_enough(entry):
        _touch(user, entry, {"status": EntryStatus.CLOSED})
    return record


def overlapping(entry):
    """Two of the entry's segments overlap (an open one runs to infinity)."""
    segs = list(Segment.objects.filter(entry=entry, deleted=False).order_by("start_utc"))
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


# ---- to-dos ----

def save_todo(user, todo_uid, fields):
    now = engine.now_ms()
    fields = dict(fields, updatedUtcMs=now)
    if todo_uid is None:
        todo_uid = new_uid()
        fields.setdefault("createdUtcMs", now)
        fields.setdefault("refs", "")
        fields.setdefault("done", False)
    put(user, "todo", todo_uid, fields)
    return todo_uid


def todo_state(user, todo, state):
    now = engine.now_ms()
    if state == "done":
        f = {"done": True, "doneUtcMs": now, "ignoredUtcMs": None}
    elif state == "ignored":
        f = {"done": False, "doneUtcMs": None, "ignoredUtcMs": now}
    else:
        f = {"done": False, "doneUtcMs": None, "ignoredUtcMs": None}
    save_todo(user, todo.uid, f)
