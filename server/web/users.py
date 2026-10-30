# SPDX-License-Identifier: GPL-3.0-or-later
"""Users of the organisation, by an admin (ARCHITECTURE.md §5), and one's own password."""

from django.contrib import messages
from django.contrib.auth import update_session_auth_hash
from django.contrib.auth.decorators import login_required
from django.contrib.auth.password_validation import password_validators_help_texts, validate_password
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction
from django.db.models import Count, Q
from django.shortcuts import get_object_or_404, redirect, render
from django.utils.translation import gettext as _
from django.views.decorators.http import require_POST

from accounts.models import Profile, User
from records.models import Company


def _admin(request):
    if not request.user.is_admin:
        raise PermissionDenied
    return request.user


def _check_password(password, again, user):
    if password != again:
        raise ValidationError(_("Le due password sono diverse."))
    validate_password(password, user)


@login_required
def users(request):
    admin = _admin(request)
    g = request.GET
    q = User.objects.filter(org=admin.org).select_related("manager").annotate(
        phones=Count("devices", filter=Q(devices__revoked__isnull=True)))
    if g.get("profile") in Profile.values:
        q = q.filter(profile=g["profile"])
    if g.get("state") != "all":
        q = q.filter(is_active=g.get("state") != "inactive")
    if g.get("q"):
        text = g["q"].strip()
        q = q.filter(Q(username__icontains=text) | Q(first_name__icontains=text) | Q(last_name__icontains=text) | Q(email__icontains=text))
    return render(request, "web/users.html", {"users": q.order_by("first_name", "last_name", "username"), "f": g, "profiles": Profile.choices})


@login_required
def user_edit(request, pk=None):
    admin = _admin(request)
    u = get_object_or_404(User, org=admin.org, pk=pk) if pk else None
    managers = User.objects.filter(org=admin.org, is_active=True, profile__in=[Profile.MANAGER, Profile.ADMIN]).exclude(pk=pk).order_by("first_name", "username")
    companies = Company.objects.filter(org=admin.org, deleted=False, owner__isnull=True).order_by("name")
    errors = {}
    if request.method == "POST":
        p = request.POST
        username = p.get("username", "").strip()
        profile = p.get("profile") if p.get("profile") in Profile.values else Profile.USER
        active = bool(p.get("active"))
        if not username:
            errors["username"] = _("Il nome utente è obbligatorio.")
        elif User.objects.filter(username__iexact=username).exclude(pk=pk).exists():
            errors["username"] = _("Questo nome utente esiste già.")
        if u == admin and profile != Profile.ADMIN and not admin.is_superuser:
            errors["profile"] = _("Non puoi toglierti il profilo di amministratore.")
        if u == admin and not active:
            errors["active"] = _("Non puoi disattivare il tuo utente.")
        target = u or User(org=admin.org)
        target.username, target.first_name, target.last_name = username, p.get("first_name", "").strip(), p.get("last_name", "").strip()
        target.email = p.get("email", "").strip()
        if u is None:
            try:
                _check_password(p.get("password", ""), p.get("password2", ""), target)
            except ValidationError as e:
                errors["password"] = " ".join(e.messages)
        if not errors:
            with transaction.atomic():
                target.profile, target.is_active = profile, active
                target.manager = managers.filter(pk=p.get("manager")).first() if p.get("manager") and profile != Profile.READER else None
                if u is None:
                    target.set_password(p["password"])
                target.save()
                target.companies.set(companies.filter(uid__in=p.getlist("companies")) if profile in (Profile.READER, Profile.MANAGER) else [])
            messages.success(request, _("Utente «%(name)s» salvato.") % {"name": target.get_full_name() or target.username})
            return redirect("user_edit", pk=target.pk)
        u_form = target
    else:
        u_form = u
    return render(request, "web/user_edit.html", {
        "u": u, "form": u_form, "errors": errors, "profiles": Profile.choices, "managers": managers, "companies": companies,
        "chosen": {str(c) for c in (request.POST.getlist("companies") if request.method == "POST" else (u.companies.values_list("uid", flat=True) if u else []))},
        "team": u.team.filter(is_active=True).order_by("first_name", "username") if u else [],
        "devices": u.devices.order_by("revoked", "-last_seen") if u else [],
        "help": password_validators_help_texts(),
    })


@login_required
@require_POST
def user_password(request, pk):
    """An admin sets a new password; the user's sessions end, the phones stay paired."""
    admin = _admin(request)
    u = get_object_or_404(User, org=admin.org, pk=pk)
    try:
        _check_password(request.POST.get("password", ""), request.POST.get("password2", ""), u)
    except ValidationError as e:
        messages.error(request, " ".join(e.messages))
        return redirect("user_edit", pk=pk)
    u.set_password(request.POST["password"])
    u.save(update_fields=["password"])
    if u == admin:
        update_session_auth_hash(request, u)
    messages.success(request, _("Password di «%(name)s» cambiata.") % {"name": u.get_full_name() or u.username})
    return redirect("user_edit", pk=pk)


@login_required
def my_password(request):
    """Everyone changes their own password."""
    u = request.user
    if request.method == "POST":
        p = request.POST
        try:
            if not u.check_password(p.get("old", "")):
                raise ValidationError(_("La password attuale non è corretta."))
            _check_password(p.get("password", ""), p.get("password2", ""), u)
        except ValidationError as e:
            messages.error(request, " ".join(e.messages))
        else:
            u.set_password(p["password"])
            u.save(update_fields=["password"])
            update_session_auth_hash(request, u)
            messages.success(request, _("Password cambiata."))
            return redirect("home")
    return render(request, "web/my_password.html", {"help": password_validators_help_texts()})
