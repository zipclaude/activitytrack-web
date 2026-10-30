# SPDX-License-Identifier: GPL-3.0-or-later
"""
Agenda by day, week and month, as in the app (manual §7): the report's lines
(day attribution, rounding, a running entry up to now) and the open to-dos
planned or due in the period.
"""

from calendar import monthrange
from datetime import date, datetime, timedelta

from django.contrib.auth.decorators import login_required
from django.db.models import Q
from django.shortcuts import redirect, render
from django.urls import reverse
from django.utils import timezone

from accounts.models import Profile
from records.models import WorkSchedule

from . import data, reporting

VIEWS = ("day", "week", "month")
# companies without a colour: the app's neutral grey (Colors.kt NO_COLOR)
NO_COLOR = 0x6B7280
DAY_MIN = 24 * 60


def period(view, anchor):
    """DAY: the day; WEEK: Monday..Sunday; MONTH: whole weeks around the month."""
    if view == "day":
        return anchor, anchor
    if view == "week":
        monday = anchor - timedelta(days=anchor.weekday())
        return monday, monday + timedelta(days=6)
    first = anchor.replace(day=1)
    last = anchor.replace(day=monthrange(anchor.year, anchor.month)[1])
    return first - timedelta(days=first.weekday()), last + timedelta(days=6 - last.weekday())


def step(view, anchor, n):
    if view == "day":
        return anchor + timedelta(days=n)
    if view == "week":
        return anchor + timedelta(weeks=n)
    month = anchor.month - 1 + n
    year, month = anchor.year + month // 12, month % 12 + 1
    return date(year, month, min(anchor.day, monthrange(year, month)[1]))


def lanes(blocks):
    """Side by side when they overlap: each block gets a lane and the lanes of its cluster."""
    blocks.sort(key=lambda b: (b["top"], -b["height"]))
    cluster, ends, out = [], [], []

    def close():
        for b in cluster:
            b["lanes"] = len(ends)
        out.extend(cluster)

    for b in blocks:
        if cluster and b["top"] >= max(ends):
            close()
            cluster, ends = [], []
        lane = next((i for i, end in enumerate(ends) if end <= b["top"]), None)
        if lane is None:
            lane = len(ends)
            ends.append(0)
        ends[lane] = b["top"] + b["height"]
        b["lane"] = lane
        cluster.append(b)
    if cluster:
        close()
    return out


def _minute(t, day, tz):
    """Minutes of [t] from the start of [day], local."""
    start = datetime.combine(day, datetime.min.time(), tzinfo=tz)
    return int((t - start).total_seconds() // 60)


def _clock(m):
    return f"{m // 60 % 24:02d}:{m % 60:02d}"


def _plannable(user, person):
    """Taps on free slots plan a to-do: own agenda, the policy's planning and to-dos on."""
    if person != user or not user.syncs:
        return False
    from policies.effective import merged

    f = merged(user).get("features", {})
    return f.get("planning", True) is not False and f.get("todos", True) is not False


@login_required
def agenda(request):
    user = request.user
    if user.profile == Profile.READER:
        return redirect("report")
    g = request.GET
    tz = timezone.get_current_timezone()
    today = timezone.localdate()
    # the last view picked comes back (app issue #44)
    view = g.get("view") if g.get("view") in VIEWS else request.session.get("agenda_view", "week")
    request.session["agenda_view"] = view
    try:
        anchor = date.fromisoformat(g["date"]) if g.get("date") else today
    except ValueError:
        anchor = today
    people = data.people(user)
    person = people.filter(pk=g.get("person")).first() if g.get("person") else None
    person = person or user
    tag = g.get("tag") or ""
    since, until = period(view, anchor)

    entries = data.readable_entries(user).filter(owner=person)
    days = reporting.report(entries, since, until)
    colors = data.tag_colors(user)
    seen_tags = sorted({t for d in days.values() for x in d.lines for t in (x.entry.tags or [])})
    if tag:
        for d in days.values():
            d.lines = [x for x in d.lines if tag in (x.entry.tags or [])]

    # open to-dos: planned in the period or, without a plan, due in it; hidden by the tag filter
    todos = []
    if person == user and not tag:
        open_q = data.todos(user).select_related("company").filter(done=False, ignored_utc__isnull=True)
        todos = list(open_q.filter(Q(plan_date__range=(since, until)) | Q(plan_date__isnull=True, due_date__range=(since, until))))

    def link(**kw):
        q = {"view": view, "date": anchor.isoformat(), "person": person.pk if person != user else "", "tag": tag, **kw}
        return "?" + "&".join(f"{k}={v}" for k, v in q.items() if v not in ("", None))

    now = timezone.now()
    columns = []
    for i in range((until - since).days + 1):
        day = since + timedelta(days=i)
        d = days.get(day)
        blocks, band = [], []
        for x in d.lines if d else []:
            e = x.entry
            top = max(0, _minute(x.start, day, tz))
            end = min(DAY_MIN, max(_minute(x.end, day, tz), top + 1))
            blocks.append({
                "kind": "entry", "top": top, "height": max(end - top, 15), "url": reverse("entry", args=[e.uid]),
                "color": (e.company.color if e.company and e.company.color is not None else NO_COLOR),
                "title": (e.client.name if e.client else "") or (e.company.name if e.company else ""),
                "activity": e.activity or "", "span": f"{_clock(top)}–{_clock(_minute(x.end, day, tz))}",
                "reported": reporting.hm(x.reported), "running": x.running, "tags": e.tags or [],
            })
        for t in todos:
            color = t.company.color if t.company and t.company.color is not None else NO_COLOR
            url = reverse("todo_edit", args=[t.uid])
            if t.plan_date == day and t.plan_start_min is not None:
                length = t.duration_min or 0
                blocks.append({
                    "kind": "todo", "top": t.plan_start_min, "height": max(length, 30), "url": url, "color": color,
                    "title": t.title, "span": _clock(t.plan_start_min) + (f"–{_clock(t.plan_start_min + length)}" if length else ""),
                })
            elif t.plan_date == day or (t.plan_date is None and t.due_date == day):
                band.append({"title": t.title, "url": url, "color": color,
                             "due": _clock(t.due_time_min) if t.plan_date is None and t.due_time_min is not None else None})
        companies = []
        for x in d.lines if d else []:
            c = x.entry.company.color if x.entry.company and x.entry.company.color is not None else NO_COLOR
            if c not in companies:
                companies.append(c)
        columns.append({
            "day": day, "today": day == today, "blocks": lanes(blocks), "band": band,
            "reported": reporting.hm(d.reported) if d and d.reported else "",
            "in_month": day.month == anchor.month, "companies": companies[:4],
            "todos": bool(band) or any(b["kind"] == "todo" for b in blocks),
            "now": _minute(now, day, tz) if day == today else None, "link": link(view="day", date=day.isoformat()),
        })

    shown = [c for c in columns if view != "month" or c["in_month"]]
    total = sum(days[c["day"]].reported for c in shown if c["day"] in days)
    # the scale opens on the working hours (else 8-18), or on the running entry outside them
    weekdays = {c["day"].isoweekday() for c in columns}
    work = WorkSchedule.objects.filter(org=user.org, deleted=False, day_of_week__in=weekdays).filter(Q(owner=person) | Q(owner__isnull=True))
    work_from = min((w.start_min for w in work), default=8 * 60)
    work_to = max((w.end_min for w in work), default=18 * 60)
    running = [b["top"] for c in columns for b in c["blocks"] if b.get("running")]
    if running and not work_from <= running[0] < work_to:
        work_from, work_to = max(0, running[0] - 60), min(DAY_MIN, running[0] + 3 * 60)

    return render(request, "web/agenda.html", {
        "view": view, "anchor": anchor, "since": since, "until": until, "columns": columns,
        "weeks": [columns[i:i + 7] for i in range(0, len(columns), 7)] if view == "month" else None,
        "total": reporting.hm(total) if total else "", "tag": tag, "tags": seen_tags, "tag_colors": colors,
        "people": people if people.count() > 1 else None, "person": person,
        "prev": link(date=step(view, anchor, -1).isoformat()), "next": link(date=step(view, anchor, 1).isoformat()),
        "today_link": link(date=today.isoformat()),
        "views": [(v, link(view=v)) for v in VIEWS],
        "plannable": _plannable(user, person), "work_from": work_from, "work_to": work_to,
        "hours": range(24), "today": today,
        "add_at": (now.astimezone(tz) + timedelta(minutes=30 - now.astimezone(tz).minute % 30)).strftime("%H:%M") if anchor == today else "09:00",
    })
