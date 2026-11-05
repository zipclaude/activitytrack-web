# SPDX-License-Identifier: GPL-3.0-or-later
"""Preparing an area: cut it out of the Protomaps basemap, as a small PMTiles file."""

import datetime
import fcntl
import os
import re
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

from django.conf import settings
from django.utils import timezone

from . import pmtiles
from .models import MapArea

USER_AGENT = "ActivityTrack-Web (+https://github.com/zipclaude/activitytrack-web)"
DAILY = "https://build.protomaps.com/%s.pmtiles"


def latest_source():
    """The newest daily build of Protomaps: today's, or one of the last days."""
    for back in range(8):
        url = DAILY % (datetime.date.today() - datetime.timedelta(days=back)).strftime("%Y%m%d")
        try:
            with urllib.request.urlopen(urllib.request.Request(url, method="HEAD", headers={"User-Agent": USER_AGENT}), timeout=30):
                return url
        except OSError:
            continue
    raise pmtiles.PMTilesError("no daily build of Protomaps found: is the server allowed to reach build.protomaps.com?")


def resolve_source():
    where = settings.MAPS_SOURCE
    return latest_source() if where == "latest" else where


def source_label(where):
    """What is kept of the source: the date of a daily build, else the file's name."""
    m = re.search(r"(\d{4})(\d{2})(\d{2})\.pmtiles$", where)
    return "-".join(m.groups()) if m else os.path.basename(where)[:200]


def log_path():
    return Path(settings.DATA_DIR) / "maps-build.log"


def start(area):
    """Queues the area and builds it in a process of its own: the request does not wait."""
    MapArea.objects.filter(pk=area.pk).update(state=MapArea.QUEUED, message="", progress=0, started=timezone.now())
    manage = Path(settings.BASE_DIR) / "manage.py"
    log = open(log_path(), "ab")
    try:
        subprocess.Popen(
            [sys.executable, str(manage), "build_maps", "--area", str(area.pk)],
            cwd=str(settings.BASE_DIR), stdin=subprocess.DEVNULL, stdout=log, stderr=log, start_new_session=True,
        )
    finally:
        log.close()


class _Lock:
    """One build at a time: the others stay queued until the lock is theirs."""

    def __enter__(self):
        path = Path(settings.DATA_DIR) / "maps-build.lock"
        path.parent.mkdir(parents=True, exist_ok=True)
        self.f = open(path, "w")
        fcntl.flock(self.f, fcntl.LOCK_EX)
        return self

    def __exit__(self, *exc):
        fcntl.flock(self.f, fcntl.LOCK_UN)
        self.f.close()


def build(pk, source=None):
    """Prepares area pk; the state, size and checksum are written to the row. Returns the area."""
    with _Lock():
        area = MapArea.objects.get(pk=pk)
        MapArea.objects.filter(pk=pk).update(state=MapArea.BUILDING, started=timezone.now(), progress=0, message="")
        directory = area.path.parent
        staging = directory / f"{area.slug}.new"
        try:
            where = source or resolve_source()
            archive = pmtiles.Archive(pmtiles.source_for(where, USER_AGENT))
            last = [0.0]

            def progress(done, total):
                now = time.monotonic()
                if now - last[0] >= 2:
                    last[0] = now
                    MapArea.objects.filter(pk=pk).update(progress=min(99, 100 * done // max(1, total)))

            directory.mkdir(parents=True, exist_ok=True)
            pmtiles.extract(archive, area.bbox, area.max_zoom, str(staging), progress=progress)
            size = staging.stat().st_size
            limit = settings.MAPS_MAX_MB * 1024 * 1024
            if size > limit:
                raise pmtiles.PMTilesError(f"la mappa pesa {size / 1e6:.1f} MB, il limite è {settings.MAPS_MAX_MB} MB: scegli un livello di zoom più basso o un'area più piccola")
            digest = pmtiles.sha256_file(staging)
            os.replace(staging, area.path)
            MapArea.objects.filter(pk=pk).update(
                state=MapArea.READY, size=size, sha256=digest, built=timezone.now(), source=source_label(where), progress=100, message="",
                built_bbox=area.bbox, built_zoom=area.max_zoom)
        except Exception as e:  # whatever went wrong is shown to the admin; the previous good file stays
            if staging.exists():
                staging.unlink()
            MapArea.objects.filter(pk=pk).update(state=MapArea.FAILED, message=str(e)[:300], progress=0)
            raise
        return MapArea.objects.get(pk=pk)


def remove_file(area):
    for p in (area.path, area.path.with_suffix(".new")):
        try:
            p.unlink()
        except FileNotFoundError:
            pass
