# SPDX-License-Identifier: GPL-3.0-or-later
"""Records as they travel (docs/SYNC.md §1): wire names and value conversions."""

import uuid
from datetime import date, datetime, timezone
from functools import cache

from django.db import models

BOOKKEEPING = {"uid", "org", "owner", "rev", "deleted", "clock"}


class WireError(ValueError):
    pass


def _camel(name):
    head, *rest = name.split("_")
    return head + "".join(p.capitalize() for p in rest)


class WireField:
    __slots__ = ("field", "name", "kind")

    def __init__(self, field):
        self.field = field
        if field.is_relation:
            self.kind = "ref"
            self.name = _camel(field.name) + "Uid"
        elif isinstance(field, models.DateTimeField):
            # *_utc -> *UtcMs, as the app's properties
            self.kind = "instant"
            self.name = _camel(field.name) + "Ms"
        elif isinstance(field, models.DateField):
            self.kind = "date"
            self.name = _camel(field.name)
        elif isinstance(field, models.JSONField):
            self.kind = "json"
            self.name = _camel(field.name)
        elif isinstance(field, models.BooleanField):
            self.kind = "bool"
            self.name = _camel(field.name)
        elif isinstance(field, (models.IntegerField, models.BigIntegerField)):
            self.kind = "int"
            self.name = _camel(field.name)
        elif isinstance(field, models.FloatField):
            self.kind = "float"
            self.name = _camel(field.name)
        else:
            self.kind = "text"
            self.name = _camel(field.name)

    def get(self, obj):
        if self.kind == "ref":
            value = getattr(obj, self.field.attname)
            return str(value) if value is not None else None
        value = getattr(obj, self.field.name)
        if value is None:
            return None
        if self.kind == "instant":
            return int(value.timestamp() * 1000)
        if self.kind == "date":
            return value.isoformat()
        return value

    def parse(self, value):
        """The model value of a wire value; refs come back as UUIDs, resolved by the caller."""
        if value is None:
            if not self.field.null:
                raise WireError(f"{self.name}: null not allowed")
            return None
        try:
            if self.kind == "ref":
                return uuid.UUID(str(value))
            if self.kind == "instant":
                if isinstance(value, bool) or not isinstance(value, int):
                    raise TypeError
                return datetime.fromtimestamp(value / 1000, tz=timezone.utc)
            if self.kind == "date":
                return date.fromisoformat(value)
            if self.kind == "bool":
                if not isinstance(value, bool):
                    raise TypeError
                return value
            if self.kind == "int":
                if isinstance(value, bool) or not isinstance(value, int):
                    raise TypeError
                return value
            if self.kind == "float":
                if isinstance(value, bool) or not isinstance(value, (int, float)):
                    raise TypeError
                return float(value)
            if self.kind == "text":
                if not isinstance(value, str):
                    raise TypeError
                if self.field.max_length and len(value) > self.field.max_length:
                    raise WireError(f"{self.name}: longer than {self.field.max_length}")
                return value
            return value
        except (TypeError, ValueError) as e:
            if isinstance(e, WireError):
                raise
            raise WireError(f"{self.name}: bad value {value!r}") from None


@cache
def fields_of(model):
    """Wire name -> WireField, in model order."""
    return {wf.name: wf for wf in (WireField(f) for f in model._meta.concrete_fields if f.name not in BOOKKEEPING)}


def record(obj):
    out = {
        "type": obj.SYNC_TYPE,
        "uid": str(obj.uid),
        "rev": obj.rev,
        "deleted": obj.deleted,
        "central": obj.owner_id is None,
    }
    if not obj.deleted:
        out["fields"] = {name: wf.get(obj) for name, wf in fields_of(type(obj)).items()}
    return out


def schema():
    """The wire schema, for the app's side (manage.py sync_schema)."""
    from records.models import SYNCED_MODELS

    return [
        {
            "type": m.SYNC_TYPE,
            "scope": str(m.SCOPE),
            "parent": m.PARENT,
            "fields": [
                {"name": n, "kind": wf.kind, "null": wf.field.null}
                | ({"ref": wf.field.related_model.SYNC_TYPE} if wf.kind == "ref" else {})
                | ({"readOnly": True} if wf.field.name in m.READ_ONLY else {})
                for n, wf in fields_of(m).items()
            ],
        }
        for m in SYNCED_MODELS
    ]
