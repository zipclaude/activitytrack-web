# SPDX-License-Identifier: GPL-3.0-or-later
"""
A user's personal master data made central (docs/ARCHITECTURE.md §6, issue #70).

The record keeps its uid and changes owner in place (`owner = NULL`) under a new revision: what points at it
stays valid, the owner's phones pull it again as central (`central: true`) and the other users' phones pull
it as a new central record, adopting their own personal one of the same name as at any pull (SYNC.md §7).
Nothing is copied or deleted. What cannot be promoted is left alone and said:

- a live central record already has its natural key (company name or code, client company + name or code,
  role name, tag name), compared without case;
- it points at a record the other users could not see (a client of a personal company, a preset or a material
  of a personal company or role): promoted only when that one is central or is promoted in the same run.

The sites and contacts of a promoted company or client follow it, as new children copy their parent's owner.
"""

from dataclasses import dataclass, field

from django.core.exceptions import PermissionDenied
from django.db import transaction
from django.utils.translation import gettext as _
from django.utils.translation import gettext_lazy

from records.models import Client, Company, Material, Preset, Role, Synced, Tag
from sync import engine

# parents before the records that point at them: also the order of the revisions the phones pull
KINDS = [
    (Company, gettext_lazy("Aziende")), (Role, gettext_lazy("Ruoli")), (Tag, gettext_lazy("Tag")),
    (Client, gettext_lazy("Clienti")), (Preset, gettext_lazy("Scelte rapide")), (Material, gettext_lazy("Materiali")),
]


def label(rec):
    return getattr(rec, "name", None) or getattr(rec, "text", "") or str(rec.uid)


def _text(value):
    return (value or "").strip().casefold()


def _keys(rec):
    """The natural keys of a record (as the phones' adoption reads them); none for presets and materials."""
    if isinstance(rec, Company):
        return [("code", _text(rec.external_code)), ("name", _text(rec.name))]
    if isinstance(rec, Client):
        return [(rec.company_id, "code", _text(rec.external_code)), (rec.company_id, "name", _text(rec.name))]
    if isinstance(rec, (Role, Tag)):
        return [("name", _text(rec.name))]
    return []


def _refs(rec):
    """The master records a record points at."""
    out = []
    for f in rec._meta.concrete_fields:
        if f.is_relation and f.related_model is not None and issubclass(f.related_model, Synced):
            target = getattr(rec, f.name)
            if target is not None:
                out.append(target)
    return out


@dataclass
class Plan:
    # (record, kind title) that become central, in the order of their revisions
    eligible: list = field(default_factory=list)
    # (record, kind title, why not)
    skipped: list = field(default_factory=list)


def plan(user, uid=None):
    """What making central would do with the user's own live personal records (only [uid] when given)."""
    out = Plan()
    promoted = set()
    for model, title in KINDS:
        taken = {}
        for c in model.objects.filter(org=user.org, owner__isnull=True, deleted=False):
            for k in _keys(c):
                if k[-1]:
                    taken.setdefault(k, c)
        mine = model.objects.filter(org=user.org, owner=user, deleted=False, archived=False) if hasattr(model, "archived") \
            else model.objects.filter(org=user.org, owner=user, deleted=False)
        if uid is not None:
            mine = mine.filter(uid=uid)
        for rec in mine.order_by("name" if hasattr(model, "name") else "text"):
            why = None
            for target in _refs(rec):
                if target.owner_id is not None and target.uid not in promoted:
                    why = _("«%(n)s» è personale: va resa centrale prima, o insieme") % {"n": label(target)}
                    break
            if why is None:
                for k in _keys(rec):
                    clash = taken.get(k) if k[-1] else None
                    if clash is not None:
                        why = _("esiste già una voce centrale con lo stesso nome o codice: «%(n)s»") % {"n": label(clash)}
                        break
            if why:
                out.skipped.append((rec, title, why))
                continue
            for k in _keys(rec):
                if k[-1]:
                    taken.setdefault(k, rec)
            promoted.add(rec.uid)
            out.eligible.append((rec, title))
    return out


def promote(user, uid=None):
    """
    Makes central what plan() allows, judged again under the organisation's lock (a push or another
    promotion may have come first); returns the Plan. One revision for each record, parents first: the
    pages of a pull end at a revision, records sharing one could be lost at the boundary.
    """
    if not user.can_edit_central:
        raise PermissionDenied
    with transaction.atomic():
        engine.next_rev(user.org)
        done = plan(user, uid)
        for rec, _title in done.eligible:
            _own_to_central(rec, user)
    return done


def _own_to_central(rec, user):
    type(rec).objects.filter(pk=rec.pk).update(owner=None, rev=engine.next_rev(rec.org))
    for child, parent in engine._children(type(rec)):
        for c in child.objects.filter(org=rec.org, deleted=False, owner_id=user.pk, **{parent: rec}).order_by("pk"):
            _own_to_central(c, user)
