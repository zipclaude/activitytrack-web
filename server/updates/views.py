# SPDX-License-Identifier: GPL-3.0-or-later
"""The Aggiornamenti page: where the server looks for new versions, whom it trusts, the packages it keeps."""

import logging
from functools import cmp_to_key

from django.conf import settings
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied
from django.shortcuts import get_object_or_404, redirect, render
from django.utils.translation import gettext as _
from django.views.decorators.http import require_POST

from atweb import __version__

from . import distro, fetch, jws, keys, relay
from .models import AcceptedSigner, MirrorFile, UpdateSettings
from .versions import compare

log = logging.getLogger("updates")
CHANNELS = ("prod", "beta", "test", "dev")


def _admin(request):
    if not request.user.is_admin:
        raise PermissionDenied
    return request.user


def newer_server(cfg):
    """The newest release of the followed channels, newer than this server, with a package for its own
    distribution (`web-deb13`, …; /etc/os-release). On a distribution we have no package for, any `web-` package
    counts: the page then shows the version only."""
    own = distro.component()
    best = None
    for r in (cfg.payload or {}).get("releases", []):
        if (r.get("channel") or "prod") not in cfg.channel_list() or compare(r["version"], __version__) <= 0:
            continue
        if not any(p["component"] == own if own else p["component"].startswith("web-") for p in r["packages"]):
            continue
        if best is None or compare(r["version"], best["version"]) > 0:
            best = r
    return best


@login_required
def index(request):
    org = _admin(request).org
    cfg = UpdateSettings.for_org(org)
    fetch.poll_if_due(cfg)
    versions = sorted(set(MirrorFile.objects.filter(org=org).values_list("version", flat=True)), key=cmp_to_key(compare), reverse=True)
    try:
        server_fp = keys.fingerprint()
    except Exception:
        log.exception("updates: the signing key cannot be read")
        server_fp = ""
    return render(request, "updates/index.html", {
        "cfg": cfg, "channels": [(c, c in cfg.channel_list()) for c in CHANNELS],
        "server_fp": jws.display(server_fp), "app_fp": jws.display(settings.UPDATES_APP_FINGERPRINT),
        "signers": [(s, jws.display(s.fingerprint)) for s in AcceptedSigner.objects.filter(org=org)],
        "mirrored": versions, "newer": newer_server(cfg), "version": __version__,
        "mirror_size": sum(MirrorFile.objects.filter(org=org).values_list("size", flat=True)),
    })


@login_required
@require_POST
def save(request):
    user = _admin(request)
    cfg = UpdateSettings.for_org(user.org)
    url = request.POST.get("manifest_url", "").strip()
    if url and not url.startswith("https://"):
        messages.error(request, _("L'indirizzo del manifesto deve iniziare con https://"))
        return redirect("updates:index")
    try:
        hours = int(request.POST.get("poll_hours", "12"))
    except ValueError:
        hours = -1
    if not 0 <= hours <= 24 * 30:
        messages.error(request, _("L'intervallo va da 0 (mai) a 720 ore."))
        return redirect("updates:index")
    cfg.manifest_url, cfg.poll_hours = url, hours
    cfg.channels = ",".join(c for c in CHANNELS if c in request.POST.getlist("channels")) or "prod"
    cfg.enabled = request.POST.get("enabled") == "on"
    cfg.mirror = request.POST.get("mirror") == "on"
    cfg.save()
    relay.rebuild(cfg)
    log.info("updates: %s saved the settings of %s: url=%s channels=%s every=%sh enabled=%s mirror=%s",
             user, user.org, cfg.manifest_url, cfg.channels, cfg.poll_hours, cfg.enabled, cfg.mirror)
    messages.success(request, _("Impostazioni salvate."))
    return redirect("updates:index")


@login_required
@require_POST
def check(request):
    cfg = UpdateSettings.for_org(_admin(request).org)
    fetch.start(cfg)
    messages.info(request, _("Controllo avviato: ricarica la pagina fra qualche istante."))
    return redirect("updates:index")


@login_required
@require_POST
def signer_add(request):
    user = _admin(request)
    fp = jws.normalize(request.POST.get("fingerprint", ""))
    if len(fp) != 64:
        messages.error(request, _("L'impronta è un SHA-256: 64 cifre esadecimali (i due punti e gli spazi non contano)."))
        return redirect("updates:index")
    _signer, made = AcceptedSigner.objects.get_or_create(
        org=user.org, fingerprint=fp, defaults={"label": request.POST.get("label", "").strip()[:80], "added_by": user})
    if not made:
        messages.info(request, _("Questa chiave è già accettata."))
        return redirect("updates:index")
    log.info("updates: %s accepted the signer %s for %s", user, fp, user.org)
    messages.success(request, _("Chiave accettata."))
    return redirect("updates:index")


@login_required
@require_POST
def signer_remove(request, pk):
    user = _admin(request)
    signer = get_object_or_404(AcceptedSigner, pk=pk, org=user.org)
    log.info("updates: %s removed the signer %s of %s", user, signer.fingerprint, user.org)
    signer.delete()
    messages.success(request, _("Chiave tolta."))
    return redirect("updates:index")
