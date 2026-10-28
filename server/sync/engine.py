# SPDX-License-Identifier: GPL-3.0-or-later
"""
Applying changes and listing them (docs/SYNC.md). Phones and the web use
the same apply(), so a field edited on the web and on a phone is settled by
the same rule: the later change wins, field by field.
"""

import time
import uuid

from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import F, Q

from records.models import BY_TYPE, SYNCED_MODELS, Scope

from . import wire
from .models import Counter, ScopeExit

MAX_CHANGES = 500


class ChangeError(Exception):
    def __init__(self, code, detail=""):
        super().__init__(code, detail)
        self.code = code
        self.detail = detail


def now_ms():
    return int(time.time() * 1000)


def next_rev(org):
    """The next revision; locks the counter until the caller's transaction ends (SYNC.md §4)."""
    Counter.objects.get_or_create(org=org)
    Counter.objects.filter(org=org).update(value=F("value") + 1)
    return Counter.objects.select_for_update().get(org=org).value


def head(org):
    counter = Counter.objects.filter(org=org).first()
    return counter.value if counter else 0


def visible(user):
    """The records a user's phones receive: their own and the central ones."""
    return Q(owner=user) | Q(owner__isnull=True)


def can_write(user, rec):
    if rec.owner_id is None:
        return user.can_edit_central
    return rec.owner_id == user.pk


def _children(model):
    """(child model, parent field) of the CHILD types whose parent is model."""
    return [(m, m.PARENT) for m in SYNCED_MODELS if m.SCOPE == Scope.CHILD and m._meta.get_field(m.PARENT).related_model is model]


def _tombstone(rec, org):
    rec.deleted = True
    rec.rev = next_rev(org)
    rec.save(update_fields=["deleted", "rev"])
    for child, parent in _children(type(rec)):
        for c in child.objects.filter(org=org, deleted=False, **{parent: rec}):
            _tombstone(c, org)


def _resolve(user, org, wf, ref_uid):
    target = wf.field.related_model.objects.filter(org=org, uid=ref_uid).filter(visible(user)).first()
    if target is None:
        raise ChangeError("unknown_ref", f"{wf.name}: {ref_uid}")
    return target


def _owner_of_new(user, model, rec, central):
    if model.SCOPE == Scope.MASTER and central:
        if not user.can_edit_central:
            raise ChangeError("forbidden", "central")
        return None
    if model.SCOPE in (Scope.MASTER, Scope.USER):
        return user.pk
    parent = getattr(rec, model.PARENT, None)
    if parent is None:
        raise ChangeError("invalid", f"{model.PARENT} required")
    if parent.owner_id == user.pk or (parent.owner_id is None and user.can_edit_central):
        return parent.owner_id
    if parent.owner_id is None and parent.SCOPE == Scope.MASTER:
        # a personal contact or site of a central client or company
        return user.pk
    raise ChangeError("forbidden", f"{model.PARENT} not writable")


def apply(user, change, offset_ms=0, now=None, central=False):
    """
    One change of a push. Returns {"uid", "status": applied|conflict, "record"};
    raises ChangeError for a change that cannot be applied at all. A new
    master record is personal, central only when asked (the web's forms).
    """
    now = now_ms() if now is None else now
    org = user.org
    model = BY_TYPE.get(change.get("type"))
    if model is None:
        raise ChangeError("unknown_type", str(change.get("type")))
    try:
        uid = uuid.UUID(str(change.get("uid")))
    except ValueError:
        raise ChangeError("invalid", "uid") from None
    op = change.get("op", "upsert")
    with transaction.atomic():
        # the counter's lock serialises every write of the organisation
        rev = next_rev(org)
        rec = model.objects.filter(org=org, uid=uid).first()
        if op == "delete":
            if rec is None:
                return {"uid": str(uid), "status": "applied", "record": None}
            if not can_write(user, rec):
                raise ChangeError("forbidden")
            if not rec.deleted:
                _tombstone(rec, org)
            return {"uid": str(uid), "status": "applied", "record": wire.record(rec)}
        if op != "upsert":
            raise ChangeError("invalid", f"op {op!r}")
        if rec is not None and rec.deleted:
            return {"uid": str(uid), "status": "conflict", "record": wire.record(rec)}
        new = rec is None
        if new:
            rec = model(org=org, uid=uid)
        elif not can_write(user, rec):
            raise ChangeError("forbidden")
        fields = change.get("fields") or {}
        times = change.get("times") or {}
        known = wire.fields_of(model)
        refused = []
        old_parent = getattr(rec, model.PARENT + "_id") if model.PARENT and not new else None
        for name, value in fields.items():
            wf = known.get(name)
            if wf is None:
                # unknown fields are ignored (a newer app)
                continue
            if wf.field.name in model.READ_ONLY:
                refused.append(name)
                continue
            t = times.get(name, change.get("time", now))
            if isinstance(t, bool) or not isinstance(t, int):
                raise ChangeError("invalid", f"time of {name}")
            t = min(t + offset_ms, now)
            if not new and rec.clock.get(wf.field.name, -1) >= t:
                refused.append(name)
                continue
            try:
                parsed = wf.parse(value)
            except wire.WireError as e:
                raise ChangeError("invalid", str(e)) from None
            if wf.kind == "ref":
                setattr(rec, wf.field.name, _resolve(user, org, wf, parsed) if parsed else None)
            else:
                setattr(rec, wf.field.name, parsed)
            rec.clock[wf.field.name] = t
        if new:
            rec.owner_id = _owner_of_new(user, model, rec, central)
        elif model.PARENT and getattr(rec, model.PARENT + "_id") != old_parent:
            raise ChangeError("invalid", f"{model.PARENT} cannot change")
        try:
            rec.full_clean(exclude=["org", "owner", "uid"], validate_unique=False)
        except ValidationError as e:
            raise ChangeError("invalid", "; ".join(f"{k}: {' '.join(v)}" for k, v in e.message_dict.items())) from None
        rec.rev = rev
        rec.save()
        return {"uid": str(uid), "status": "conflict" if refused else "applied", "record": wire.record(rec), **({"refused": refused} if refused else {})}


def push(device, body, now=None):
    now = now_ms() if now is None else now
    changes = body.get("changes")
    if not isinstance(changes, list) or len(changes) > MAX_CHANGES:
        raise ChangeError("invalid", f"changes: a list of at most {MAX_CHANGES}")
    device_time = body.get("deviceTime")
    offset = now - device_time if isinstance(device_time, int) and not isinstance(device_time, bool) else 0
    results = []
    for change in changes:
        if not isinstance(change, dict):
            results.append({"status": "error", "error": "invalid"})
            continue
        try:
            results.append(apply(device.user, change, offset, now))
        except ChangeError as e:
            results.append({"uid": str(change.get("uid")), "status": "error", "error": e.code, "detail": e.detail})
    return {"serverTime": now_ms(), "results": results}


def pull(user, cursor, limit=500):
    org = user.org
    top = head(org)
    batch = []
    for model in SYNCED_MODELS:
        rows = model.objects.filter(org=org, rev__gt=cursor, rev__lte=top).filter(visible(user)).order_by("rev")[: limit + 1]
        batch.extend((r.rev, wire.record(r)) for r in rows)
    exits = ScopeExit.objects.filter(user=user, rev__gt=cursor, rev__lte=top).order_by("rev")[: limit + 1]
    batch.extend((e.rev, {"gone": True, "type": e.type, "uid": str(e.uid)}) for e in exits)
    batch.sort(key=lambda pair: pair[0])
    more = len(batch) > limit
    batch = batch[:limit]
    new_cursor = batch[-1][0] if more else top
    return {
        "serverTime": now_ms(),
        "cursor": max(new_cursor, cursor),
        "more": more,
        "records": [r for _, r in batch if "gone" not in r],
        "gone": [{"type": r["type"], "uid": r["uid"]} for _, r in batch if "gone" in r],
    }
