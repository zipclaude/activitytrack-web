# SPDX-License-Identifier: GPL-3.0-or-later
"""
The Home list as in the app (ui/home/HomeTabs.kt homeSections, manual §3):
to-dos (TODO activities) and stopped activities in one list with the
filters period, state, company and tag.
"""

from datetime import datetime, time, timedelta

from django.utils import timezone

from records import activity as A

# period: -1 today, 0 everything, else hours
WINDOWS = (-1, 24, 72, 168, 720, 0)
TODO, MISSING, DONE, IGNORED = "todo", "missing", "done", "ignored"
SHOWS = (TODO, MISSING, DONE, IGNORED)
DEFAULT_SHOW = (TODO, MISSING)
SECTIONS = ("overdue", "missing", "today", "next", "earlier", "no_date", "ignored")


def _at(day, minute, tz):
    return datetime.combine(day, time.min, tzinfo=tz) + timedelta(minutes=minute)


def plan_bounds(t, tz):
    """(start, end, all day) of a to-do's plan (TodoPlan.kt), or None."""
    if not t.plan_date:
        return None
    if t.plan_start_min is None:
        start = _at(t.plan_date, 0, tz)
        return start, start + timedelta(days=1), True
    start = _at(t.plan_date, t.plan_start_min, tz)
    return start, start + timedelta(minutes=t.duration_min or 0), False


def moment(t, tz):
    """The plan's start, else the due moment (todoMoment)."""
    plan = plan_bounds(t, tz)
    if plan:
        return plan[0]
    if t.due_date:
        return _at(t.due_date, min(max(t.due_time_min or 0, 0), 1440), tz)
    return None


def overdue(t, now, tz, segments=()):
    """Late (ACTIVITY-MODEL §2.3); [segments]: the activity's, for its planned ones."""
    if t.status != A.TODO or t.deleted_utc:
        return False
    return A.late(t, segments, now, tz)


def state(t, now, tz, segments=()):
    """done, ignored (in the bin), overdue, planned or open (todoState)."""
    if t.status == A.CLOSED:
        return "done"
    if t.deleted_utc:
        return "ignored"
    if overdue(t, now, tz, segments):
        return "overdue"
    return "planned" if t.plan_date else "open"


def today_from(carry_over_min, now, tz):
    """Today begins at the carry-over time (06:00), as in the reports; before it, yesterday's (closedBoundary)."""
    today = timezone.localtime(now, tz).date()
    start = _at(today, carry_over_min, tz)
    return start if start <= now else _at(today - timedelta(days=1), carry_over_min, tz)


def sections(todos, stopped, show, window, company=None, tag=None, carry_over_min=360, now=None, incomplete=frozenset()):
    """
    [(section, [item])], empty sections left out. todos: every TODO activity
    of the user; stopped: [(entry, end)] of the closed activities; incomplete:
    the uids of those not complete (§2.4, "details missing").
    An item is ("todo", activity, state) or ("entry", entry, end).
    """
    # TODO(activity-1.0-pages): the displayed states of ACTIVITY-MODEL §2.2 instead of the to-do's
    tz = timezone.get_current_timezone()
    now = now or timezone.now()
    today = timezone.localtime(now, tz).date()
    start_today = today_from(carry_over_min, now, tz)
    ahead = _at(today + timedelta(days=1), 0, tz) if window == -1 else None if window == 0 else now + timedelta(hours=window)
    behind = start_today if window == -1 else None if window == 0 else now - timedelta(hours=window)
    groups = {s: [] for s in SECTIONS}
    far = datetime.max.replace(tzinfo=tz)

    def ours(company_id):
        return company is None or company_id == company

    with_todos = not tag
    for t in todos:
        if not ours(t.company_id) or not with_todos:
            continue
        st = state(t, now, tz)
        if st in ("done", "ignored"):
            at = (t.closed_utc or t.updated_utc) if st == "done" else t.deleted_utc
            if st == "done" and DONE in show and (behind is None or at >= behind):
                key = ("1", -at.timestamp()) if at >= start_today else -at.timestamp()
                groups["today" if at >= start_today else "earlier"].append((key, ("todo", t, st)))
            elif st == "ignored" and IGNORED in show and (behind is None or at >= behind):
                groups["ignored"].append(((-at.timestamp(),), ("todo", t, st)))
            continue
        if TODO not in show:
            continue
        m = moment(t, tz)
        if st == "overdue":
            groups["overdue"].append(((m or far).timestamp(), ("todo", t, st)))
        elif m is None:
            # the most urgent first (5 emergency .. 1 low), then none
            groups["no_date"].append(((-(t.priority or 0), (t.activity or "").lower()), ("todo", t, st)))
        elif ahead is not None and m > ahead:
            continue
        elif timezone.localtime(m, tz).date() > today:
            groups["next"].append((m.timestamp(), ("todo", t, st)))
        else:
            groups["today"].append((("0", m.timestamp()), ("todo", t, st)))
    for e, end in stopped:
        if not ours(e.company_id) or (tag and tag not in (e.tags or [])):
            continue
        if e.uid in incomplete:
            if MISSING in show:
                groups["missing"].append((-end.timestamp(), ("entry", e, end)))
        elif DONE in show and (behind is None or end >= behind):
            # after today's to-dos, the most recent first
            key = ("1", -end.timestamp()) if end >= start_today else -end.timestamp()
            groups["today" if end >= start_today else "earlier"].append((key, ("entry", e, end)))
    out = []
    for s in SECTIONS:
        rows = groups[s]
        if rows:
            rows.sort(key=lambda r: r[0])
            out.append((s, [r[1] for r in rows]))
    return out
