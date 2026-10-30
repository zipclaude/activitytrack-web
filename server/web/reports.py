# SPDX-License-Identifier: GPL-3.0-or-later
"""
Totals by company and client over a period, and the CSV of the entries: the
app's report lines (an entry on a day), rounded with each owner's rules
(web/reporting.py).
"""

import csv
from datetime import date

from django.contrib.auth.decorators import login_required
from django.http import HttpResponse
from django.shortcuts import render
from django.utils import timezone

from accounts.models import Profile

from . import data, reporting


def _period(request):
    today = timezone.localdate()
    try:
        since = date.fromisoformat(request.GET["from"]) if request.GET.get("from") else today.replace(day=1)
        until = date.fromisoformat(request.GET["to"]) if request.GET.get("to") else today
    except ValueError:
        since, until = today.replace(day=1), today
    return since, until


def _lines(request, since, until):
    """The report lines of the period; an entry's expenses go with its first line in it."""
    q = data.readable_entries(request.user)
    if request.GET.get("company"):
        q = q.filter(company__uid=request.GET["company"])
    if request.GET.get("person"):
        q = q.filter(owner_id=request.GET["person"])
    days = reporting.report(q, since, until)
    lines = [x for d in sorted(days) for x in days[d].lines]
    exps = data.expenses_of([x.entry for x in lines])
    seen = set()
    for x in lines:
        x.expenses = 0 if x.entry.uid in seen else sum(e.amount_cents for e in exps.get(x.entry.uid, []))
        seen.add(x.entry.uid)
    return lines


def _row(x):
    """A line for web/_entry_row.html: rounded times, the real ones beside."""
    hm = reporting.hm_seconds
    return {
        "e": x.entry, "day": x.day, "start": x.start, "end": None if x.running else x.end,
        "work": reporting.hm(x.r_work), "travel": reporting.hm(x.r_travel) if x.r_travel else "",
        "real": hm(x.work) + (f" · ✈ {hm(x.travel)}" if x.travel else ""),
    }


@login_required
def report(request):
    user = request.user
    since, until = _period(request)
    lines = _lines(request, since, until)
    totals = {}
    names = ("work", "travel", "pause", "r_work", "r_travel", "r_pause")
    for x in lines:
        e = x.entry
        key = (e.company.name if e.company else "—", e.client.name if e.client else "—")
        t = totals.setdefault(key, {**{n: 0 for n in names}, "entries": set(), "expenses": 0})
        for n in names:
            t[n] += getattr(x, n)
        t["entries"].add(e.uid)
        t["expenses"] += x.expenses

    def shown(v):
        hm = reporting.hm_seconds
        return {
            "work": reporting.hm(v["r_work"]), "travel": reporting.hm(v["r_travel"]), "pause": reporting.hm(v["r_pause"]),
            "real_work": hm(v["work"]), "real_travel": hm(v["travel"]), "real_pause": hm(v["pause"]),
        }

    table = [{"company": k[0], "client": k[1], "count": len(v["entries"]), "expenses": v["expenses"], **shown(v)} for k, v in sorted(totals.items())]
    whole = shown({n: sum(v[n] for v in totals.values()) for n in names})
    reader = user.profile == Profile.READER
    return render(request, "web/report.html", {
        "since": since, "until": until, "f": request.GET, "table": table, "whole": whole, "rows": [_row(x) for x in lines], "reader": reader,
        "companies": user.companies.all() if reader else data.companies(user),
        "people": data.readable_people(user) if (user.is_admin or user.profile == Profile.MANAGER) else None,
        "expenses": sum(v["expenses"] for v in totals.values()),
    })


@login_required
def report_csv(request):
    since, until = _period(request)
    response = HttpResponse(content_type="text/csv; charset=utf-8")
    response["Content-Disposition"] = f'attachment; filename="activitytrack-{since}-{until}.csv"'
    w = csv.writer(response, delimiter=";")
    # one row per entry and day; *_min real, *_rounded_min as reported
    w.writerow([
        "date", "user", "company", "client", "role", "activity", "status", "start", "end", "work_min", "travel_min", "pause_min",
        "expenses_eur", "tags", "uid", "work_rounded_min", "travel_rounded_min", "pause_rounded_min",
    ])
    for x in _lines(request, since, until):
        e = x.entry
        local = lambda t: timezone.localtime(t).strftime("%Y-%m-%d %H:%M") if t else ""  # noqa: E731
        w.writerow([
            x.day.isoformat(), e.owner.username if e.owner else "",
            e.company.name if e.company else "", e.client.name if e.client else "", e.role.name if e.role else "",
            e.activity or "", e.status, local(x.start), "" if x.running else local(x.end),
            x.work // 60, x.travel // 60, x.pause // 60, f"{x.expenses / 100:.2f}",
            " ".join(e.tags or []), str(e.uid), x.r_work, x.r_travel, x.r_pause,
        ])
    return response
