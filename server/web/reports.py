# SPDX-License-Identifier: GPL-3.0-or-later
"""
The reports: the summary (totals by company and client) and the detail (a row for each activity and day), the
app's report lines rounded with each owner's rules (web/reporting.py). Both are tables whose columns can be
sorted and chosen (web/tables.py), exported as shown, and printed; the CSV of the entries stays as it was.
"""

import csv
from datetime import date

from django.contrib.auth.decorators import login_required
from django.db.models import Q
from django.http import Http404, HttpResponse
from django.shortcuts import render
from django.urls import reverse
from django.utils import timezone
from django.utils.translation import gettext as _

from accounts.models import Profile

from records.models import ActivityKind, Attachment, Segment

from . import data, formats, reporting, states, tables
from .tables import Column, cell
from .templatetags import at

ABSENCE = ActivityKind.ABSENCE
SUMMARY, DETAIL = "summary", "detail"
# the rows of the detail: one for each activity and day (the report's lines), or one for each segment
BY_ACTIVITY, BY_SEGMENT = "activity", "segment"


def _period(request):
    today = timezone.localdate()
    try:
        since = date.fromisoformat(request.GET["from"]) if request.GET.get("from") else today.replace(day=1)
        until = date.fromisoformat(request.GET["to"]) if request.GET.get("to") else today
    except ValueError:
        since, until = today.replace(day=1), today
    return since, until


def _entries(request):
    """The activities and absences the user reads, with the company and person filters of the request."""
    q = data.readable_entries(request.user)
    # a filter that is not an id is left out, as a bad link
    company, person = data.parse_uid(request.GET.get("company")), data.parse_pk(request.GET.get("person"))
    if company:
        q = q.filter(Q(company__uid=company) | Q(kind=ABSENCE, company__isnull=True))
    if person is not None:
        q = q.filter(owner_id=person)
    return q


def _days(request, since, until):
    """The report's days of the period; an absence without a company counts for every company."""
    return reporting.report(_entries(request), since, until)


def _absences(days):
    """The absence lines, in order of day."""
    return [x for d in sorted(days) for x in days[d].absences]


def _lines(request, since, until, days=None):
    """The report lines of the period; an entry's expenses go with its first line in it."""
    days = days if days is not None else _days(request, since, until)
    lines = [x for d in sorted(days) for x in days[d].lines]
    exps = data.expenses_of([x.entry for x in lines])
    seen = set()
    for x in lines:
        x.expenses = 0 if x.entry.uid in seen else sum(e.amount_cents for e in exps.get(x.entry.uid, []))
        seen.add(x.entry.uid)
    return lines


def export_rules(user):
    """
    (formats, longest period in days) the policy gives the user's exports: the feature `export`, the formats of
    `export.formats` (all three when it names none) and the behaviour `exportMaxDays` (366).
    """
    from policies.effective import merged

    p = merged(user)
    wanted = p.get("export", {}).get("formats")
    formats_ = [f for f in tables.FORMATS if wanted is None or f in wanted]
    longest = p.get("behaviours", {}).get("exportMaxDays")
    on = p.get("features", {}).get("export", True) is not False
    return (formats_ if on else []), longest if isinstance(longest, int) and longest > 0 else 366


def _records(lines):
    """
    The report lines with what the tables show: km of the travel, attachments and expenses of the activity, which
    go with its first line in the period (an activity of several days counts them once).
    """
    entries = [x.entry for x in lines]
    km = {}
    for sg in Segment.objects.filter(entry__in=entries, deleted=False, planned=False, kind="TRAVEL"):
        here = sg.km_declared if sg.km_declared is not None else sg.km_estimated or 0
        km[sg.entry_id] = km.get(sg.entry_id, 0) + here
    attached = {}
    for a in Attachment.objects.filter(entry__in=entries, deleted=False):
        kinds = attached.setdefault(a.entry_id, {})
        kinds[a.kind] = kinds.get(a.kind, 0) + 1
    states.annotate({x.entry.uid: x.entry for x in lines}.values())
    seen, out = set(), []
    for x in lines:
        e = x.entry
        first = e.uid not in seen
        seen.add(e.uid)
        kinds = attached.get(e.uid, {}) if first else {}
        out.append({
            "x": x, "e": e, "day": x.day, "start": x.start, "end": None if x.running else x.end,
            "work": x.r_work, "travel": x.r_travel, "pause": x.r_pause, "reported": x.r_work + x.r_travel,
            "real_work": x.work // 60, "real_travel": x.travel // 60, "real_pause": x.pause // 60,
            "km": round(km.get(e.uid, 0), 1) if first and e.uid in km else None, "expenses": x.expenses,
            "attachments": sum(kinds.values()), "kinds": kinds, "absence": None,
        })
    return out


def _absence_records(days):
    """The absences of the period as rows of the detail, apart from work, travel and pause."""
    out = []
    for x in _absences(days):
        e = x.entry
        out.append({
            "x": x, "e": e, "day": x.day, "start": x.start, "end": x.end, "work": None, "travel": None, "pause": None,
            "reported": None, "real_work": None, "real_travel": None, "real_pause": None, "km": None, "expenses": 0,
            "attachments": 0, "kinds": {}, "absence": x.seconds // 60, "absent": True,
        })
    return out


# --- the columns -------------------------------------------------------------------------------------------

def _name(user):
    return (user.get_full_name() or user.username) if user else ""


def _cf(value):
    return value.casefold() if value else None


def _local(t):
    return timezone.localtime(t).strftime("%H:%M") if t else ""


def _hm(minutes):
    return reporting.hm(minutes) if minutes is not None else ""


def _duration(key, label, real=None, default=True):
    """A column of minutes, rounded as reported with the real ones under; summed in the footer."""
    def show(r):
        if r[key] is None:
            return cell()
        return cell(_hm(r[key]), _hm(r[real]) if real and r[real] is not None and r[real] != r[key] else "")

    def total(rows):
        mine = [r for r in rows if r[key] is not None]
        if not mine:
            return cell()
        whole, kept = sum(r[key] for r in mine), sum(r[real] for r in mine) if real else None
        return cell(_hm(whole), _hm(kept) if kept is not None and kept != whole else "")

    fields = ((key, label, "min"),) + (((f"{key}_real", _("%(l)s registrato") % {"l": label}, "min"),) if real else ())
    plain = lambda r: [r[key]] + ([r[real]] if real else [])  # noqa: E731
    return Column(key, label, show, lambda r: r[key], plain, fields, True, default, total)


def _text_column(key, label, get, default=True, href=None):
    def show(r):
        return cell(get(r) or "", href=href(r) if href and get(r) else "")
    return Column(key, label, show, lambda r: _cf(get(r)), lambda r: [get(r) or ""], ((key, label, ""),),
                  default=default)


def _detail_columns(ctx):
    link = lambda r: reverse("todo_edit" if r.get("absent") else "entry", args=[r["e"].uid])  # noqa: E731
    colors = ctx["colors"]
    when = lambda r: (r["day"], r["start"].timestamp() if r["start"] else 0)  # noqa: E731

    def when_cell(r):
        return cell(formats.date(r["day"]), href=link(r))

    def span_cell(r):
        span = formats.time(timezone.localtime(r["start"])) if r["start"] else ""
        if r["end"]:
            span += "–" + formats.time(timezone.localtime(r["end"]))
        return cell(span)

    def state(r):
        e = r["e"]
        return cell(states.label(e.shown, e), html=at.state_pill(e), title=str(states.label(e.shown, e)))

    def attachments(r):
        titles = ", ".join(f"{n} {k.lower()}" for k, n in sorted(r["kinds"].items()))
        return cell(str(r["attachments"]) if r["attachments"] else "", title=titles)

    def km_cell(r):
        return cell(f"{r['km']:.1f}" if r["km"] is not None else "")

    def money(r):
        return cell(at.eur(r["expenses"]) if r["expenses"] else "")

    def sum_total(key, fmt):
        return lambda rows: cell(fmt(sum(r[key] or 0 for r in rows))) if any(r[key] for r in rows) else None

    return [
        Column("date", _("Data"), when_cell, when, lambda r: [r["day"].isoformat()], (("date", _("Data"), ""),)),
        Column("time", _("Orario"), span_cell, lambda r: r["start"].timestamp() if r["start"] else None,
               lambda r: [_local(r["start"]), _local(r["end"])], (("start", _("Inizio"), ""), ("end", _("Fine"), ""))),
        _text_column("person", _("Persona"), lambda r: _name(r["e"].owner), ctx["people"]),
        _text_column("company", _("Azienda"), lambda r: r["e"].company.name if r["e"].company else ""),
        _text_column("client", _("Cliente"), lambda r: r["e"].client.name if r["e"].client else ""),
        _text_column("role", _("Ruolo"), lambda r: r["e"].role.name if r["e"].role else "", False),
        _text_column("activity", _("Attività"), lambda r: r["e"].activity or (
            str(states.label(r["e"].shown, r["e"])) if r.get("absent") else ""), href=link),
        _text_column("site", _("Località"), lambda r: r["e"].locality),
        Column("tags", _("Tag"), lambda r: cell(html=at.tag_chips(r["e"].tags, colors) if r["e"].tags else None),
               lambda r: _cf(" ".join(r["e"].tags or [])), lambda r: [" ".join(r["e"].tags or [])],
               (("tags", _("Tag"), ""),)),
        _duration("work", _("Lavoro"), "real_work"),
        _duration("travel", _("Viaggio"), "real_travel"),
        _duration("pause", _("Pause"), "real_pause", False),
        _duration("reported", _("Rendicontato"), None, False),
        Column("km", _("Km"), km_cell, lambda r: r["km"], lambda r: [r["km"]], (("km", _("Km"), "km"),), True, True,
               lambda rows: cell(f"{sum(r['km'] or 0 for r in rows):.1f}") if any(r["km"] for r in rows) else None),
        Column("expenses", _("Spese"), money, lambda r: r["expenses"] or None,
               lambda r: [f"{r['expenses'] / 100:.2f}" if r["expenses"] else ""],
               (("expenses", _("Spese"), "EUR"),), True, False,
               sum_total("expenses", at.eur)),
        Column("attachments", _("Allegati"), attachments, lambda r: r["attachments"] or None,
               lambda r: [r["attachments"]],
               (("attachments", _("Allegati"), ""),), True, True, sum_total("attachments", str)),
        Column("state", _("Stato"), state, lambda r: _cf(str(states.label(r["e"].shown, r["e"]))),
               lambda r: [str(states.label(r["e"].shown, r["e"]))], (("state", _("Stato"), ""),)),
        _duration("absence", _("Assenza"), None, ctx["absences"]),
    ]


def _segment_records(request, since, until):
    """
    The real segments (work, travel, pause, absence) that start in the period, one row each, on the local day
    they start: no carry-over to the day before and no rounding, which are rules of an activity's day (the
    other mode and the summary); the durations are the recorded minutes, a running segment up to now.
    """
    lo, hi = data.day_bounds(since)[0], data.day_bounds(until)[1]
    segs = Segment.objects.filter(
        entry__in=_entries(request), deleted=False, planned=False, start_utc__gte=lo, start_utc__lt=hi)
    segs = segs.select_related("entry__company", "entry__client", "entry__role", "entry__owner")
    segs = list(segs.order_by("start_utc"))
    # one object for each activity, annotated with its state once
    entries = {}
    for s in segs:
        s.entry = entries.setdefault(s.entry_id, s.entry)
    states.annotate(entries.values())
    now, out = timezone.now(), []
    for s in segs:
        km = s.km_declared if s.km_declared is not None else s.km_estimated
        out.append({
            "s": s, "e": s.entry, "day": timezone.localtime(s.start_utc).date(), "start": s.start_utc, "end": s.end_utc,
            "kind": s.kind, "minutes": int(((s.end_utc or now) - s.start_utc).total_seconds() // 60),
            "km": round(km, 1) if s.kind == "TRAVEL" and km is not None else None, "absent": s.kind == "ABSENCE",
            "note": s.note or "",
        })
    return out


def _segment_columns(ctx):
    """The columns of the detail by segment: the segment's own, then what its activity says."""
    shared = {c.key: c for c in _detail_columns(ctx)}
    kind = lambda r: str(at.MODE.get(r["kind"], r["kind"]))  # noqa: E731
    duration = _duration("minutes", _("Durata"))
    duration.key, duration.fields = "duration", (("duration", _("Durata"), "min"),)
    for key in ("role",):
        shared[key].default = False
    return [
        shared["date"], shared["time"],
        Column("kind", _("Tratto"), lambda r: cell(kind(r)), lambda r: _cf(kind(r)), lambda r: [r["kind"]],
               (("kind", _("Tratto"), ""),)),
        duration,
        Column("km", _("Km"), lambda r: cell(f"{r['km']:.1f}" if r["km"] is not None else ""), lambda r: r["km"],
               lambda r: [r["km"]], (("km", _("Km"), "km"),), True, True,
               lambda rows: cell(f"{sum(r['km'] or 0 for r in rows):.1f}") if any(r["km"] for r in rows) else None),
        shared["person"], shared["company"], shared["client"], shared["role"], shared["activity"], shared["site"],
        shared["tags"], shared["state"],
        _text_column("note", _("Nota del tratto"), lambda r: r["note"], False),
    ]


def rows_mode(request):
    """activity or segment: the request's ?rows=, else the last one chosen, else by activity."""
    asked, kept = request.GET.get("rows"), tables.prefs(request.user)
    if asked in (BY_ACTIVITY, BY_SEGMENT):
        tables.store(request.user, {**kept, "rows": asked})
        return asked
    return kept["rows"] if kept.get("rows") in (BY_ACTIVITY, BY_SEGMENT) else BY_ACTIVITY


def _summary_columns(ctx):
    return [
        Column("company", _("Azienda"), lambda r: cell(r["company"]), lambda r: _cf(r["company"]),
               lambda r: [r["company"]], (("company", _("Azienda"), ""),)),
        Column("client", _("Cliente"), lambda r: cell(r["client"]), lambda r: _cf(r["client"]),
               lambda r: [r["client"]], (("client", _("Cliente"), ""),)),
        Column("count", _("Attività"), lambda r: cell(str(r["count"])), lambda r: r["count"], lambda r: [r["count"]],
               (("count", _("Attività"), ""),), True, True,
               lambda rows: cell(str(sum(r["count"] for r in rows))) if rows else None),
        _duration("work", _("Lavoro"), "real_work"), _duration("travel", _("Viaggio"), "real_travel"),
        _duration("pause", _("Pause"), "real_pause"), _duration("reported", _("Rendicontato"), None, False),
        Column("km", _("Km"), lambda r: cell(f"{r['km']:.1f}" if r["km"] else ""), lambda r: r["km"] or None,
               lambda r: [r["km"] or None], (("km", _("Km"), "km"),), True, False,
               lambda rows: cell(f"{sum(r['km'] for r in rows):.1f}") if any(r["km"] for r in rows) else None),
        Column("expenses", _("Spese"), lambda r: cell(at.eur(r["expenses"])), lambda r: r["expenses"],
               lambda r: [f"{r['expenses'] / 100:.2f}"], (("expenses", _("Spese"), "EUR"),), True, True,
               lambda rows: cell(at.eur(sum(r["expenses"] for r in rows))) if rows else None),
    ]


SUMS = ("work", "travel", "pause", "real_work", "real_travel", "real_pause", "reported", "km", "expenses")


def _summary_rows(records):
    totals = {}
    for r in records:
        e = r["e"]
        key = (e.company.name if e.company else "—", e.client.name if e.client else "—")
        t = totals.setdefault(key, {n: 0 for n in SUMS} | {"uids": set()})
        for n in SUMS:
            t[n] += r[n] or 0
        t["uids"].add(e.uid)
    return [{"company": k[0], "client": k[1], "count": len(v["uids"]), **{n: v[n] for n in v if n != "uids"}}
            for k, v in sorted(totals.items())]


def _ctx(request, people, absences=False):
    return {"people": bool(people), "colors": data.tag_colors(request.user), "absences": absences}


@login_required
def report(request):
    """Riepilogativo: the totals by company and client, and the absences."""
    return _report(request, SUMMARY)


@login_required
def detail(request):
    """Dettaglio attività: a row for each activity and day, and the absences."""
    return _report(request, DETAIL)


def _tables(request, mode):
    """(table, since, until, days, people) of a report with the filters of the request."""
    user = request.user
    rows = rows_mode(request) if mode == DETAIL else None
    if rows:
        # the links of the page (sort, exports, the form) carry the mode shown
        request.GET = request.GET.copy()
        request.GET["rows"] = rows
    since, until = _period(request)
    days = _days(request, since, until)
    records = _records(_lines(request, since, until, days))
    people = data.readable_people(user) if (user.is_admin or user.profile == Profile.MANAGER) else None
    if mode == SUMMARY:
        table = tables.build(request, mode, _summary_columns(_ctx(request, people)), _summary_rows(records))
    elif rows == BY_SEGMENT:
        table = tables.build(request, f"{DETAIL}-{BY_SEGMENT}", _segment_columns(_ctx(request, people)),
                             _segment_records(request, since, until))
    else:
        rows = records + _absence_records(days)
        rows.sort(key=lambda r: (r["day"], r["start"]))
        table = tables.build(request, mode, _detail_columns(_ctx(request, people, bool(_absences(days)))), rows)
    return table, since, until, days, people


def _report(request, mode):
    user = request.user
    table, since, until, days, people = _tables(request, mode)
    away = {}
    for x in _absences(days):
        t = away.setdefault(x.entry.absence_kind or "ABSENCE", {"days": set(), "seconds": 0})
        t["days"].add((x.entry.owner_id, x.day))
        t["seconds"] += x.seconds
    order = list(states.ABSENCE_LABELS)
    absences = [
        {"kind": states.ABSENCE_LABELS[k], "days": len(v["days"]),
         "time": reporting.hm_seconds(v["seconds"])}
        for k, v in sorted(away.items(), key=lambda kv: order.index(kv[0]))]
    reader = user.profile == Profile.READER
    formats_, longest = export_rules(user)
    return render(request, "web/report.html", {
        "mode": mode, "since": since, "until": until, "f": request.GET, "t": tables.page(request, table),
        "rows": request.GET.get("rows", ""),
        "columns": [{"key": c.key, "label": c.label, "on": c.key in table.chosen} for c in table.columns],
        "sorted": table.sort, "direction": table.direction,
        "absences": absences if mode == SUMMARY else [], "reader": reader,
        "companies": user.companies.all() if reader else data.companies(user), "people": people,
        "exports": formats_, "export_long": (until - since).days + 1 > longest, "export_max": longest,
        "title": _("Riepilogativo") if mode == SUMMARY else _("Dettaglio attività"),
        "page_title": _("Report") + " · " + (_("Riepilogativo") if mode == SUMMARY else _("Dettaglio attività")),
        "company": next((c for c in data.companies(user) if str(c.uid) == request.GET.get("company")), None),
        "person": next((p for p in (people or []) if str(p.pk) == request.GET.get("person")), None),
        "now": timezone.localtime(),
    })


def _refuse(user, fmt, since, until):
    """Why the policy does not let the user export in [fmt] over since..until, else None."""
    formats_, longest = export_rules(user)
    if fmt not in formats_:
        return _("l'esportazione in questo formato non è consentita dalla policy")
    if (until - since).days + 1 > longest:
        return _("il periodo supera i %(n)s giorni che la policy consente di esportare") % {"n": longest}
    return None


@login_required
def export(request, fmt, mode):
    """The table as shown (filters, sort, visible columns) in CSV, XML or JSON, within the policy's export rules."""
    if fmt not in tables.FORMATS or mode not in (SUMMARY, DETAIL):
        raise Http404
    since, until = _period(request)
    why = _refuse(request.user, fmt, since, until)
    if why:
        return HttpResponse(why, status=403, content_type="text/plain; charset=utf-8")
    table = _tables(request, mode)[0]
    meta = {"report": table.name, "from": since.isoformat(), "to": until.isoformat()}
    return tables.export(table, fmt, meta, _cell)


def _cell(text):
    """A free text for the CSV: one a spreadsheet would read as a formula starts with an apostrophe."""
    text = text or ""
    return "'" + text if text.startswith(("=", "+", "-", "@", "\t", "\r")) else text


@login_required
def report_csv(request):
    since, until = _period(request)
    why = _refuse(request.user, "csv", since, until)
    if why:
        return HttpResponse(why, status=403, content_type="text/plain; charset=utf-8")
    response = HttpResponse(content_type="text/csv; charset=utf-8")
    response["Content-Disposition"] = f'attachment; filename="activitytrack-{since}-{until}.csv"'
    w = csv.writer(response, delimiter=";")
    # one row per entry and day; *_min real, *_rounded_min as reported
    w.writerow([
        "date", "user", "company", "client", "role", "activity", "status", "start", "end", "work_min", "travel_min", "pause_min",
        "expenses_eur", "tags", "uid", "work_rounded_min", "travel_rounded_min", "pause_rounded_min",
        "absence_min",
    ])
    days = _days(request, since, until)
    for x in _lines(request, since, until, days):
        e = x.entry
        local = lambda t: timezone.localtime(t).strftime("%Y-%m-%d %H:%M") if t else ""  # noqa: E731
        w.writerow([
            x.day.isoformat(), _cell(e.owner.username if e.owner else ""),
            _cell(e.company.name if e.company else ""), _cell(e.client.name if e.client else ""), _cell(e.role.name if e.role else ""),
            _cell(e.activity), e.status, local(x.start), "" if x.running else local(x.end),
            x.work // 60, x.travel // 60, x.pause // 60, f"{x.expenses / 100:.2f}",
            _cell(" ".join(e.tags or [])), str(e.uid), x.r_work, x.r_travel, x.r_pause, 0,
        ])
    # absences: a row each and day, status ABSENCE, the kind as activity; only absence_min is filled
    for x in _absences(days):
        e = x.entry
        local = lambda t: timezone.localtime(t).strftime("%Y-%m-%d %H:%M")  # noqa: E731
        w.writerow([
            x.day.isoformat(), _cell(e.owner.username if e.owner else ""),
            _cell(e.company.name if e.company else ""), "", "",
            _cell(e.activity or str(states.ABSENCE_LABELS.get(e.absence_kind, ""))), "ABSENCE",
            local(x.start), local(x.end),
            0, 0, 0, "0.00", _cell(" ".join(e.tags or [])), str(e.uid), 0, 0, 0, x.seconds // 60,
        ])
    return response
