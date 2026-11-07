# SPDX-License-Identifier: GPL-3.0-or-later
"""
Looking for new versions (docs/SYNC.md §12.4): the manifest at the organisation's address is
downloaded, checked and, if accepted, kept; its packages are mirrored when the admin wants.

No scheduler runs on the server: whoever finds the manifest stale (a phone's `head`, the admin page)
starts the check in a thread of its own and does not wait for it; `manage.py fetch_updates` does the
same from cron or a timer.
"""

import hashlib
import http.client
import logging
import os
import ssl
import tempfile
import threading
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone as dt_timezone
from functools import cmp_to_key
from pathlib import Path

from django.conf import settings
from django.db import connection
from django.utils import timezone

from . import jws, relay
from .models import AcceptedSigner, MirrorFile, UpdateSettings
from .versions import compare

log = logging.getLogger("updates")
# after a failed check the next one comes sooner than the interval
RETRY = timedelta(hours=1)
KEEP = 2
USER_AGENT = "ActivityTrack-Web (+https://gitlab.com/infraconsulting/activitytrack-web)"


def mirror_dir():
    return Path(settings.FILES_DIR) / "updates"


def mirror_path(sha256):
    return mirror_dir() / sha256


def trusted(org):
    """The fingerprints a manifest may be signed with: the app's key and the ones an admin accepted."""
    pins = {jws.normalize(settings.UPDATES_APP_FINGERPRINT)}
    pins.update(AcceptedSigner.objects.filter(org=org).values_list("fingerprint", flat=True))
    return pins


def _open(req, timeout):
    return urllib.request.build_opener(urllib.request.HTTPSHandler(context=ssl.create_default_context())).open(req, timeout=timeout)


def _https(url):
    """An https address with nothing a browser or a client would read otherwise (no spaces or control characters)."""
    url = str(url)
    if not url.startswith("https://") or any(c.isspace() or ord(c) < 32 for c in url):
        return False
    try:
        return urllib.parse.urlsplit(url).scheme == "https"
    except ValueError:  # "https://[bad": a malformed address is not an https one
        return False


def read(url, limit=jws.MAX_BYTES, timeout=30):
    """The text at [url] (https only, at most [limit] bytes)."""
    if not _https(url):
        raise jws.ManifestError("the manifest address must be https")
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "application/json"})
    with _open(req, timeout) as r:
        data = r.read(limit + 1)
    if len(data) > limit:
        raise jws.ManifestError("too large")
    return data.decode("utf-8")


def _when(text):
    """An ISO time as an aware UTC datetime (a time without an offset is UTC), or None."""
    try:
        when = datetime.fromisoformat(str(text).replace("Z", "+00:00"))
    except ValueError:
        return None
    return when.replace(tzinfo=dt_timezone.utc) if when.tzinfo is None else when.astimezone(dt_timezone.utc)


def _top(payload):
    versions = [str(r["version"]) for r in payload["releases"]]
    return max(versions, key=cmp_to_key(compare)) if versions else ""


def tops(payload):
    """The highest version of each channel of [payload]."""
    out = {}
    for r in payload["releases"]:
        channel, version = str(r.get("channel") or "prod"), str(r["version"])
        if channel not in out or compare(version, out[channel]) > 0:
            out[channel] = version
    return out


def validate(payload, cfg):
    """ManifestError unless [payload] is fit to replace the one accepted before."""
    issued = _when(payload.get("issued"))
    if issued is None:
        raise jws.ManifestError("no issue time")
    releases = payload.get("releases")
    if not isinstance(releases, list):
        raise jws.ManifestError("no releases")
    for r in releases:
        try:
            r["version"], r["packages"]
            for p in r["packages"]:
                if not _https(p["url"]):
                    raise jws.ManifestError(f"package address not https: {r['version']}")
                if len(jws.normalize(p["sha256"])) != 64 or not p.get("component"):
                    raise jws.ManifestError(f"package without sha256: {r['version']}")
        except (KeyError, TypeError) as e:
            raise jws.ManifestError("malformed release") from e
    before = _when(cfg.issued) if cfg.issued else None
    if before is not None and issued < before:
        raise jws.ManifestError("older than the manifest accepted before")
    now = tops(payload)
    for channel in cfg.channel_list():
        if channel in now and channel in (cfg.tops or {}) and compare(now[channel], cfg.tops[channel]) < 0:
            raise jws.ManifestError(f"lower version in the channel {channel} than the one accepted before")


def accept(cfg, text):
    """Verify [text] and keep it as the organisation's manifest; ManifestError when refused."""
    payload = jws.verify(text, trusted(cfg.org))
    validate(payload, cfg)
    for r in payload["releases"]:
        for p in r["packages"]:
            p["sha256"] = jws.normalize(p["sha256"])
        # the notes are a link on the admin page: https or nothing
        if not _https(r.get("notes_url") or ""):
            r["notes_url"] = ""
    cfg.payload, cfg.issued, cfg.top_version = payload, payload["issued"], _top(payload) or cfg.top_version
    cfg.tops = {**(cfg.tops or {}), **tops(payload)}
    return payload


def _download(url, sha256):
    """The package at [url] as the file of its sha256 in the mirror; False when it does not match."""
    if not _https(url):
        raise jws.ManifestError("package address not https")
    final = mirror_path(sha256)
    if final.exists():
        return final.stat().st_size
    final.parent.mkdir(parents=True, exist_ok=True)
    limit = settings.UPDATES_MAX_MB * 1024 * 1024
    h, size = hashlib.sha256(), 0
    fd, tmp = tempfile.mkstemp(dir=final.parent, suffix=".part")
    try:
        req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
        with os.fdopen(fd, "wb") as f, _open(req, 120) as r:
            while chunk := r.read(1024 * 1024):
                size += len(chunk)
                if size > limit:
                    raise jws.ManifestError("package too large")
                h.update(chunk)
                f.write(chunk)
        if h.hexdigest() != sha256:
            raise jws.ManifestError("sha256 does not match")
        os.chmod(tmp, 0o644)
        os.replace(tmp, final)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)
    return size


def mirror(cfg):
    """Keep the packages of the followed channels; True when every one of them is in."""
    complete = True
    channels = cfg.channel_list()
    for r in (cfg.payload or {}).get("releases", []):
        if (r.get("channel") or "prod") not in channels:
            continue
        for p in r["packages"]:
            if MirrorFile.objects.filter(org=cfg.org, version=r["version"], component=p["component"], sha256=p["sha256"]).exists():
                continue
            try:
                size = _download(p["url"], p["sha256"])
            except (OSError, http.client.HTTPException, jws.ManifestError) as e:
                complete = False
                log.warning("updates: %s %s not mirrored: %s", r["version"], p["component"], e)
                continue
            MirrorFile.objects.update_or_create(org=cfg.org, version=r["version"], component=p["component"],
                                                defaults={"sha256": p["sha256"], "size": size})
    prune(cfg.org)
    return complete


def prune(org):
    """Only the files of the newest KEEP versions stay; fewer versions than that, nothing goes."""
    versions = sorted(set(MirrorFile.objects.filter(org=org).values_list("version", flat=True)),
                      key=cmp_to_key(compare), reverse=True)
    old = versions[KEEP:]
    if not old:
        return
    gone = list(MirrorFile.objects.filter(org=org, version__in=old).values_list("sha256", flat=True))
    MirrorFile.objects.filter(org=org, version__in=old).delete()
    for sha in set(gone):
        if not MirrorFile.objects.filter(sha256=sha).exists():
            mirror_path(sha).unlink(missing_ok=True)


def run(cfg):
    """Check now: fetch, accept, mirror, sign for the phones. The error text, or "" when all went well."""
    now = timezone.now()
    cfg.last_checked, error = now, ""
    try:
        accept(cfg, read(cfg.manifest_url))
        cfg.last_ok = now
        log.info("updates: manifest of %s accepted (issued %s, top %s)", cfg.org, cfg.issued, cfg.top_version)
    except (OSError, http.client.HTTPException, ValueError, jws.ManifestError) as e:
        error = str(e)[:300] or e.__class__.__name__
        log.warning("updates: manifest of %s refused: %s", cfg.org, error)
    except Exception as e:
        # whatever else: the check must not fail without a word, or the page and the retry would never know
        error = f"{e.__class__.__name__}: {e}"[:300]
        log.exception("updates: the check of %s failed", cfg.org)
    cfg.last_error = error
    cfg.save()
    try:
        if not error and cfg.mirror:
            mirror(cfg)
        if cfg.payload:
            relay.rebuild(cfg)
    except Exception as e:
        cfg.last_error = error = f"{e.__class__.__name__}: {e}"[:300]
        cfg.save(update_fields=["last_error"])
        log.exception("updates: the packages of %s could not be kept", cfg.org)
    return error


def reset(cfg):
    """Forget what was accepted before (the highest versions, the issue time): the next manifest is taken as the
    first one. For an upstream that withdrew a release or went back on purpose."""
    cfg.issued, cfg.top_version, cfg.tops = "", "", {}
    cfg.save(update_fields=["issued", "top_version", "tops"])


def due(cfg, now=None):
    now = now or timezone.now()
    if not cfg.enabled or not cfg.poll_hours or not cfg.manifest_url:
        return False
    if cfg.last_checked is None:
        return True
    return now - cfg.last_checked >= (RETRY if cfg.last_error else timedelta(hours=cfg.poll_hours))


def _background(pk):
    try:
        run(UpdateSettings.objects.get(pk=pk))
    except Exception:
        log.exception("updates: the check failed")
    finally:
        connection.close()


def poll_if_due(cfg):
    """Start the check in a thread when the manifest is stale; never waits for it. One request wins the claim."""
    if not due(cfg):
        return False
    claimed = UpdateSettings.objects.filter(pk=cfg.pk, last_checked=cfg.last_checked).update(last_checked=timezone.now())
    if not claimed:
        return False
    start(cfg)
    return True


def start(cfg):
    """Run the check in a thread of its own (in the tests: here and now)."""
    if settings.UPDATES_BACKGROUND:
        threading.Thread(target=_background, args=(cfg.pk,), daemon=True).start()
    else:
        run(cfg)
