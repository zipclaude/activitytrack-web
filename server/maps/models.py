# SPDX-License-Identifier: GPL-3.0-or-later
"""The areas an admin prepares as offline maps for the phones (docs/OFFLINE-MAPS.md)."""

import re
from datetime import timedelta

from django.conf import settings
from django.db import models
from django.utils import timezone
from django.utils.translation import gettext_lazy as _

from . import tiles

SLUG = re.compile(r"[a-z0-9][a-z0-9-]{0,63}")
# a build that has not finished by then was interrupted (the server restarted, the process died)
STALE = timedelta(minutes=45)


class MapArea(models.Model):
    QUEUED, BUILDING, READY, FAILED = "queued", "building", "ready", "failed"
    STATES = [(QUEUED, _("in coda")), (BUILDING, _("in preparazione")), (READY, _("pronta")), (FAILED, _("non riuscita"))]

    org = models.ForeignKey("accounts.Organization", on_delete=models.CASCADE, related_name="map_areas")
    # the name of the file and the id on the wire: lower case, digits and dashes
    slug = models.CharField(max_length=64)
    name = models.CharField(_("nome"), max_length=80)
    lat = models.FloatField(_("latitudine"))
    lon = models.FloatField(_("longitudine"))
    radius_km = models.PositiveIntegerField(_("raggio (km)"))
    max_zoom = models.PositiveSmallIntegerField(_("livello di zoom"))
    state = models.CharField(max_length=16, choices=STATES, default=QUEUED)
    message = models.CharField(max_length=300, blank=True)
    # what the last good build made
    size = models.PositiveBigIntegerField(default=0)
    sha256 = models.CharField(max_length=64, blank=True)
    built = models.DateTimeField(null=True, blank=True)
    # the box and zoom the file was really built with: editing the area does not change the file
    built_bbox = models.JSONField(default=list, blank=True)
    built_zoom = models.PositiveSmallIntegerField(default=0)
    source = models.CharField(max_length=200, blank=True)
    # a build in progress: when it began and how far it is (0-100)
    started = models.DateTimeField(null=True, blank=True)
    progress = models.PositiveSmallIntegerField(default=0)
    created = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["name"]
        constraints = [models.UniqueConstraint(fields=["org", "slug"], name="one_map_area_per_slug")]

    def __str__(self):
        return self.name

    @property
    def bbox(self):
        return tiles.bbox(self.lat, self.lon, self.radius_km)

    @property
    def file_name(self):
        return f"{self.slug}.pmtiles"

    @property
    def path(self):
        return settings.FILES_DIR / str(self.org_id) / "maps" / self.file_name

    @property
    def scale(self):
        return tiles.SCALES.get(self.max_zoom)

    @property
    def busy(self):
        return self.state in (self.QUEUED, self.BUILDING) and not self.stale

    @property
    def stale(self):
        return self.state in (self.QUEUED, self.BUILDING) and (
            (self.started or self.created) < timezone.now() - STALE)

    @property
    def usable(self):
        """What the phones are offered: a good file, whatever a later rebuild is doing."""
        return bool(self.sha256) and self.size > 0
