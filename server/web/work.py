# SPDX-License-Identifier: GPL-3.0-or-later
"""Home, activities and to-dos in the browser (docs/ROADMAP.md phase 4)."""

import uuid
from datetime import date, datetime, timedelta

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied
from django.db.models import Exists, OuterRef, Q
from django.http import JsonResponse, QueryDict
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.urls import reverse
from django.utils.http import url_has_allowed_host_and_scheme
from django.utils.translation import gettext as _
from django.views.decorators.http import require_POST

from accounts.models import Profile
from records import activity as A
from records.models import ActivityKind, Attachment, Client, Company, Entry, EntryStatus, Expense, Role, Segment, tag_name

from . import data, homelist, ops, reporting, states

# the longest absence: a year (the app: TodoRepository.MAX_ABSENCE_DAYS)
MAX_ABSENCE_DAYS = 366


def _parse_dt(text):
    """A datetime-local input in the server's zone."""
    if not text:
        return None
    try:
        return timezone.make_aware(datetime.fromisoformat(text), timezone.get_current_timezone())
    except ValueError:
        raise ops.OpError(_("data e ora non valide")) from None


def _pick(user, model, uid):
    uid = data.parse_uid(uid)
    if uid is None:
        return None
    return data.masters(user, model).filter(uid=uid).first()


def _entry(request, uid, write=False):
    entry = get_object_or_404(data.readable_entries(request.user), uid=uid)
    if write and not data.writable(request.user, entry):
        raise PermissionDenied
    return entry


def _rows(entries):
    """Entries with segments, totals and expenses for a list; each gets its state (.shown)."""
    states.annotate(entries)
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


def _is_incomplete(e):
    """A closed activity that is not complete (ACTIVITY-MODEL §2.4), the "details missing"."""
    states.annotate([e])
    return e.shown == A.INCOMPLETE


@login_required
def home(request):
    """Running activities, then one list of to-dos and stopped entries with the app's filters (manual §3)."""
    user = request.user
    if user.profile == Profile.READER:
        return redirect("report")
    g = request.GET
    now = timezone.now()
    from policies.effective import merged

    todos_on = merged(user).get("features", {}).get("todos", True) is not False
    offered = [s for s in homelist.SHOWS if todos_on or s != homelist.TODO]
    # the choices are remembered (app: homeWindow, homeShow); "f" marks a submitted form
    saved = request.session.get("home_filters", {})
    if g.get("f"):
        try:
            window = int(g.get("period", -1))
        except ValueError:
            window = -1
        f = {"period": window if window in homelist.WINDOWS else -1, "show": [x for x in g.getlist("show") if x in homelist.SHOWS],
             "company": g.get("company", ""), "tag": g.get("tag", "")}
        request.session["home_filters"] = f
    else:
        f = {"period": -1, "show": list(homelist.DEFAULT_SHOW), "company": "", "tag": "", **saved}
    show = [x for x in f["show"] if x in offered]
    company = _pick(user, Company, f["company"])
    company = company if company and not company.archived else None

    mine = Entry.objects.filter(org=user.org, owner=user, deleted=False, deleted_utc__isnull=True, kind=ActivityKind.ACTIVITY)
    mine = mine.select_related("company", "client", "role")
    running = _rows(list(mine.filter(status=EntryStatus.RUNNING).order_by("-created_utc")))
    for r in running:
        previous = ops.previous_kind(r["e"])
        r["actions"] = states.buttons(states.allowed(r["e"].shown, previous=previous))
    window = f["period"]
    stopped_q = mine.filter(status=EntryStatus.CLOSED)
    if window != 0:
        since = now - timedelta(hours=window if window > 0 else 36)
        # the incomplete ones whatever their age (§2.4: some work, and no client or no work segment)
        real = Segment.objects.filter(entry=OuterRef("pk"), deleted=False, planned=False)
        incomplete_q = Exists(real) & (Q(client__isnull=True) | ~Exists(real.filter(kind=ops.WORK)))
        stopped_q = stopped_q.filter(incomplete_q | Q(updated_utc__gte=since))
    rows = {r["e"].uid: r for r in _rows(list(stopped_q.order_by("-updated_utc")[:300]))}
    stopped = [(r["e"], r["end"] or r["e"].closed_utc or r["e"].updated_utc) for r in rows.values()]
    todos = list(data.todos(user).filter(deleted_utc__isnull=True)) if todos_on else []
    states.annotate(todos, now)
    carry = reporting.rules_for(user).carry_over_min
    tag = f["tag"] if (homelist.MISSING in show or homelist.DONE in show) else ""
    sections = homelist.sections(todos, stopped, show, window, company=company.uid if company else None, tag=tag or None,
                                 carry_over_min=carry, now=now)
    tags = sorted({t for e, _ in stopped for t in (e.tags or [])})
    listed = [(name, [(kind, obj, extra, rows.get(obj.uid) if kind == "entry" else None) for kind, obj, extra in items]) for name, items in sections]

    def link(**kw):
        q = QueryDict(mutable=True)
        q.update({"f": "1", "period": f["period"], "company": f["company"], "tag": f["tag"]})
        for x in kw.pop("show", show):
            q.appendlist("show", x)
        q.update(kw)
        return "?" + q.urlencode()

    return render(request, "web/home.html", {
        "running": running, "sections": listed, "first": not user.devices.exists() if user.syncs else False,
        "f": f, "show": show, "offered": offered, "windows": homelist.WINDOWS, "companies": data.companies(user), "company": company,
        "tags": tags, "tag": tag, "todos_on": todos_on, "today": timezone.localdate(),
        "show_changed": set(show) != set(homelist.DEFAULT_SHOW) & set(offered),
        "only_todo": link(show=[homelist.TODO]), "only_done": link(show=[homelist.DONE]), "show_all": link(show=offered),
    })


@login_required
def entry_new(request):
    user = request.user
    if not user.syncs:
        raise PermissionDenied
    fixed = data.owner_times_fixed_for(user)
    todo_uid = data.parse_uid(request.GET.get("todo") or request.POST.get("todo"))
    todo = data.todos(user).filter(uid=todo_uid).first() if todo_uid else None
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
    initial = {"client": str(todo.client_id or "") if todo else "", "company": str(todo.company_id or "") if todo else "", "activity": todo.activity or "" if todo else ""}
    return render(request, "web/entry_new.html", {"companies": data.companies(user), "clients": data.clients(user), "roles": data.roles(user), "todo": todo, "initial": initial, "fixed": fixed})


@login_required
@require_POST
def travel_now(request):
    """The Home's Viaggio button: an anonymous activity travelling from now (§3 creation)."""
    if not request.user.syncs:
        raise PermissionDenied
    try:
        e = ops.start(request.user, mode=ops.TRAVEL)
    except ops.OpError as err:
        messages.error(request, str(err))
        return redirect("home")
    return redirect("entry", uid=e.uid)


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
    # a filter that is not an id is left out, as a bad link
    if data.parse_uid(g.get("company")):
        q = q.filter(company__uid=data.parse_uid(g["company"]))
    if data.parse_pk(g.get("person")) is not None:
        q = q.filter(owner_id=data.parse_pk(g["person"]))
    rows = _rows(list(q.order_by("-created_utc")[:500]))
    # the state filter is the displayed state (ACTIVITY-MODEL §2.2), not the stored status
    if g.get("status") in states.UI:
        rows = [r for r in rows if r["e"].shown == g["status"]]
    rows = [r for r in rows if not r["start"] or (data.day_bounds(since)[0] <= r["start"] < data.day_bounds(until)[1])]
    days = {}
    for r in rows:
        day = timezone.localtime(r["start"]).date() if r["start"] else timezone.localtime(r["e"].created_utc).date()
        days.setdefault(day, []).append(r)
    return render(request, "web/entries.html", {
        "days": sorted(days.items(), reverse=True), "since": since, "until": until, "f": g,
        "companies": data.companies(user) if user.profile != Profile.READER else user.companies.all(),
        "people": data.readable_people(user) if user.profile in (Profile.ADMIN, Profile.MANAGER) or user.is_admin else None,
        "statuses": [(k, v[1]) for k, v in states.UI.items() if k != A.DELETED],
    })


def _tags_free(owner):
    """Free tags besides the master data's: unless the owner's policy locks them off (issue #133)."""
    from policies.effective import fixed_setting

    return fixed_setting(owner, "tagsFreeText") is not False


def _tags(entry, text):
    """The tags typed in the sheet, normalised as the app does; only known ones (or ones already there) when free tags are off."""
    tags = []
    for word in text.replace(",", " ").split():
        t = tag_name(word)
        if t and t not in tags:
            tags.append(t)
    if not _tags_free(entry.owner):
        known = {t.name for t in data.tags(entry.owner)} | set(entry.tags or [])
        unknown = [t for t in tags if t not in known]
        if unknown:
            raise ops.OpError(_("solo i tag dell'anagrafica: %(t)s non c'è") % {"t": ", ".join(unknown)})
    return sorted(tags)


@login_required
def busy(request):
    """
    The minutes of a day taken by the segments of whose times are being
    chosen, for the hatched cells of the time grid (pickers.js, issue #38):
    the user's own, or the entry's owner's on its sheet for who fixes its
    times; one segment, the one being changed, left out.
    """
    try:
        day = date.fromisoformat(request.GET.get("day", ""))
        entry_uid = uuid.UUID(request.GET["entry"]) if request.GET.get("entry") else None
        segment_uid = uuid.UUID(request.GET["segment"]) if request.GET.get("segment") else None
    except ValueError:
        return JsonResponse({"busy": []})
    owner = request.user
    if entry_uid:
        e = _entry(request, entry_uid)
        if e.owner_id != request.user.pk and not data.can_fix_times(request.user, e):
            # someone else's day only to who may change that entry's times: a
            # reader, or a manager for a company, sees just the entry itself
            return JsonResponse({"busy": []})
        owner = e.owner
    tz = timezone.get_current_timezone()
    start = timezone.make_aware(datetime.combine(day, datetime.min.time()), tz)
    end = start + timedelta(days=1)
    q = Segment.objects.filter(owner=owner, deleted=False, planned=False, entry__deleted=False, entry__deleted_utc__isnull=True, start_utc__lt=end).filter(Q(end_utc__isnull=True) | Q(end_utc__gt=start))
    if segment_uid:
        q = q.exclude(uid=segment_uid)
    now = timezone.now()
    out = []
    for s in q:
        a, b = max(s.start_utc, start), min(s.end_utc or now, end)
        if b > a:
            out.append([int((a - start).total_seconds() // 60), int(-(-(b - start).total_seconds() // 60))])
    return JsonResponse({"busy": sorted(out)})


# the commands the to-do page offers; its older buttons (done, ignored, open) still post their state
TODO_COMMANDS = (
    "start_work", "start_travel", "tick", "untick", "delete", "reopen_work", "reopen_travel")
LEGACY_STATES = {"done": "tick", "ignored": "delete", "open": "untick"}
# the commands of ACTIVITY-MODEL §3 the pages post; "travel" and "stop" may first ask (3 and 3.2)
SWITCH = {"pause": ops.PAUSE, "travel_no": ops.TRAVEL, "work": ops.WORK, "travel": ops.TRAVEL}
# the answers of the question pages and the older names, as the command they stand for
ASKED = {"travel_yes": "travel", "travel_no": "travel", "finish": "stop", "stop_return": "stop", "stop_travel": "stop",
         "reopen": "reopen_work"}
# what _command runs
COMMANDS_RUN = set(SWITCH) | set(ASKED) | {
    "resume", "start_work", "start_travel", "stop", "reopen_work", "reopen_travel", "tick", "untick", "delete"}


def _command(request, e, action, actor=None):
    """
    Runs a command on [e] as its owner and returns where to go next, or None
    for an unknown one. Starting travel from work and stopping an activity
    with a return trip to offer first go to the question page (?ask=).
    """
    user = actor or request.user
    here = reverse("entry", args=[e.uid])
    # only what the state allows (§3), as the buttons: a stale page or a forged post runs nothing else
    if action in COMMANDS_RUN:
        states.annotate([e])
        if ASKED.get(action, action) not in states.allowed(e.shown, bool(ops.real_segments(e)), ops.previous_kind(e)):
            raise ops.OpError(_("azione non consentita in questo stato"))
    if action == "travel" and e.status == EntryStatus.RUNNING and e.mode == ops.WORK:
        return here + "?ask=travel"
    if action in SWITCH:
        ops.switch(user, e, SWITCH[action])
    elif action == "resume":
        ops.switch(user, e, ops.previous_kind(e) or ops.WORK)
    elif action == "travel_yes":
        return reverse("entry", args=[ops.travel_from_work(user, e, True).uid])
    elif action in ("start_work", "start_travel"):
        mode = ops.WORK if action == "start_work" else ops.TRAVEL
        ops.start(user, todo=e, mode=mode, company=e.company, client=e.client, role=e.role,
                  activity=e.activity or "")
    elif action == "stop" and ops.outbound(e) is not None and e.status == EntryStatus.RUNNING:
        return here + "?ask=stop"
    elif action in ("stop", "finish", "stop_return", "stop_travel"):
        if action == "stop_travel":
            ops.switch(user, e, ops.TRAVEL)
            return here
        if action == "stop_return":
            if data.owner_times_fixed(e):
                raise ops.OpError(_("la policy non consente di scegliere gli orari"))
            ops.stop_with_return(user, e)
        else:
            ops.stop(user, e)
        e.refresh_from_db()
        if _is_incomplete(e):
            messages.info(request, _("Mancano dei dettagli: completa l'attività."))
        return here
    elif action in ("reopen_work", "reopen_travel", "reopen"):
        ops.reopen(user, e, ops.TRAVEL if action == "reopen_travel" else ops.WORK)
    elif action == "tick":
        ops.tick(user, e)
    elif action == "untick":
        ops.untick(user, e)
    elif action == "delete":
        ops.to_bin(user, e)
        messages.success(request, _("Attività eliminata: è nel cestino."))
        return reverse("entries")
    else:
        return None
    return here


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
                tags = _tags(e, p.get("tags", ""))
                ops.edit_entry(user, e, {
                    "companyUid": str(company.uid) if company else None, "clientUid": str(client.uid) if client else None,
                    "roleUid": str(role.uid) if role else None,
                    "activity": p.get("activity", "").strip() or None, "notes": p.get("notes", "").strip() or None,
                    "locality": p.get("locality", "").strip() or None, "tags": tags,
                })
                messages.success(request, _("Attività salvata."))
            elif action not in ("segment", "add_segment", "expense", "remove_expense"):
                to = _command(request, e, action)
                if to is None:
                    raise ops.OpError(_("azione sconosciuta"))
                return redirect(to)
            elif action == "segment":
                seg = get_object_or_404(Segment, entry=e, uid=data.parse_uid(p.get("segment")), deleted=False)
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
                x = get_object_or_404(Expense, entry=e, uid=data.parse_uid(p.get("expense")), deleted=False)
                ops.delete(user, "expense", x.uid)
            else:
                raise ops.OpError(_("azione sconosciuta"))
        except (ops.OpError, ValueError) as err:
            messages.error(request, str(err))
        return redirect("entry", uid=e.uid)
    e.refresh_from_db()
    if e.kind == ActivityKind.ABSENCE or e.status == EntryStatus.TODO:
        return redirect("todo_edit", uid=e.uid)
    segs = list(Segment.objects.filter(entry=e, deleted=False).order_by("start_utc"))
    states.annotate([e])
    ask = request.GET.get("ask") if can and e.status == EntryStatus.RUNNING else None
    return render(request, "web/entry.html", {
        "ask": ask if ask in ("travel", "stop") else None,
        "return_trip": ask == "stop" and not data.owner_times_fixed(e),
        "actions": states.buttons(
            states.allowed(e.shown, bool(ops.real_segments(e)), ops.previous_kind(e))),
        "form_url": "",
        "e": e, "can": can, "fix": fix, "segments": segs, "tags_free": _tags_free(e.owner), "tag_names": [t.name for t in data.tags(e.owner)], "d": {k: data.hm(v) for k, v in data.durations(segs).items()},
        "expenses": Expense.objects.filter(entry=e, deleted=False).order_by("created_utc"),
        "attachments": Attachment.objects.filter(entry=e, deleted=False).order_by("created_utc"),
        "companies": data.companies(user), "clients": data.clients(user), "roles": data.roles(user),
        "complete": A.complete(e, segs), "kinds": [ops.WORK, ops.TRAVEL, ops.PAUSE] if fix else [ops.TRAVEL, ops.PAUSE],
    })


@login_required
def bin_page(request):
    """
    The bin (ACTIVITY-MODEL §3 delete): deleted activities, restorable; the
    only place where one is deleted for good. Only an owner changes theirs.
    """
    user = request.user
    if user.profile == Profile.READER:
        raise PermissionDenied
    if request.method == "POST":
        uid = data.parse_uid(request.POST.get("uid"))
        e = get_object_or_404(data.readable_entries(user, binned=True), uid=uid)
        if not data.writable(user, e):
            raise PermissionDenied
        try:
            if request.POST.get("action") == "restore":
                ops.restore(user, e)
                messages.success(request, _("Attività ripristinata."))
            elif request.POST.get("action") == "purge":
                ops.purge(user, e)
                messages.success(request, _("Attività eliminata definitivamente."))
        except ops.OpError as err:
            messages.error(request, str(err))
        return redirect("bin")
    items = list(data.readable_entries(user, binned=True).order_by("-deleted_utc")[:500])
    states.annotate(items)
    return render(request, "web/bin.html", {
        "items": [(e, data.writable(user, e)) for e in items],
        "restore": states.buttons(["restore"])[0], "purge": states.buttons(["purge"])[0],
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
    now = timezone.now()
    show = request.GET.get("show", "open")
    # a manager or an admin: their own, one person's, or everyone's they assign to ("all")
    people = data.assignable(user)
    who = request.GET.get("person", "")
    q = data.team_todos(user)
    if who == "all" and people.count() > 1:
        pass
    elif who and people.filter(pk=data.parse_pk(who)).exists():
        q = q.filter(owner_id=who)
    else:
        who = ""
        q = q.filter(owner=user)
    # in the bin they are not listed (the bin page has them); absences have their own tab
    q = q.filter(deleted_utc__isnull=True)
    rows = list(q.order_by("due_date", "plan_date", "activity"))
    if show in ("absences", "all"):
        owners = people if who == "all" else [user] if not who else [who]
        absences = Entry.objects.filter(
            org=user.org, deleted=False, deleted_utc__isnull=True, kind=ActivityKind.ABSENCE,
            owner__in=owners)
        rows += list(absences.select_related("company", "owner").order_by("-created_utc"))
    states.annotate(rows, now)
    wanted = {"open": states.OPEN, "done": (A.DONE,), "absences": (A.ABSENCE,)}.get(show)
    items = [(t, t.shown) for t in rows if wanted is None or t.shown in wanted]
    return render(request, "web/todos.html", {
        "items": items, "show": show, "people": people if people.count() > 1 else None, "who": who,
        "person_q": f"&person={who}" if who else "",
    })


def _absence_days(entry):
    """(first day, last day, start "HH:MM", end "HH:MM") of an absence's segments, as the form has."""
    tz = timezone.get_current_timezone()
    segs = list(Segment.objects.filter(entry=entry, deleted=False).order_by("start_utc"))
    if not segs:
        return "", "", "", ""
    local = [(timezone.localtime(x.start_utc, tz), x.end_utc and timezone.localtime(x.end_utc, tz))
             for x in segs]
    first, last = local[0][0].date(), local[-1][0].date()
    days = {a.date() for a, _ in local}
    # times: one segment a day, all from the same time to the same time
    same = len(days) == len(local) and all(b and b.date() == a.date() for a, b in local) \
        and len({(a.strftime("%H:%M"), b.strftime("%H:%M")) for a, b in local}) == 1
    start, end = ("", "")
    if same:
        start, end = local[0][0].strftime("%H:%M"), local[0][1].strftime("%H:%M")
    return first.isoformat(), last.isoformat() if last != first else "", start, end


@login_required
def todo_edit(request, uid=None):
    """
    A to-do (a TODO activity) or an absence (an ABSENCE activity), new or
    changed; the form of the to-do of before 1.0.
    """
    user = request.user
    if not user.syncs:
        raise PermissionDenied
    people = data.assignable(user)
    todo = None
    if uid:
        absences = Entry.objects.filter(
            org=user.org, deleted=False, deleted_utc__isnull=True, kind=ActivityKind.ABSENCE,
            owner__in=people)
        absences = absences.select_related("company", "client", "owner", "assignment__by")
        todos = data.team_todos(user).filter(deleted_utc__isnull=True)
        todo = get_object_or_404(todos | absences, uid=uid)
    absence = (todo.kind if todo else request.POST.get("kind") or request.GET.get("kind")) == ActivityKind.ABSENCE
    # opened from the agenda: back there afterwards
    back = request.POST.get("next") or request.GET.get("next") or ""
    if not url_has_allowed_host_and_scheme(back, allowed_hosts={request.get_host()}, require_https=request.is_secure()):
        back = ""
    back = back or reverse("todos")
    days = _absence_days(todo) if todo and absence else ("", "", "", "")
    if request.method == "POST":
        p = request.POST
        try:
            command = p.get("action") or LEGACY_STATES.get(p.get("state"))
            if command and todo:
                if command not in TODO_COMMANDS or (absence and command != "delete"):
                    raise ops.OpError(_("azione sconosciuta"))
                if command not in ("tick", "untick", "delete") and todo.owner_id != user.pk:
                    raise PermissionDenied
                to = _command(request, todo, command, actor=todo.owner)
                return redirect(to if command.startswith(("start", "reopen")) else back)
            title = p.get("title", "").strip()
            if not title and not absence:
                raise ops.OpError(_("il titolo è obbligatorio"))
            owner = todo.owner if todo else user
            to = people.filter(pk=data.parse_pk(p.get("owner"))).first() if p.get("owner") else owner
            if to is None:
                raise PermissionDenied

            def minutes(t):
                hh, mm = t.split(":")
                return int(hh) * 60 + int(mm)
            plan_time = p.get("plan_time") or None
            if absence:
                if not p.get("plan_date"):
                    raise ops.OpError(_("la data è obbligatoria"))
                first = date.fromisoformat(p["plan_date"])
                last = date.fromisoformat(p["plan_end"]) if p.get("plan_end") else first
                if not 0 <= (last - first).days < MAX_ABSENCE_DAYS:
                    raise ops.OpError(_("l'ultimo giorno non è valido"))
                start, duration = None, None
                if plan_time:
                    start = minutes(plan_time)
                    if p.get("end_time"):
                        duration = minutes(p["end_time"]) - start
                        if duration <= 0:
                            raise ops.OpError(_("l'ora di fine deve seguire quella di inizio"))
                    elif p.get("duration"):
                        duration = int(p["duration"])
                kind = p.get("absence_kind") if p.get("absence_kind") in A.ABSENCE_KINDS else \
                    A.absence_kind_of(title) or (todo.absence_kind if todo else A.ABSENCE)
                label = None if A.absence_kind_of(title) else title or None
                company = _pick(user, Company, p.get("company"))
                if todo and to != todo.owner:
                    raise PermissionDenied
                # the segments are written anew only when the days or times change
                end_text = p.get("end_time") or ""
                if not end_text and plan_time and duration:
                    end_text = hhmm_add(plan_time, duration)
                last_text = p.get("plan_end", "") if p.get("plan_end") != p.get("plan_date") else ""
                same = todo and (p.get("plan_date", ""), last_text,
                                 plan_time or "", end_text if plan_time else "") == days
                notes = p.get("notes", "").strip() or None
                if same:
                    ops.edit_entry(to, todo, {
                        "absenceKind": kind, "notes": notes, "activity": label,
                        "companyUid": str(company.uid) if company else None})
                else:
                    ops.save_absence(user, todo.uid if todo else None, first=first, last=last, start_min=start, duration_min=duration,
                                     absence_kind=kind, activity=label, company=company,
                                     notes=notes, owner=to)
                messages.success(request, _("Voce salvata.") if to == user else _("Voce assegnata a %(name)s.") % {"name": to.get_full_name() or to.username})
                return redirect(back)
            client = _pick(user, Client, p.get("client"))
            company = client.company if client and client.company_id else _pick(user, Company, p.get("company"))
            due_time = p.get("due_time") or None
            fields = {
                "activity": title, "notes": p.get("notes", "").strip() or None,
                "companyUid": str(company.uid) if company else None, "clientUid": str(client.uid) if client else None,
                "dueDate": p.get("due_date") or None, "dueTimeMin": minutes(due_time) if due_time else None,
                "planDate": p.get("plan_date") or None, "planStartMin": minutes(plan_time) if plan_time and p.get("plan_date") else None,
                "durationMin": int(p["duration"]) if p.get("duration") else None,
                # 0 none .. 5 emergency (ACTIVITY-MODEL §5)
                "priority": min(max(int(p["priority"]), 0), A.PRIORITY_MAX) if p.get("priority") else 0,
            }
            if todo and to != todo.owner:
                ops.assign(user, todo, to)
            saved = ops.save_todo(user, todo.uid if todo else None, fields, owner=to)
            if not todo and to != user:
                ops.assign(user, Entry.objects.get(uid=saved), to)
            messages.success(request, _("Voce salvata.") if to == user else _("Voce assegnata a %(name)s.") % {"name": to.get_full_name() or to.username})
            return redirect(back)
        except (ops.OpError, ValueError) as err:
            messages.error(request, str(err))

    def hhmm(m):
        return f"{m // 60:02d}:{m % 60:02d}" if m is not None else ""
    end_time = ""
    if absence and todo:
        plan_date, plan_end, plan_time, end_time = days
        duration = ""
    elif absence:
        plan_date, plan_end, duration = _iso_date(request.GET.get("plan_date")), "", ""
        plan_time = _hhmm(request.GET.get("plan_time"))
    else:
        plan_date = todo.plan_date.isoformat() if todo and todo.plan_date else "" if todo else _iso_date(request.GET.get("plan_date"))
        plan_end, duration = "", todo.duration_min if todo and todo.duration_min else ""
        plan_time = hhmm(todo.plan_start_min) if todo else _hhmm(request.GET.get("plan_time"))
    mine = todo is None or todo.owner_id == user.pk
    actions = []
    if todo:
        states.annotate([todo])
        keys = states.allowed(todo.shown, bool(ops.real_segments(todo)))
        actions = states.buttons(
            [k for k in keys if k in TODO_COMMANDS and (mine or k in ("tick", "untick", "delete"))])
    return render(request, "web/todo_edit.html", {
        "can_start": bool(todo and mine and todo.shown in states.OPEN),
        "actions": actions, "form_url": "", "field": "action",
        "absence_kinds": [(k, states.ABSENCE_LABELS[k]) for k in A.ABSENCE_KINDS],
        "priorities": states.PRIORITIES, "end_time": end_time, "t": todo,
        "companies": data.companies(user), "clients": data.clients(user),
        "due_time": hhmm(todo.due_time_min) if todo else "",
        "absence": absence, "plan_end": plan_end, "plan_date": plan_date, "plan_time": plan_time, "duration": duration,
        "done": bool(todo and todo.status == EntryStatus.CLOSED and not absence),
        "next": back if back != reverse("todos") else "",
        "people": people if people.count() > 1 else None,
        "owner_pk": todo.owner_id if todo else int(request.GET["person"]) if request.GET.get("person", "").isdigit() else user.pk,
        "mine": mine,
    })


def hhmm_add(text, minutes):
    """The time of day [minutes] after "HH:MM" (within the day)."""
    hh, mm = text.split(":")
    total = int(hh) * 60 + int(mm) + minutes
    return f"{total // 60 % 24:02d}:{total % 60:02d}"


def _iso_date(text):
    try:
        return date.fromisoformat(text).isoformat() if text else ""
    except ValueError:
        return ""


def _hhmm(text):
    try:
        return datetime.strptime(text, "%H:%M").strftime("%H:%M") if text else ""
    except ValueError:
        return ""


@login_required
@require_POST
def quick(request, uid):
    """The commands of the Home cards."""
    e = _entry(request, uid, write=True)
    try:
        to = _command(request, e, request.POST.get("action"))
    except ops.OpError as err:
        messages.error(request, str(err))
        return redirect("home")
    if to and "?ask=" in to:
        return redirect(to)
    e.refresh_from_db()
    stopped = request.POST.get("action") in ("stop", "finish", "stop_return")
    if stopped and e.status == EntryStatus.CLOSED and _is_incomplete(e):
        return redirect("entry", uid=e.uid)
    return redirect("home")
