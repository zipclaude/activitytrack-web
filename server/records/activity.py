# SPDX-License-Identifier: GPL-3.0-or-later
"""
The activity model 1.0 (the app's docs/ACTIVITY-MODEL.md): pure rules, no
database. The to-do is a state of the activity (an Entry), not an entity.

- display(): the displayed state and its "since" (§2.2-2.4), the same table
  as the app and the Python library (cases: tests/data/activity-states.json);
- absence_segments(): the segments of an absence (§3.1);
- priority_from_legacy(), absence_kind_of(): the conversions of §5 and §6.

Activities and segments are read by attribute, so model instances and plain
namespaces both work: an activity has kind, status, deleted_utc, closed_utc,
client_id, plan_date, plan_start_min, duration_min, due_date, due_time_min;
a segment has kind, start_utc, end_utc, planned (aware datetimes, dates).
"""

from datetime import datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

# Entry.kind
ACTIVITY, ABSENCE = "ACTIVITY", "ABSENCE"
# Entry.status
TODO, RUNNING, CLOSED = "TODO", "RUNNING", "CLOSED"
# Segment.kind (ABSENCE as above)
WORK, TRAVEL, PAUSE = "WORK", "TRAVEL", "PAUSE"
# Entry.absence_kind: "Assenza", "Ferie", "Permesso", "Malattia"
ABSENCE_KINDS = (ABSENCE, "HOLIDAY", "LEAVE", "SICK")
PRIORITY_MAX = 5

# displayed states (§2.2), in the order of the rules
DELETED, LATE, PLANNED, DONE, INCOMPLETE, SUSPENDED = "DELETED", "LATE", "PLANNED", "DONE", "INCOMPLETE", "SUSPENDED"
STATES = (DELETED, ABSENCE, LATE, PLANNED, TODO, WORK, TRAVEL, PAUSE, SUSPENDED, DONE, INCOMPLETE)


def _zone(zone):
    return ZoneInfo(zone) if isinstance(zone, str) else zone


def at_minute(day, minute, zone):
    """[day] at [minute] of the wall clock in [zone] (DST-safe: never start of day + minutes)."""
    local = datetime.combine(day, time.min) + timedelta(minutes=minute)
    return local.replace(tzinfo=_zone(zone))


def plus_minutes(dt, minutes):
    """[dt] + [minutes] of elapsed time (aware datetimes add wall-clock time: wrong across a DST change)."""
    return (dt.astimezone(timezone.utc) + timedelta(minutes=minutes)).astimezone(dt.tzinfo)


def plan_bounds(a, zone):
    """(start, end, whole day) of the plan (TodoPlan.kt), or None; a point in time ends where it starts."""
    if not a.plan_date:
        return None
    if a.plan_start_min is None:
        start = at_minute(a.plan_date, 0, zone)
        return start, at_minute(a.plan_date + timedelta(days=1), 0, zone), True
    start = at_minute(a.plan_date, a.plan_start_min, zone)
    return start, plus_minutes(start, a.duration_min or 0), False


def due_limit(a, zone):
    """The due time, or the end of the due day without a time; None without a due date."""
    if not a.due_date:
        return None
    if a.due_time_min is not None:
        return at_minute(a.due_date, min(max(a.due_time_min, 0), 1440), zone)
    return at_minute(a.due_date + timedelta(days=1), 0, zone)


def _planned(segments):
    return [s for s in segments if s.planned]


def _real(segments):
    return [s for s in segments if not s.planned]


def late(a, segments, now, zone):
    """§2.3: a TODO activity past its due date, its last planned segment or (without planned segments) its plan."""
    limit = due_limit(a, zone)
    if limit is not None and now > limit:
        return True
    planned = _planned(segments)
    if planned:
        ends = [s.end_utc for s in planned if s.end_utc is not None]
        return bool(ends) and len(ends) == len(planned) and now > max(ends)
    plan = plan_bounds(a, zone)
    if plan:
        start, end, whole_day = plan
        return now >= end if whole_day else now > end
    return False


def complete(a, segments, missing_required=False):
    """§2.4: no real segment at all (a ticked task), or a client, some work and every required field."""
    real = _real(segments)
    if not real:
        return True
    return a.client_id is not None and any(s.kind == WORK for s in real) and not missing_required


def display(a, segments, now, zone, missing_required=False):
    """(state, since) of §2.2: the first rule that matches wins; since may be None."""
    real = _real(segments)
    if a.deleted_utc is not None:
        return DELETED, a.deleted_utc
    if a.kind == ABSENCE:
        starts = [s.start_utc for s in real if s.kind == ABSENCE]
        return ABSENCE, min(starts) if starts else None
    if a.status == TODO:
        plan = plan_bounds(a, zone)
        planned = sorted(_planned(segments), key=lambda s: s.start_utc)
        since = plan[0] if plan else planned[0].start_utc if planned else None
        if late(a, segments, now, zone):
            return LATE, since
        if plan or a.due_date or planned:
            return PLANNED, since
        return TODO, since
    open_ = [s for s in real if s.end_utc is None]
    if a.status == RUNNING and open_:
        seg = max(open_, key=lambda s: s.start_utc)
        return (seg.kind if seg.kind in (WORK, TRAVEL, PAUSE) else WORK), seg.start_utc
    ends = [s.end_utc for s in real if s.end_utc is not None]
    since = max(ends) if ends else a.closed_utc
    if a.status == RUNNING:
        return SUSPENDED, since
    return (DONE if complete(a, segments, missing_required) else INCOMPLETE), since


def absence_segments(first, last, zone, start_min=None, duration_min=None, schedule=(), lunch=(), days_off=(),
                     with_times_every_day=False):
    """
    §3.1: [(day, n, start, end)] of an absence from [first] to [last] (dates,
    inclusive); n numbers the segments of a day from 0.
    - With a start: one segment a day from [start_min] lasting [duration_min]
      (elapsed time, as TodoPlan.end; none: a point in time). A new absence
      skips the days without working hours and the days off;
      [with_times_every_day] (the migration's permessi, one day long) does not.
    - Without: one a working day covering that weekday's schedule, its windows
      merged where they overlap or touch, cut by the lunch breaks; days without
      a schedule and days off are skipped.
    schedule, lunch: (day_of_week 1 = Monday, start_min, end_min); days_off: dates.
    """
    out = []
    off = set(days_off)
    day = first
    while day <= last:
        windows = working_windows(day.isoweekday(), schedule, lunch) if day not in off else []
        if start_min is not None:
            if windows or with_times_every_day:
                start = at_minute(day, start_min, zone)
                out.append((day, 0, start, plus_minutes(start, duration_min or 0)))
        else:
            for n, (a, b) in enumerate(windows):
                out.append((day, n, at_minute(day, a, zone), at_minute(day, b, zone)))
        day += timedelta(days=1)
    return out


def working_windows(weekday, schedule, lunch):
    """The working hours of a weekday as (start_min, end_min): windows merged, lunch breaks cut out."""
    merged = []
    for _, a, b in sorted(x for x in schedule if x[0] == weekday and x[2] > x[1]):
        if merged and a <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], b))
        else:
            merged.append((a, b))
    breaks = sorted((s, e) for d, s, e in lunch if d == weekday and e > s)
    out = []
    for a, b in merged:
        cur = a
        for la, lb in breaks:
            if lb <= cur or la >= b:
                continue
            if la > cur:
                out.append((cur, la))
            cur = max(cur, lb)
        if cur < b:
            out.append((cur, b))
    return out


def priority_from_legacy(p):
    """§5: P1 (the highest) .. P5 -> 5 .. 1; empty -> 0; above 5 -> 1; below 1 -> 0."""
    if p is None or p < 1:
        return 0
    if p > 5:
        return 1
    return 6 - p


# the four labels in the app's languages (it, en, de, es), compared case-insensitively;
# the same list as the app's migration reference (scripts/activity_migration.py)
ABSENCE_LABELS = {
    ABSENCE: ("Assenza", "Absence", "Abwesenheit", "Ausencia"),
    "HOLIDAY": ("Ferie", "Holiday", "Urlaub", "Vacaciones"),
    "LEAVE": ("Permesso", "Leave", "Freistellung", "Permiso"),
    "SICK": ("Malattia", "Sick leave", "Krankheit", "Enfermedad"),
}
_BY_LABEL = {label.casefold(): kind for kind, labels in ABSENCE_LABELS.items() for label in labels}


def absence_kind_of(title):
    """§6 step 2: the kind a title names when it is one of the labels, else None."""
    return _BY_LABEL.get((title or "").strip().casefold())

