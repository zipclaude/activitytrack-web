# SPDX-License-Identifier: GPL-3.0-or-later
"""
The migration to the activity model 1.0 (the app's docs/ACTIVITY-MODEL.md §6)
as a pure function over plain rows: the server's data migration
(records/migrations/0008_activity_data.py) feeds it the database and writes
back what it returns; the tests run it against the cases shared with the app
(records/tests/data/activity-migration.json, from the app's reference
scripts/activity_migration.py, whose CHOICEs this follows except where noted
below).

The data migration imports this module: change it only in ways that leave the
result on old data the same.

Rows are dicts. Instants are epoch milliseconds, local dates "yyyy-mm-dd",
uids strings; foreign keys hold the uid (or the owner's key) of the target.

Input (every list optional):
- entries: uid, owner, company, client, notes, status, mode, todo, activity,
  plugin_fields, created_utc_ms, updated_utc_ms, deleted (a tombstone)
- segments: uid, entry, kind, start_utc_ms, end_utc_ms, deleted
- external_refs: uid, entry, kind, value, deleted
- todos: uid, owner, kind (TASK, ABSENCE), title, notes, refs, company, client,
  external_key, due_date, due_time_min, priority, done, done_utc_ms,
  created_utc_ms, updated_utc_ms, plan_date, plan_start_min, duration_min,
  ignored_utc_ms, plugin_fields, plan_end_date, deleted
- absences (the legacy table): uid, owner, date, kind, time_min, deleted
- work_schedules, lunch_breaks: owner, day_of_week, start_min, end_min, deleted
- days_off: owner, date, deleted
Rows of central master data have owner None and apply to everyone.

Output of migrate():
- entries: {uid: fields} of every entry, old ones with the fields of the new
  model and those a to-do brought, new ones (from to-dos and absences) whole,
  with "new": True;
- segments, external_refs: the new rows, whole (uid, entry, ...).

Where the server differs from the reference, on purpose:
- an ignored absence to-do goes to the bin (deleted_utc_ms = ignored_utc_ms),
  as an ignored task does (the reference keeps it visible);
- tombstones (rows deleted by a sync) are skipped: a to-do tombstone becomes
  nothing, a deleted segment or reference counts for nothing;
- external_key is unique with the company for each owner (a database
  constraint here): on a clash the earliest created activity keeps the key.
"""

import re
import uuid
from datetime import date, datetime, time
from zoneinfo import ZoneInfo

from . import activity as A

NS = uuid.UUID("6f1c2a52-6b1e-5a8e-9d7e-0a5c1e7d1000")

RUNNING_OLD = ("ACTIVE", "SUSPENDED", "RUNNING")


def uid_for(base, suffix):
    """§6: uuid5(NS, "<to-do uid>/<suffix>"), the same on the phone and on the server."""
    return str(uuid.uuid5(NS, f"{base}/{suffix}"))


def classify_ref(value):
    """ExternalRefClassifier.kt: URL, UUID, TICKET or OTHER."""
    v = A.trim(value)
    if re.fullmatch(r"[a-zA-Z][a-zA-Z0-9+.-]*://\S+", v):
        return "URL"
    if re.fullmatch(r"\{?[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\}?", v):
        return "UUID"
    if re.fullmatch(r"#?([A-Za-z][A-Za-z0-9_]*[-_ ]?)?[0-9]+", v):
        return "TICKET"
    return "OTHER"


def _ms(dt):
    return int(dt.timestamp() * 1000)


def _day(text):
    return date.fromisoformat(text) if text else None


def _live(rows):
    return [r for r in rows or () if not r.get("deleted")]


def _blank(s):
    return s is None or not A.trim(str(s))


def _merge(todo_fields, entry_fields):
    """The to-do's plugin fields overridden by the entry's; None when both are empty."""
    def obj(x):
        return x if isinstance(x, dict) else {}
    merged = {**obj(todo_fields), **obj(entry_fields)}
    return merged or None


def _ref_values(t):
    """The to-do's references: one a non-blank line, trimmed, duplicates dropped. Lines end at LF, CRLF
    or CR only, as the app's lines() (str.splitlines() also breaks on \\v, \\f, U+0085, U+2028 and others)."""
    out = []
    for line in re.split(r"\r\n|\r|\n", t.get("refs") or ""):
        v = A.trim(line)
        if v and v not in out:
            out.append(v)
    return out


def migrate(data, zone):
    """§6 over [data] (see the module's doc); [zone]: the zone of what is made from to-dos and absences."""
    tz = ZoneInfo(zone)
    entries = {e["uid"]: e for e in data.get("entries") or ()}
    segs_by_entry = {}
    for s in _live(data.get("segments")):
        segs_by_entry.setdefault(s["entry"], []).append(s)
    refs_by_entry = {}
    for r in _live(data.get("external_refs")):
        refs_by_entry.setdefault(r["entry"], []).append(r)
    out_entries, out_segments, out_refs = {}, [], []

    def add_ref(entry_uid, ref_uid, kind, value):
        """One reference unless the entry has the same value already."""
        value = A.trim(value)
        if any(A.trim(r["value"]) == value for r in refs_by_entry.get(entry_uid, [])):
            return
        row = {"uid": ref_uid, "entry": entry_uid, "kind": kind, "value": value}
        refs_by_entry.setdefault(entry_uid, []).append(row)
        out_refs.append(row)

    # 1. entries: the new statuses; mode follows the open segment (the invariant of §1.1), the latest
    # started, then the greatest key (the app's id, here the uid)
    for e in entries.values():
        own = segs_by_entry.get(e["uid"], [])
        opened = sorted((s for s in own if s.get("end_utc_ms") is None), key=lambda s: (s["start_utc_ms"], s["uid"]))
        f = {"kind": A.ACTIVITY, "priority": 0, "mode": opened[-1]["kind"] if opened else None}
        if e["status"] in RUNNING_OLD:
            f["status"] = A.RUNNING
        else:
            ends = [s["end_utc_ms"] for s in own if s.get("end_utc_ms") is not None]
            f.update(status=A.CLOSED, closed_utc_ms=max(ends) if ends else e["updated_utc_ms"])
        out_entries[e["uid"]] = f

    # the working hours and days off of each owner (with the central ones)
    def of_owner(rows, owner):
        return [r for r in _live(rows) if r.get("owner") in (owner, None)]

    def hours(owner):
        return ([(w["day_of_week"], w["start_min"], w["end_min"]) for w in of_owner(data.get("work_schedules"), owner)],
                [(w["day_of_week"], w["start_min"], w["end_min"]) for w in of_owner(data.get("lunch_breaks"), owner)],
                {_day(d["date"]) for d in of_owner(data.get("days_off"), owner)})

    def new_activity(t, **fields):
        base = {
            "new": True, "owner": t.get("owner"), "kind": A.ACTIVITY, "status": A.TODO, "mode": None,
            "company": t.get("company"), "client": t.get("client"),
            "activity": None if _blank(t.get("title")) else t["title"],
            "notes": t.get("notes"), "zone_id": zone, "plugin_fields": None, "tags": [],
            "created_utc_ms": t["created_utc_ms"], "updated_utc_ms": t["updated_utc_ms"],
            "closed_utc_ms": None, "deleted_utc_ms": None,
            "external_key": None, "due_date": None, "due_time_min": None, "plan_date": None, "plan_start_min": None,
            "duration_min": None, "priority": 0, "absence_kind": None,
        }
        base.update(fields)
        return base

    def todo_fields(t):
        return {
            # a blank key is no key: the uniqueness pass skips it, the constraint of 0009 would not
            "external_key": None if _blank(t.get("external_key")) else t["external_key"], "due_date": t.get("due_date"), "due_time_min": t.get("due_time_min"),
            "plan_date": t.get("plan_date"), "plan_start_min": t.get("plan_start_min"),
            "duration_min": t.get("duration_min"), "priority": A.priority_from_legacy(t.get("priority")),
        }

    # 2. absences: an ABSENCE activity with the to-do's uid and one segment a working day (a permesso: its day)
    def absence(t, **fields):
        first = _day(t.get("plan_date"))
        spans = []
        if first:
            start = t.get("plan_start_min")
            last = max(_day(t.get("plan_end_date")) or first, first) if start is None else first
            schedule, lunch, off = hours(t.get("owner"))
            spans = A.absence_segments(first, last, tz, start, t.get("duration_min"), schedule, lunch, off,
                                       with_times_every_day=True)
        for day, n, a, b in spans:
            out_segments.append({
                "uid": uid_for(t["uid"], f"abs/{day.isoformat()}/{n}"), "entry": t["uid"], "kind": A.ABSENCE,
                "start_utc_ms": _ms(a), "end_utc_ms": _ms(b), "zone_id": zone, "source": "MANUAL", "planned": False,
            })
        ends = [_ms(b) for *_, b in spans]
        out_entries[t["uid"]] = new_activity(
            t, kind=A.ABSENCE, status=A.CLOSED, absence_kind=A.absence_kind_of(t.get("title")) or A.ABSENCE,
            closed_utc_ms=max(ends) if ends else t["updated_utc_ms"], deleted_utc_ms=t.get("ignored_utc_ms"), **fields,
        )

    todos = _live(data.get("todos"))
    live_todo = {t["uid"] for t in todos}
    linked = {}
    for e in entries.values():
        if not e.get("deleted") and e.get("todo") in live_todo:
            linked.setdefault(e["todo"], []).append(e)

    def first_start(e):
        starts = [s["start_utc_ms"] for s in segs_by_entry.get(e["uid"], [])]
        return (0, min(starts)) if starts else (1, 0)

    for t in todos:
        if t.get("kind") == "ABSENCE":
            # entries linked to an absence stay as they are
            absence(t)
            continue
        # 3. tasks, by the entries linked to them
        mine = sorted(linked.get(t["uid"], []), key=lambda e: (first_start(e), e["created_utc_ms"], e["uid"]))
        done = bool(t.get("done"))
        if not mine or not (done or any(e["status"] in RUNNING_OLD for e in mine)):
            # none linked: the to-do itself; all closed and the to-do open: it stays its own TODO.
            # done_utc_ms = 0 is a time: only a missing one falls back to the last update
            done_at = t["updated_utc_ms"] if t.get("done_utc_ms") is None else t["done_utc_ms"]
            out_entries[t["uid"]] = new_activity(
                t, status=A.CLOSED if done else A.TODO, closed_utc_ms=done_at if done else None,
                deleted_utc_ms=t.get("ignored_utc_ms"), plugin_fields=_merge(t.get("plugin_fields"), None),
                **todo_fields(t),
            )
            for i, v in enumerate(_ref_values(t)):
                add_ref(t["uid"], uid_for(t["uid"], f"ref/{i}"), classify_ref(v), v)
            for e in mine:
                add_ref(e["uid"], uid_for(t["uid"], f"link/{e['uid']}"), "UUID", t["uid"])
            continue
        # the earliest entry absorbs the to-do; the entry's own values win
        head = mine[0]
        f = out_entries[head["uid"]]
        f.update(todo_fields(t))
        f["plugin_fields"] = _merge(t.get("plugin_fields"), head.get("plugin_fields"))
        if _blank(head.get("activity")):
            # a blank title is no description, as in new_activity()
            f["activity"] = None if _blank(t.get("title")) else t["title"]
        if _blank(head.get("notes")):
            f["notes"] = t.get("notes")
        for name in ("company", "client"):
            if head.get(name) is None:
                f[name] = t.get(name)
        for i, v in enumerate(_ref_values(t)):
            add_ref(head["uid"], uid_for(t["uid"], f"ref/{i}"), classify_ref(v), v)
        for e in mine[1:]:
            add_ref(e["uid"], uid_for(t["uid"], f"link/{e['uid']}"), "UUID", head["uid"])

    # 4. legacy absences follow 2: the day-start prompt's record as the 20 -> 21 app migration read it
    for a in _live(data.get("absences")):
        day = _day(a["date"])
        schedule, _, _ = hours(a.get("owner"))
        rows = [(s, e) for d, s, e in schedule if d == day.isoweekday()]
        lo, hi = min((s for s, _ in rows), default=0), max((e for _, e in rows), default=1440)
        t_min = a.get("time_min")
        if a["kind"] == "FULL_DAY":
            start, length = None, None
        elif a["kind"] == "LATE_ENTRY" and t_min is not None and t_min > lo:
            start, length = lo, t_min - lo
        elif a["kind"] == "EARLY_EXIT" and t_min is not None and t_min < hi:
            start, length = t_min, hi - t_min
        else:
            # outside the working hours, or unknown: nothing was missed
            continue
        # no timestamps on the row: the start of its day
        stamp = _ms(datetime.combine(day, time.min).replace(tzinfo=tz))
        absence({"uid": a["uid"], "owner": a.get("owner"), "title": None, "created_utc_ms": stamp,
                 "updated_utc_ms": stamp, "plan_date": a["date"], "plan_start_min": start, "duration_min": length})
        if not any(s["entry"] == a["uid"] for s in out_segments):
            # on a day off: the activity stays, without segment nor closing time
            out_entries[a["uid"]]["closed_utc_ms"] = None

    _unique_keys(entries, out_entries)
    return {"entries": out_entries, "segments": out_segments, "external_refs": out_refs}


def _unique_keys(entries, out):
    """external_key is unique with the company for each owner: on a clash the earliest created keeps it."""
    rows = []
    for uid, f in out.items():
        old = entries.get(uid, {})
        if _blank(f.get("external_key")):
            if f.get("external_key") is not None:
                f["external_key"] = None
            continue
        if old.get("deleted"):
            continue
        row = f if f.get("new") else {**old, **f}
        rows.append((row["created_utc_ms"], uid, (row.get("owner"), row.get("company"), f["external_key"])))
    seen = set()
    for _, uid, key in sorted(rows):
        if key in seen:
            out[uid]["external_key"] = None
        seen.add(key)
