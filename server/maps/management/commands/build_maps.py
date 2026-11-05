# SPDX-License-Identifier: GPL-3.0-or-later
"""Prepares offline maps: one area (the web page starts this), or all that are old (a timer)."""

from datetime import timedelta

from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

from maps import build
from maps.models import MapArea


class Command(BaseCommand):
    help = "Prepare the offline maps of the areas chosen in the web page."

    def add_arguments(self, parser):
        parser.add_argument("--area", type=int, help="the id of one area")
        parser.add_argument("--all", action="store_true", help="every area")
        parser.add_argument("--older-than", type=int, metavar="DAYS", help="the areas built more than DAYS days ago, or never")
        parser.add_argument("--source", help="a PMTiles file or URL instead of [maps] source")

    def handle(self, *args, **o):
        if o["area"]:
            areas = MapArea.objects.filter(pk=o["area"])
        elif o["all"]:
            areas = MapArea.objects.all()
        elif o["older_than"] is not None:
            cutoff = timezone.now() - timedelta(days=o["older_than"])
            areas = [a for a in MapArea.objects.filter(org__offline_maps=True) if a.built is None or a.built < cutoff]
        else:
            raise CommandError("give --area, --all or --older-than")
        failed = 0
        for a in areas:
            self.stdout.write(f"{a.org_id}/{a.slug}: z{a.max_zoom}, {a.radius_km} km")
            try:
                done = build.build(a.pk, o["source"])
                self.stdout.write(self.style.SUCCESS(f"  ready: {done.size / 1e6:.2f} MB"))
            except Exception as e:  # recorded on the area by build(); the others go on
                failed += 1
                self.stderr.write(f"  failed: {e}")
        if failed:
            raise CommandError(f"{failed} area(s) failed")
