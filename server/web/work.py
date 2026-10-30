# SPDX-License-Identifier: GPL-3.0-or-later
"""Home, activities and to-dos in the browser (docs/ROADMAP.md phase 4)."""

from datetime import date, datetime, timedelta

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.utils.translation import gettext as _
from django.views.decorators.http import require_POST

from accounts.models import Profile
from records.models import Attachment, Client, Company, Entry, EntryStatus, Expense, Role, Segment

from . import data, ops


def _parse_dt(text):
    """A datetime-local input in the server's zone."""
    if not text:
        return None
    try:
        return timezone.make_aware(datetime.fromisoformat(text), timezone.get_current_timezone())
    except ValueError:
        raise ops.OpError(_("data e ora non valide")) from None


def _pick(user, model, uid):
    if not uid:
        return None
    return data.masters(user, model).filter(uid=uid).first()


def _entry(request, uid, write=False):
    entry = get_object_or_404(data.readable_entries(request.user), uid=uid)
    if write and not data.writable(request.user, entry):
        raise PermissionDenied
    return entry


def _rows(entries):
    """Entries with their segments, totals and expenses for a list."""
    segs = data.segments_of(entries)
    exps = data.expenses_of(entries)
    out = []
    for e in entries:
        s = segs.get(e.uid, [])
        d = data.durations(s)
        out.append({
            "e": e,
            "start": s[0].start_utc if s else None,
            "end": None if not s or any(x.end_utc is None for x in s) else max(x.end_utc for x in s),
            "work": data.hm(d["WORK"]), "travel": data.hm(d["TRAVEL"]) if d["TRAVEL"] else "", "pause": data.hm(d["PAUSE"]) if d["PAUSE"] else "",
            "minutes": d,
            "expenses": sum(x.amount_cents for x in exps.get(e.uid, [])),
        })
    return out


def _todo_state(t, today):
    if t.done:
        return "done"
    if t.ignored_utc:
        return "ignored"
    if t.due_date and t.due_date < today:
        return "overdue"
    if t.plan_date:
        return "planned"
    return "open"


@login_required
def home(request):
    user = request.user
    if user.profile == Profile.READER:
        return redirect("report")
    today = timezone.localdate()
    start, end = data.day_bounds(today)
    mine = Entry.objects.filter(org=user.org, owner=user, deleted=False).select_related("company", "client", "role")
    running = _rows(list(mine.filter(status__in=[EntryStatus.ACTIVE, EntryStatus.SUSPENDED]).order_by("-created_utc")))
    missing = _rows(list(mine.filter(status=EntryStatus.TO_COMPLETE).order_by("-created_utc")[:20]))
    closed_today = [r for r in _rows(list(mine.filter(status=EntryStatus.CLOSED, updated_utc__gte=start - timedelta(days=1)).order_by("-updated_utc")[:30])) if r["end"] and r["end"] >= start]
    open_todos = [t for t in data.todos(user).filter(done=False, ignored_utc__isnull=True)]
    working = {r["e"].todo_id for r in running}
    overdue = [t for t in open_todos if _todo_state(t, today) == "overdue" and t.uid not in working]
    today_todos = [t for t in open_todos if t not in overdue and t.uid not in working and (t.plan_date == today or t.due_date == today)]
    return render(request, "web/home.html", {
        "running": running, "missing": missing, "closed_today": closed_today,
        "overdue": overdue, "today_todos": today_todos, "first": not user.devices.exists() if user.syncs else False,
    })


@login_required
def entry_new(request):
    user = request.user
    if not user.syncs:
        raise PermissionDenied
    fixed = data.owner_times_fixed_for(user)
    todo = data.todos(user).filter(uid=request.GET.get("todo") or request.POST.get("todo") or None).first() if (request.GET.get("todo") or request.POST.get("todo")) else None
    if request.method == "POST":
        p = request.POST
        try:
            client = _pick(user, Client, p.get("client"))
            company = client.company if client and client.company_id else _pick(user, Company, p.get("company"))
            kwargs = {"company": company, "client": client, "role": _pick(user, Role, p.get("role")), "activity": p.get("activity", "").strip(), "todo": todo}
            if fixed and p.get("when") in ("started", "past"):
                raise ops.OpError(_("la policy non consente di scegliere gli orari"))
            if p.get("when") == "past":
                e = ops.record_past(user, start_at=_parse_dt(p.get("start")), end_at=_parse_dt(p.get("end")), **kwargs)
            else:
                e = ops.start(user, at=_parse_dt(p.get("start")) if p.get("when") == "started" else None, **kwargs)
            return redirect("entry", uid=e.uid)
        except (ops.OpError, TypeError) as e:
            messages.error(request, str(e))
    initial = {"client": str(todo.client_id or "") if todo else "", "company": str(todo.company_id or "") if todo else "", "activity": todo.title if todo else ""}
    return render(request, "web/entry_new.html", {"companies": data.companies(user), "clients": data.clients(user), "roles": data.roles(user), "todo": todo, "initial": initial, "fixed": fixed})


@login_required
def entries(request):
    user = request.user
    q = data.readable_entries(user)
    g = request.GET
    today = timezone.localdate()
    try:
        since = date.fromisoformat(g.get("from")) if g.get("from") else today - timedelta(days=30)
        until = date.fromisoformat(g.get("to")) if g.get("to") else today
    except ValueError:
        since, until = today - timedelta(days=30), today
    q = q.filter(created_utc__gte=data.day_bounds(since)[0] - timedelta(days=1), created_utc__lt=data.day_bounds(until)[1])
    if g.get("company"):
        q = q.filter(company__uid=g["company"])
    if g.get("status"):
        q = q.filter(status=g["status"])
    if g.get("person"):
        q = q.filter(owner_id=g["person"])
    rows = _rows(list(q.order_by("-created_utc")[:500]))
    rows = [r for r in rows if not r["start"] or (data.day_bounds(since)[0] <= r["start"] < data.day_bounds(until)[1])]
    days = {}
    for r in rows:
        day = timezone.localtime(r["start"]).date() if r["start"] else timezone.localtime(r["e"].created_utc).date()
        days.setdefault(day, []).append(r)
    return render(request, "web/entries.html", {
        "days": sorted(days.items(), reverse=True), "since": since, "until": until, "f": g,
        "companies": data.companies(user) if user.profile != Profile.READER else user.companies.all(),
        "people": data.people(user) if user.profile in (Profile.ADMIN, Profile.MANAGER) or user.is_admin else None,
        "statuses": EntryStatus.choices,
    })


@login_required
def entry(request, uid):
    user = request.user
    e = _entry(request, uid)
    can = data.writable(user, e)
    fix = data.can_fix_times(user, e)
    if request.method == "POST":
        p = request.POST
        action = p.get("action")
        if action in ("segment", "add_segment") and fix:
            # an admin or a manager writes the owner's records as the owner
            user = e.owner
        elif not (can and (action != "segment" and (action != "add_segment" or p.get("kind") in (ops.TRAVEL, ops.PAUSE)))):
            # with the times locked the owner only adds forgotten travel and pauses, as on the phone
            raise PermissionDenied
        try:
            if action == "save":
                client = _pick(user, Client, p.get("client"))
                company = client.company if client and client.company_id else _pick(user, Company, p.get("company"))
                role = _pick(user, Role, p.get("role"))
                tags = sorted({t.strip().lower().lstrip("#") for t in p.get("tags", "").replace(",", " ").split() if t.strip()})
                ops.edit_entry(user, e, {
                    "companyUid": str(company.uid) if company else None, "clientUid": str(client.uid) if client else None,
                    "roleUid": str(role.uid) if role else None,
                    "activity": p.get("activity", "").strip() or None, "notes": p.get("notes", "").strip() or None,
                    "locality": p.get("locality", "").strip() or None, "tags": tags,
                })
                messages.success(request, _("Attività salvata."))
            elif action in ("pause", "travel", "work"):
                ops.switch(user, e, {"pause": ops.PAUSE, "travel": ops.TRAVEL, "work": ops.WORK}[action])
            elif action == "suspend":
                ops.suspend(user, e)
            elif action == "stop":
                ops.stop(user, e)
            elif action == "complete":
                ops.complete(user, e)
            elif action == "reopen":
                ops.reopen(user, e)
            elif action == "delete":
                ops.delete(user, "entry", e.uid)
                messages.success(request, _("Attività eliminata."))
                return redirect("entries")
            elif action == "segment":
                seg = get_object_or_404(Segment, entry=e, uid=p.get("segment"), deleted=False)
                if p.get("remove"):
                    ops.remove_segment(user, seg)
                else:
                    ops.edit_segment(user, seg, start_at=_parse_dt(p.get("start")), end_at=_parse_dt(p.get("end")), kind=p.get("kind", seg.kind))
            elif action == "add_segment":
                ops.add_segment(user, e, start_at=_parse_dt(p.get("start")), end_at=_parse_dt(p.get("end")), kind=p.get("kind", ops.WORK))
            elif action == "expense":
                cents = round(float(p.get("amount", "0").replace(",", ".")) * 100)
                ops.add_expense(user, e, cents=cents, description=p.get("description", ""))
            elif action == "remove_expense":
                x = get_object_or_404(Expense, entry=e, uid=p.get("expense"), deleted=False)
                ops.delete(user, "expense", x.uid)
            else:
                raise ops.OpError(_("azione sconosciuta"))
        except (ops.OpError, ValueError) as err:
            messages.error(request, str(err))
        return redirect("entry", uid=e.uid)
    e.refresh_from_db()
    segs = list(Segment.objects.filter(entry=e, deleted=False).order_by("start_utc"))
    return render(request, "web/entry.html", {
        "e": e, "can": can, "fix": fix, "segments": segs, "d": {k: data.hm(v) for k, v in data.durations(segs).items()},
        "expenses": Expense.objects.filter(entry=e, deleted=False).order_by("created_utc"),
        "attachments": Attachment.objects.filter(entry=e, deleted=False).order_by("created_utc"),
        "companies": data.companies(user), "clients": data.clients(user), "roles": data.roles(user),
        "complete": ops.complete_enough(e), "kinds": [ops.WORK, ops.TRAVEL, ops.PAUSE] if fix else [ops.TRAVEL, ops.PAUSE],
    })


@login_required
def attachment(request, uid):
    att = get_object_or_404(Attachment, uid=uid, deleted=False, has_file=True)
    _entry(request, att.entry_id)
    from sync.views import file_path, serve_file

    return serve_file(att, file_path(att))


@login_required
def todos(request):
    user = request.user
    if not user.syncs:
        raise PermissionDenied
    today = timezone.localdate()
    show = request.GET.get("show", "open")
    items = []
    for t in data.todos(user).order_by("due_date", "plan_date", "title"):
        state = _todo_state(t, today)
        if (show == "open" and state in ("done", "ignored")) or (show == "done" and state != "done") or (show == "ignored" and state != "ignored"):
            continue
        items.append((t, state))
    return render(request, "web/todos.html", {"items": items, "show": show})


@login_required
def todo_edit(request, uid=None):
    user = request.user
    if not user.syncs:
        raise PermissionDenied
    todo = get_object_or_404(data.todos(user), uid=uid) if uid else None
    if request.method == "POST":
        p = request.POST
        try:
            if p.get("state") and todo:
                ops.todo_state(user, todo, p["state"])
                return redirect("todos")
            title = p.get("title", "").strip()
            if not title:
                raise ops.OpError(_("il titolo è obbligatorio"))
            client = _pick(user, Client, p.get("client"))
            company = client.company if client and client.company_id else _pick(user, Company, p.get("company"))
            plan_time = p.get("plan_time") or None
            due_time = p.get("due_time") or None

            def minutes(t):
                hh, mm = t.split(":")
                return int(hh) * 60 + int(mm)
            fields = {
                "title": title, "notes": p.get("notes", "").strip() or None,
                "companyUid": str(company.uid) if company else None, "clientUid": str(client.uid) if client else None,
                "dueDate": p.get("due_date") or None, "dueTimeMin": minutes(due_time) if due_time else None,
                "planDate": p.get("plan_date") or None, "planStartMin": minutes(plan_time) if plan_time and p.get("plan_date") else None,
                "durationMin": int(p["duration"]) if p.get("duration") else None,
                "priority": int(p["priority"]) if p.get("priority") else None,
            }
            ops.save_todo(user, todo.uid if todo else None, fields)
            messages.success(request, _("Voce salvata."))
            return redirect("todos")
        except (ops.OpError, ValueError) as err:
            messages.error(request, str(err))

    def hhmm(m):
        return f"{m // 60:02d}:{m % 60:02d}" if m is not None else ""
    return render(request, "web/todo_edit.html", {
        "t": todo, "companies": data.companies(user), "clients": data.clients(user),
        "due_time": hhmm(todo.due_time_min) if todo else "", "plan_time": hhmm(todo.plan_start_min) if todo else "",
    })


@login_required
@require_POST
def quick(request, uid):
    """The commands of the Home cards."""
    e = _entry(request, uid, write=True)
    action = request.POST.get("action")
    try:
        if action in ("pause", "travel", "work"):
            ops.switch(request.user, e, {"pause": ops.PAUSE, "travel": ops.TRAVEL, "work": ops.WORK}[action])
        elif action == "suspend":
            ops.suspend(request.user, e)
        elif action == "stop":
            ops.stop(request.user, e)
            e.refresh_from_db()
            if e.status == EntryStatus.TO_COMPLETE:
                messages.info(request, _("Mancano dei dettagli: completa l'attività."))
                return redirect("entry", uid=e.uid)
    except ops.OpError as err:
        messages.error(request, str(err))
    return redirect("home")

