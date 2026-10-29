# SPDX-License-Identifier: GPL-3.0-or-later
"""Master data in the browser: central ones for admins and managers, personal ones for everyone."""

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied
from django.http import Http404
from django.shortcuts import get_object_or_404, redirect, render
from django.utils.translation import gettext as _
from django.utils.translation import gettext_lazy

from records.models import Client, Company, Material, Preset, Role

from . import data, ops

# kind -> model, wire type, editable fields (wire name, label, input)
KINDS = {
    "aziende": (Company, "company", gettext_lazy("Aziende")),
    "clienti": (Client, "client", gettext_lazy("Clienti")),
    "ruoli": (Role, "role", gettext_lazy("Ruoli")),
    "preset": (Preset, "preset", gettext_lazy("Scelte rapide")),
    "materiali": (Material, "material", gettext_lazy("Materiali")),
}


def _kind(kind):
    if kind not in KINDS:
        raise Http404
    return KINDS[kind]


@login_required
def index(request, kind="aziende"):
    user = request.user
    model, _type, title = _kind(kind)
    q = data.masters(user, model)
    if model is Client:
        q = q.select_related("company").order_by("company__name", "name")
    elif model is Preset:
        q = q.order_by("text")
    else:
        q = q.order_by("name")
    return render(request, "web/masters.html", {"kind": kind, "kinds": KINDS, "title": title, "items": q, "can_central": user.can_edit_central})


def _fields(request, model):
    p = request.POST
    user = request.user
    if model is Company:
        return {"name": p.get("name", "").strip(), "archived": bool(p.get("archived")), "externalCode": p.get("external_code", "").strip() or None}
    if model is Client:
        company = data.masters(user, Company).filter(uid=p.get("company")).first() if p.get("company") else None
        return {"name": p.get("name", "").strip(), "archived": bool(p.get("archived")), "companyUid": str(company.uid) if company else None,
                "externalCode": p.get("external_code", "").strip() or None}
    if model is Role:
        return {"name": p.get("name", "").strip(), "archived": bool(p.get("archived"))}
    if model is Preset:
        return {"text": p.get("text", "").strip()}
    cents = p.get("unit_cost", "").replace(",", ".").strip()
    return {"name": p.get("name", "").strip(), "archived": bool(p.get("archived")), "unit": p.get("unit", "").strip() or None,
            "unitCostCents": round(float(cents) * 100) if cents else None}


@login_required
def edit(request, kind, uid=None):
    user = request.user
    model, type_, title = _kind(kind)
    rec = get_object_or_404(data.masters(user, model), uid=uid) if uid else None
    if rec is not None and not (rec.owner_id == user.pk or (rec.owner_id is None and user.can_edit_central)):
        raise PermissionDenied
    if request.method == "POST":
        try:
            if request.POST.get("delete") and rec:
                ops.delete(user, type_, rec.uid)
                messages.success(request, _("Eliminato."))
                return redirect("masters", kind=kind)
            fields = _fields(request, model)
            if not (fields.get("name") or fields.get("text")):
                raise ops.OpError(_("il nome è obbligatorio"))
            central = rec is None and bool(request.POST.get("central")) and user.can_edit_central
            ops.put(user, type_, rec.uid if rec else ops.new_uid(), fields, central=central)
            messages.success(request, _("Salvato."))
            return redirect("masters", kind=kind)
        except (ops.OpError, ValueError) as err:
            messages.error(request, str(err))
    return render(request, "web/master_edit.html", {
        "kind": kind, "title": title, "r": rec, "model": model.__name__, "companies": data.companies(user),
        "can_central": user.can_edit_central and rec is None,
    })
