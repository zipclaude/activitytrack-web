# SPDX-License-Identifier: GPL-3.0-or-later
"""The graphical policy editor (docs/ARCHITECTURE.md §7), built from the app's catalog."""

import json

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied
from django.shortcuts import get_object_or_404, redirect, render
from django.utils.translation import gettext as _

from accounts.models import Profile, User

from .catalog import catalog, check_value
from .models import Policy

# a setting: not set / starting value (the user may change it) / locked / hidden
MODES = ("", "value", "locked", "hidden")


def _admin(request):
    if not request.user.is_admin:
        raise PermissionDenied


def _scope(request, scope):
    """(label, lookup) of a scope name: org, profile-<p>, user-<id>."""
    org = request.user.org
    if scope == "org":
        return _("Organizzazione"), {"org": org, "profile": "", "user": None}
    if scope.startswith("profile-"):
        p = scope[8:]
        if p not in (Profile.MANAGER, Profile.USER):
            raise PermissionDenied
        return _("Profilo: %(p)s") % {"p": Profile(p).label}, {"org": org, "profile": p, "user": None}
    if scope.startswith("user-"):
        u = get_object_or_404(User, pk=scope[5:], org=org)
        return _("Utente: %(u)s") % {"u": u.get_full_name() or u.username}, {"org": org, "profile": "", "user": u}
    raise PermissionDenied


@login_required
def index(request):
    _admin(request)
    org = request.user.org
    users = User.objects.filter(org=org, is_active=True).exclude(profile=Profile.READER).order_by("username")
    with_policy = {p.user_id for p in Policy.objects.filter(org=org, user__isnull=False)}
    scopes = [("org", _("Organizzazione"), Policy.objects.filter(org=org, profile="", user__isnull=True).first())]
    for p in (Profile.MANAGER, Profile.USER):
        scopes.append((f"profile-{p}", _("Profilo: %(p)s") % {"p": Profile(p).label}, Policy.objects.filter(org=org, profile=p, user__isnull=True).first()))
    for u in users.filter(pk__in=with_policy):
        scopes.append((f"user-{u.pk}", _("Utente: %(u)s") % {"u": u.get_full_name() or u.username}, u.policy))
    return render(request, "policies/index.html", {"scopes": scopes, "users": users.exclude(pk__in=with_policy)})


def _initial(data):
    """The form's values from a stored policy body."""
    v = {}
    for k, on in data.get("features", {}).items():
        v[f"feature:{k}"] = "on" if on else "off"
    for k, attrs in data.get("fields", {}).items():
        for a in ("visible", "enabled", "required"):
            if a in attrs:
                v[f"field:{k}:{a}"] = "yes" if attrs[a] else "no"
        for lang, text in attrs.get("label", {}).items():
            v[f"field:{k}:label:{lang}"] = text
    for k, s in data.get("settings", {}).items():
        if isinstance(s, dict):
            mode = "hidden" if s.get("hidden") else "locked" if s.get("locked") else "value"
            value = s.get("value")
        else:
            mode, value = "value", s
        v[f"setting:{k}:mode"] = mode
        v[f"setting:{k}"] = value
    for k, value in data.get("behaviours", {}).items():
        v[f"behaviour:{k}"] = value
    v["entries"] = data.get("entries", {}).get("source", "")
    v["masterData"] = data.get("masterData", {}).get("source", "")
    v["export"] = data.get("export", {}).get("formats", [])
    v["language"] = data.get("ui", {}).get("language", "")
    return v


def _parse(post):
    """The policy body from the form, and the errors by field."""
    c = catalog()
    data, errors = {}, {}
    features = {f["key"]: post.get(f"feature:{f['key']}") == "on" for f in c["features"] if post.get(f"feature:{f['key']}") in ("on", "off")}
    for f in c["features"]:
        if f["essential"] and features.get(f["key"]) is False:
            errors[f"feature:{f['key']}"] = _("essenziale: non si disattiva")
    if features:
        data["features"] = features
    fields = {}
    for f in c["fields"]:
        attrs = {}
        for a in ("visible", "enabled", "required"):
            choice = post.get(f"field:{f['key']}:{a}")
            if choice in ("yes", "no"):
                attrs[a] = choice == "yes"
        labels = {lang: post.get(f"field:{f['key']}:label:{lang}", "").strip() for lang in ("it", "en")}
        labels = {k: t for k, t in labels.items() if t}
        if labels:
            attrs["label"] = labels
        if f["essential"] and (attrs.get("visible") is False or attrs.get("enabled") is False or attrs.get("required") is False):
            errors[f"field:{f['key']}"] = _("essenziale: resta visibile, attivo e obbligatorio")
        if attrs.get("required") and (attrs.get("visible") is False or attrs.get("enabled") is False):
            errors[f"field:{f['key']}"] = _("un campo nascosto o non modificabile non può essere obbligatorio")
        if attrs:
            fields[f["key"]] = attrs
    if fields:
        data["fields"] = fields
    settings = {}
    for s in c["settings"]:
        key = s["key"]
        mode = post.get(f"setting:{key}:mode", "")
        if mode not in MODES[1:]:
            continue
        if s["kind"] == "STRING" and s.get("multiple"):
            raw = ",".join(post.getlist(f"setting:{key}"))
        else:
            raw = post.get(f"setting:{key}", "").strip()
        try:
            if s["kind"] == "BOOLEAN":
                value = raw == "true"
            elif s["kind"] == "INT":
                value = int(raw)
            elif s["kind"] == "DOUBLE":
                value = float(raw.replace(",", "."))
            else:
                value = raw
        except ValueError:
            errors[f"setting:{key}"] = _("valore non valido")
            continue
        problem = check_value(s, value)
        if problem:
            errors[f"setting:{key}"] = problem
            continue
        settings[key] = value if mode == "value" else {"value": value, mode: True}
    if settings:
        data["settings"] = settings
    behaviours = {}
    for b in c["behaviours"]:
        raw = post.get(f"behaviour:{b['key']}", "").strip()
        if raw == "":
            continue
        if b["kind"] == "BOOLEAN":
            behaviours[b["key"]] = raw == "true"
        else:
            try:
                behaviours[b["key"]] = int(raw)
            except ValueError:
                errors[f"behaviour:{b['key']}"] = _("numero intero")
    if behaviours:
        data["behaviours"] = behaviours
    if post.get("entries") in c["entriesSource"]:
        data["entries"] = {"source": post["entries"]}
    if post.get("masterData") in c["masterDataSource"]:
        data["masterData"] = {"source": post["masterData"]}
    formats = [f for f in post.getlist("export") if f in c["exportFormats"]]
    if formats:
        data["export"] = {"formats": formats}
    if post.get("language") in c["languages"]:
        data["ui"] = {"language": post["language"]}
    return data, errors


@login_required
def edit(request, scope):
    _admin(request)
    label, lookup = _scope(request, scope)
    policy = Policy.objects.filter(**lookup).first()
    if request.method == "POST":
        if "delete" in request.POST:
            if policy and Policy.objects.filter(org=lookup["org"]).exclude(pk=policy.pk).exists():
                policy.delete()
            elif policy:
                # the last one becomes empty: a phone keeps its policy until
                # another replaces it (the app's POLICY.md), so it must get one
                policy.data = {}
                policy.revision += 1
                policy.save()
            messages.success(request, _("Policy di «%(s)s» eliminata.") % {"s": label})
            return redirect("policies:index")
        data, errors = _parse(request.POST)
        if not errors:
            policy = policy or Policy(**lookup)
            policy.data = data
            policy.revision += 1
            policy.save()
            messages.success(request, _("Policy di «%(s)s» salvata (revisione %(r)d).") % {"s": label, "r": policy.revision})
            return redirect("policies:edit", scope=scope)
        values = {k: (request.POST.getlist(k) if len(request.POST.getlist(k)) > 1 else request.POST.get(k)) for k in request.POST}
    else:
        errors, values = {}, _initial(policy.data if policy else {})
    # the phones of the scope whose app would refuse this policy
    from accounts.models import Device

    from .catalog import unknown_to_phones

    devices = Device.objects.filter(user__org=lookup["org"], revoked__isnull=True).select_related("user")
    if lookup["user"] is not None:
        devices = devices.filter(user=lookup["user"])
    elif lookup["profile"]:
        devices = devices.filter(user__profile=lookup["profile"])
    too_old = unknown_to_phones(policy.data if policy else {}, list(devices))
    return render(request, "policies/edit.html", {
        "too_old": too_old,
        "label": label,
        "scope": scope,
        "policy": policy,
        "c": catalog(),
        "v": values,
        "errors": errors,
        "preview": json.dumps(policy.data, ensure_ascii=False, indent=2) if policy else "",
    })
