# SPDX-License-Identifier: GPL-3.0-or-later
"""The offline maps page (docs/OFFLINE-MAPS.md): an admin chooses areas and zoom, the server prepares them."""

import re

from django.conf import settings
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied
from django.http import Http404, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils.text import slugify
from django.utils.translation import gettext as _
from django.views.decorators.http import require_GET, require_POST

from . import build, tiles
from .models import SLUG, MapArea
from .presets import PRESETS

MIN_ZOOM = 6
MIN_RADIUS_KM = 5
DEFAULT_RADIUS_KM = 100
DEFAULT_ZOOM = 10
# the estimate is rough (±15 % on the measures it comes from)
SAFETY = 1.3


def _admin(request):
    if not request.user.is_admin:
        raise PermissionDenied
    return request.user


def zoom_choices():
    return [(z, z, tiles.SCALES.get(z)) for z in range(8, settings.MAPS_MAX_ZOOM + 1)]


def _free_slug(org, base, keep=None):
    base = (slugify(base)[:56] or "mappa")
    slug, n = base, 2
    while MapArea.objects.filter(org=org, slug=slug).exclude(pk=keep).exists():
        slug, n = f"{base}-{n}", n + 1
    return slug


def _float(post, key):
    try:
        return float(str(post.get(key, "")).replace(",", "."))
    except ValueError:
        return None


def _parse(post):
    """(values, errors) of the area form."""
    v, errors = {}, {}
    v["name"] = post.get("name", "").strip()
    if not v["name"] or len(v["name"]) > 80:
        errors["name"] = _("Il nome è obbligatorio (al massimo 80 caratteri).")
    v["lat"], v["lon"] = _float(post, "lat"), _float(post, "lon")
    if v["lat"] is None or not -85 <= v["lat"] <= 85:
        errors["lat"] = _("La latitudine va da -85 a 85.")
    if v["lon"] is None or not -180 <= v["lon"] <= 180:
        errors["lon"] = _("La longitudine va da -180 a 180.")
    try:
        v["radius_km"] = int(post.get("radius_km", ""))
    except ValueError:
        v["radius_km"] = None
    if v["radius_km"] is None or not MIN_RADIUS_KM <= v["radius_km"] <= settings.MAPS_MAX_RADIUS_KM:
        errors["radius_km"] = _("Il raggio va da %(a)s a %(b)s km.") % {"a": MIN_RADIUS_KM, "b": settings.MAPS_MAX_RADIUS_KM}
    try:
        v["max_zoom"] = int(post.get("max_zoom", ""))
    except ValueError:
        v["max_zoom"] = None
    if v["max_zoom"] is None or not 8 <= v["max_zoom"] <= settings.MAPS_MAX_ZOOM:
        errors["max_zoom"] = _("Il livello di zoom va da 8 a %(b)s.") % {"b": settings.MAPS_MAX_ZOOM}
    if not errors:
        _n, size = tiles.estimate(v["lat"], v["lon"], v["radius_km"], v["max_zoom"])
        if size * SAFETY > settings.MAPS_MAX_MB * 1024 * 1024:
            errors["max_zoom"] = _("Con questo raggio e questo zoom la mappa supererebbe i %(m)s MB: abbassa lo zoom o riduci l'area.") % {"m": settings.MAPS_MAX_MB}
    return v, errors


@login_required
def index(request):
    org = _admin(request).org
    areas = list(MapArea.objects.filter(org=org))
    taken = {a.slug for a in areas}
    return render(request, "maps/index.html", {
        "org": org, "areas": areas, "presets": [p for p in PRESETS if p[0] not in taken],
        "source": settings.MAPS_SOURCE, "max_mb": settings.MAPS_MAX_MB,
        "building": any(a.busy for a in areas),
    })


@login_required
@require_POST
def toggle(request):
    org = _admin(request).org
    org.offline_maps = request.POST.get("offline_maps") == "on"
    org.save(update_fields=["offline_maps"])
    messages.success(request, _("I telefoni ricevono le mappe offline.") if org.offline_maps else _("I telefoni non ricevono più le mappe offline."))
    return redirect("maps:index")


@login_required
def edit(request, pk=None):
    org = _admin(request).org
    area = get_object_or_404(MapArea, pk=pk, org=org) if pk else None
    form, errors = {}, {}
    if request.method == "POST":
        form, errors = _parse(request.POST)
        if not errors:
            moved = area is None or any(getattr(area, k) != form[k] for k in ("lat", "lon", "radius_km", "max_zoom"))
            if area is None:
                area = MapArea(org=org, slug=_free_slug(org, request.POST.get("slug") or form["name"]))
            for k, val in form.items():
                setattr(area, k, val)
            area.save()
            if moved:
                build.start(area)
                messages.success(request, _("Area salvata: la preparazione è iniziata."))
            else:
                messages.success(request, _("Area salvata."))
            return redirect("maps:index")
    elif area:
        form = {k: getattr(area, k) for k in ("name", "lat", "lon", "radius_km", "max_zoom")}
    else:
        preset = next((p for p in PRESETS if p[0] == request.GET.get("preset")), None)
        form = {"name": preset[1], "lat": preset[2], "lon": preset[3]} if preset else {}
        form.update(radius_km=DEFAULT_RADIUS_KM, max_zoom=DEFAULT_ZOOM)
        if preset:
            form["slug"] = preset[0]
    return render(request, "maps/edit.html", {
        "area": area, "form": form, "errors": errors, "zooms": zoom_choices(),
        "max_radius": settings.MAPS_MAX_RADIUS_KM, "min_radius": MIN_RADIUS_KM, "max_mb": settings.MAPS_MAX_MB,
    })


@login_required
@require_POST
def rebuild(request, pk):
    area = get_object_or_404(MapArea, pk=pk, org=_admin(request).org)
    if area.busy:
        messages.info(request, _("L'area è già in preparazione."))
    else:
        build.start(area)
        messages.success(request, _("La preparazione è iniziata."))
    return redirect("maps:index")


@login_required
@require_POST
def delete(request, pk):
    area = get_object_or_404(MapArea, pk=pk, org=_admin(request).org)
    build.remove_file(area)
    area.delete()
    messages.success(request, _("Area eliminata: i telefoni la tolgono alla prossima sincronizzazione."))
    return redirect("maps:index")


@login_required
@require_GET
def status(request):
    """For the page's polling: where each area is."""
    areas = MapArea.objects.filter(org=_admin(request).org)
    return JsonResponse({"areas": [{
        "id": a.pk, "state": "stale" if a.stale else a.state, "progress": a.progress, "size": a.size,
        "message": a.message, "built": a.built.isoformat() if a.built else "",
    } for a in areas], "busy": any(a.busy for a in areas)})


@login_required
@require_GET
def estimate(request):
    """Tiles and bytes of an area, as the form is filled in."""
    _admin(request)
    lat, lon = _float(request.GET, "lat"), _float(request.GET, "lon")
    try:
        radius, zoom = int(request.GET.get("radius_km", "")), int(request.GET.get("max_zoom", ""))
    except ValueError:
        radius = zoom = None
    if None in (lat, lon, radius, zoom) or not (-85 <= lat <= 85 and -180 <= lon <= 180 and 1 <= radius <= 2000 and 0 <= zoom <= 14):
        return JsonResponse({"error": "invalid"}, status=400)
    n, size = tiles.estimate(lat, lon, radius, zoom)
    return JsonResponse({
        "tiles": n, "bytes": size, "scale": tiles.SCALES.get(zoom),
        "too_big": size * SAFETY > settings.MAPS_MAX_MB * 1024 * 1024,
    })
