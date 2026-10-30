# SPDX-License-Identifier: GPL-3.0-or-later
from urllib.parse import urlencode

import segno
from django.conf import settings
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.http import Http404, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.utils.translation import gettext as _
from django.views.decorators.http import require_POST

from accounts.models import Device, PairingCode


def pair_link(code):
    """The QR code's content (docs/SYNC.md §9)."""
    query = {"v": "1", "u": settings.BASE_URL, "c": code}
    if settings.TLS_PIN:
        query["f"] = settings.TLS_PIN
    return "activitytrack://pair?" + urlencode(query)


@login_required
def phones(request):
    if not request.user.syncs:
        raise Http404
    return render(request, "web/phones.html", {"devices": request.user.devices.order_by("-created")})


@login_required
@require_POST
def pair_new(request):
    if not request.user.syncs:
        raise Http404
    pairing, code = PairingCode.issue(request.user)
    link = pair_link(code)
    qr = segno.make(link, error="m").svg_inline(scale=6, dark="#000", light="#fff")
    return render(request, "web/pair.html", {"pairing": pairing, "link": link, "qr": qr})


@login_required
def pair_status(request, pk):
    pairing = get_object_or_404(PairingCode, pk=pk, user=request.user)
    if pairing.device:
        return JsonResponse({"state": "paired", "device": pairing.device.name})
    if pairing.expires <= timezone.now():
        return JsonResponse({"state": "expired"})
    return JsonResponse({"state": "waiting"})


@login_required
@require_POST
def device_holder(request, pk):
    """This phone becomes the one of its user that sends to the management systems."""
    devices = Device.objects.filter(user__org=request.user.org) if request.user.is_admin else request.user.devices
    device = get_object_or_404(devices, pk=pk, revoked__isnull=True)
    Device.objects.filter(user=device.user).update(plugin_holder=False)
    Device.objects.filter(pk=device.pk).update(plugin_holder=True)
    messages.success(request, _("«%(name)s» invia ora ai gestionali.") % {"name": device.name})
    return redirect("phones")


@login_required
@require_POST
def device_revoke(request, pk):
    devices = Device.objects.filter(user__org=request.user.org) if request.user.is_admin else request.user.devices
    device = get_object_or_404(devices, pk=pk, revoked__isnull=True)
    device.revoked = timezone.now()
    device.plugin_holder = False
    device.save(update_fields=["revoked", "plugin_holder"])
    messages.success(request, _("Associazione di «%(name)s» revocata.") % {"name": device.name})
    back = request.POST.get("next", "")
    # from a user's sheet: back there
    return redirect(back if back.startswith("/") and not back.startswith("//") else "phones")
