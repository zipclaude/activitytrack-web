# SPDX-License-Identifier: GPL-3.0-or-later
"""manage.py fetch_updates: look for new versions now, for every organisation (cron or a timer; docs/SYNC.md §12.4)."""

from django.core.management.base import BaseCommand

from accounts.models import Organization
from updates import fetch
from updates.models import UpdateSettings


class Command(BaseCommand):
    help = "Download and check the update manifest, mirror its packages, sign it for the devices."

    def add_arguments(self, parser):
        parser.add_argument("--force", action="store_true", help="also when the interval has not passed or the check is off")

    def handle(self, *args, **options):
        for org in Organization.objects.all():
            cfg = UpdateSettings.for_org(org)
            if not options["force"] and not fetch.due(cfg):
                continue
            error = fetch.run(cfg)
            self.stdout.write(f"{org}: {'refused: ' + error if error else 'ok, top version ' + (cfg.top_version or '-')}")
