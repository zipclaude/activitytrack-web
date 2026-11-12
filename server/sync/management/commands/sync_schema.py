# SPDX-License-Identifier: GPL-3.0-or-later
import json

from django.core.management.base import BaseCommand

from sync import compat, wire


class Command(BaseCommand):
    help = "Prints the wire schema of the synced records (docs/SYNC.md §1), for the app's side."

    def handle(self, *args, **options):
        # version: of this document's format; schema: the wire schema it describes (sync/compat.py)
        self.stdout.write(json.dumps({"version": 1, "schema": compat.SCHEMA, "types": wire.schema()}, indent=2))
