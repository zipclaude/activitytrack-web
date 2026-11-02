# SPDX-License-Identifier: GPL-3.0-or-later
"""
Repairs JSON fields stored before the wire checked their shape (sync/wire.py
JSON_SHAPES): a tag list that is not one broke the pages of whoever read the
entry. The fixed records get a new revision, so phones receive them.
"""

from django.core.management.base import BaseCommand
from django.db import models, transaction

from records.models import SYNCED_MODELS
from sync import engine, wire


def _repaired(field, value):
    """The value in its field's shape, keeping what can be kept."""
    shape = wire.JSON_SHAPES[field.name]
    if field.null and value is None:
        return None
    if isinstance(value, str) and shape in (wire._strings, wire._tags):
        value = value.replace(",", " ").split() if shape is wire._tags else [value]
    if isinstance(value, list) and shape in (wire._strings, wire._tags):
        value = [x for x in value if isinstance(x, str)]
    try:
        return shape(field.name, value)
    except wire.WireError:
        return field.get_default()


class Command(BaseCommand):
    help = "Repairs JSON fields of the synced records (tags, phones, plugin fields) that have the wrong shape."

    def add_arguments(self, parser):
        parser.add_argument("--dry-run", action="store_true", help="only list what would change")

    def handle(self, *args, dry_run, **options):
        fixed = 0
        for model in SYNCED_MODELS:
            fields = [f for f in model._meta.concrete_fields if isinstance(f, models.JSONField) and f.name not in wire.BOOKKEEPING]
            for rec in model.objects.filter(deleted=False).iterator() if fields else ():
                changed = {}
                for f in fields:
                    good = _repaired(f, getattr(rec, f.name))
                    if good != getattr(rec, f.name):
                        changed[f.name] = good
                if not changed:
                    continue
                fixed += 1
                self.stdout.write(f"{model.SYNC_TYPE} {rec.uid}: " + ", ".join(f"{k} {getattr(rec, k)!r} -> {v!r}" for k, v in changed.items()))
                if dry_run:
                    continue
                with transaction.atomic():
                    now = engine.now_ms()
                    for k, v in changed.items():
                        setattr(rec, k, v)
                        # the repair wins over the stored change it replaces
                        rec.clock[k] = now
                    rec.rev = engine.next_rev(rec.org)
                    rec.save(update_fields=[*changed, "clock", "rev"])
        self.stdout.write(f"{fixed} record(s) {'to repair' if dry_run else 'repaired'}.")
