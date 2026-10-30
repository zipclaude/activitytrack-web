# SPDX-License-Identifier: GPL-3.0-or-later
"""Master data in the browser: central ones for admins and managers, personal ones for everyone."""

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied
from django.db.models import Q
from django.http import Http404
from django.shortcuts import get_object_or_404, redirect, render
from django.utils.translation import gettext as _
from django.utils.translation import gettext_lazy

from records.models import Client, Company, Entry, Material, Preset, Role, Tag, tag_name

from . import data, ops

# the app's palette (Colors.kt PALETTE), 0xRRGGBB
PALETTE = [
    (0x3D7BEA, gettext_lazy("blu")), (0x00A3A3, gettext_lazy("verde acqua")), (0x2E9D4E, gettext_lazy("verde")),
    (0x8BB82D, gettext_lazy("lime")), (0xF2B705, gettext_lazy("giallo")), (0xE8862A, gettext_lazy("arancione")),
    (0xD64545, gettext_lazy("rosso")), (0xC2185B, gettext_lazy("magenta")), (0x8E5CD9, gettext_lazy("viola")),
    (0x5B6BD6, gettext_lazy("indaco")), (0x8D6E63, gettext_lazy("marrone")), (0x78909C, gettext_lazy("grigio")),
]

# kind -> model, wire type, editable fields (wire name, label, input)
KINDS = {
    "aziende": (Company, "company", gettext_lazy("Aziende")),
    "clienti": (Client, "client", gettext_lazy("Clienti")),
    "ruoli": (Role, "role", gettext_lazy("Ruoli")),
    "tag": (Tag, "tag", gettext_lazy("Tag")),
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
    elif model is Tag:
        q = q.order_by("position", "name")
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
    if model is Tag:
        color = p.get("color", "")
        return {"name": tag_name(p.get("name", "")), "archived": bool(p.get("archived")),
                "color": int(color) if color and int(color) in {c for c, _l in PALETTE} else None}
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
            if model is Tag:
                _unique_tag(user, rec, fields["name"], central)
            ops.put(user, type_, rec.uid if rec else ops.new_uid(), fields, central=central)
            if model is Tag and rec is not None and rec.name != fields["name"]:
                rename_tag(rec, rec.name, fields["name"])
            messages.success(request, _("Salvato."))
            return redirect("masters", kind=kind)
        except (ops.OpError, ValueError) as err:
            messages.error(request, str(err))
    return render(request, "web/master_edit.html", {
        "kind": kind, "title": title, "r": rec, "model": model.__name__, "companies": data.companies(user),
        "can_central": user.can_edit_central and rec is None, "palette": PALETTE,
    })


def _unique_tag(user, rec, name, central):
    """No two tags of one name where a user sees them (issue #133)."""
    central = central or (rec is not None and rec.owner_id is None)
    q = Tag.objects.filter(org=user.org, deleted=False, name=name)
    if rec is not None:
        q = q.exclude(uid=rec.uid)
    if not central:
        q = q.filter(Q(owner__isnull=True) | Q(owner=user))
    else:
        q = q.filter(owner__isnull=True)
    if q.exists():
        raise ops.OpError(_("esiste già un tag «%(n)s»") % {"n": name})


def rename_tag(tag, old, new):
    """
    A renamed tag follows on the entries that carry it: a central one on
    every user's, a personal one on its owner's; each written as its owner's
    so the phones pull it.
    """
    q = Entry.objects.filter(org=tag.org, deleted=False).select_related("owner")
    if tag.owner_id:
        q = q.filter(owner_id=tag.owner_id)
    for e in q:
        if old in (e.tags or []):
            tags = []
            for t in e.tags:
                t = new if t == old else t
                if t not in tags:
                    tags.append(t)
            ops.put(e.owner, "entry", e.uid, {"tags": sorted(tags), "updatedUtcMs": ops.engine.now_ms()})
