# SPDX-License-Identifier: GPL-3.0-or-later
"""Totals by company and client over a period, and the CSV of the entries."""

import csv
from datetime import date, timedelta

from django.contrib.auth.decorators import login_required
from django.http import HttpResponse
from django.shortcuts import render
from django.utils import timezone

from accounts.models import Profile

from . import data
from .work import _rows


def _period(request):
    today = timezone.localdate()
    try:
        since = date.fromisoformat(request.GET["from"]) if request.GET.get("from") else today.replace(day=1)
        until = date.fromisoformat(request.GET["to"]) if request.GET.get("to") else today
    except ValueError:
        since, until = today.replace(day=1), today
    return since, until


def _entries(request, since, until):
    q = data.readable_entries(request.user).filter(
        created_utc__gte=data.day_bounds(since)[0] - timedelta(days=1), created_utc__lt=data.day_bounds(until)[1])
    if request.GET.get("company"):
        q = q.filter(company__uid=request.GET["company"])
    if request.GET.get("person"):
        q = q.filter(owner_id=request.GET["person"])
    rows = _rows(list(q.order_by("created_utc")))
    lo, hi = data.day_bounds(since)[0], data.day_bounds(until)[1]
    return [r for r in rows if r["start"] and lo <= r["start"] < hi]


@login_required
def report(request):
    user = request.user
    since, until = _period(request)
    rows = _entries(request, since, until)
    totals = {}
    for r in rows:
        e = r["e"]
        key = (e.company.name if e.company else "—", e.client.name if e.client else "—")
        t = totals.setdefault(key, {"work": 0, "travel": 0, "pause": 0, "count": 0, "expenses": 0})
        t["work"] += r["minutes"]["WORK"]
        t["travel"] += r["minutes"]["TRAVEL"]
        t["pause"] += r["minutes"]["PAUSE"]
        t["expenses"] += r["expenses"]
        t["count"] += 1
    table = [{"company": k[0], "client": k[1], **{n: data.hm(v[n]) for n in ("work", "travel", "pause")}, "count": v["count"], "expenses": v["expenses"]} for k, v in sorted(totals.items())]
    whole = {n: data.hm(sum(v[n] for v in totals.values())) for n in ("work", "travel", "pause")}
    reader = user.profile == Profile.READER
    return render(request, "web/report.html", {
        "since": since, "until": until, "f": request.GET, "table": table, "whole": whole, "rows": rows, "reader": reader,
        "companies": user.companies.all() if reader else data.companies(user),
        "people": data.people(user) if (user.is_admin or user.profile == Profile.MANAGER) else None,
        "expenses": sum(v["expenses"] for v in totals.values()),
    })


@login_required
def report_csv(request):
    since, until = _period(request)
    response = HttpResponse(content_type="text/csv; charset=utf-8")
    response["Content-Disposition"] = f'attachment; filename="activitytrack-{since}-{until}.csv"'
    w = csv.writer(response, delimiter=";")
    w.writerow(["date", "user", "company", "client", "role", "activity", "status", "start", "end", "work_min", "travel_min", "pause_min", "expenses_eur", "tags", "uid"])
    for r in _entries(request, since, until):
        e = r["e"]
        local = lambda t: timezone.localtime(t).strftime("%Y-%m-%d %H:%M") if t else ""  # noqa: E731
        w.writerow([
            timezone.localtime(r["start"]).date().isoformat(), e.owner.username if e.owner else "",
            e.company.name if e.company else "", e.client.name if e.client else "", e.role.name if e.role else "",
            e.activity or "", e.status, local(r["start"]), local(r["end"]),
            r["minutes"]["WORK"], r["minutes"]["TRAVEL"], r["minutes"]["PAUSE"], f"{r['expenses'] / 100:.2f}",
            " ".join(e.tags or []), str(e.uid),
        ])
    return response
