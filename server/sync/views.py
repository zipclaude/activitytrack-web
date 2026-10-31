# SPDX-License-Identifier: GPL-3.0-or-later
"""The phones' API (docs/SYNC.md §5, §9): JSON, bearer device tokens."""

import hashlib
import json
import os
import tempfile
import uuid
from functools import wraps
from pathlib import Path

from django.conf import settings
from django.db import transaction
from django.http import FileResponse, HttpResponse, JsonResponse
from django.utils import timezone
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_GET, require_http_methods, require_POST

from accounts.models import Device, PairingCode
from records.models import Attachment

from . import compat, engine, wire


def _error(status, code, detail=""):
    return JsonResponse({"error": code, "detail": detail}, status=status)


def device_api(view):
    @csrf_exempt
    @wraps(view)
    def wrapped(request, *args, **kwargs):
        auth = request.headers.get("Authorization", "")
        if not auth.startswith("Bearer "):
            return _error(401, "no_token")
        device = Device.objects.by_token(auth[7:].strip())
        if device is None:
            return _error(401, "revoked")
        if not device.user.is_active or not device.user.syncs:
            return _error(403, "user_disabled")
        version, schema = compat.client(request)
        refused = compat.refusal(schema)
        if refused:
            return _incompatible(refused)
        now = timezone.now()
        seen = {}
        if device.last_seen is None or (now - device.last_seen).total_seconds() > 60:
            seen["last_seen"] = now
        # the app's version as it is now, not as it was at pairing
        if version and version != device.app_version:
            seen["app_version"] = version
        if schema != device.schema:
            seen["schema"] = schema
        if seen:
            Device.objects.filter(pk=device.pk).update(**seen)
        request.device = device
        request.schema = schema
        return view(request, *args, **kwargs)

    return wrapped


def _incompatible(code):
    """426: outside the compatibility window (docs/SYNC.md §12); the app tells what to update."""
    return JsonResponse({"error": code, "detail": "", **compat.describe()}, status=426)


def _json(request):
    try:
        body = json.loads(request.body or b"{}")
    except ValueError:
        return None
    return body if isinstance(body, dict) else None


@csrf_exempt
@require_POST
def pair(request):
    body = _json(request)
    if body is None or not isinstance(body.get("code"), str):
        return _error(400, "invalid")
    version, schema = compat.client(request)
    refused = compat.refusal(schema)
    if refused:
        return _incompatible(refused)
    with transaction.atomic():
        pairing = PairingCode.redeem(body["code"])
        if pairing is None:
            return _error(400, "invalid_code")
        user = pairing.user
        if not user.is_active or not user.syncs:
            return _error(403, "user_disabled")
        device, token = Device.create_for(
            user,
            name=str(body.get("deviceName") or "Telefono")[:100],
            model=str(body.get("model") or "")[:100],
            app_version=str(body.get("appVersion") or version)[:40],
            schema=schema,
        )
        pairing.device = device
        pairing.save(update_fields=["device"])
    return JsonResponse(
        {
            "deviceId": str(device.pk),
            "token": token,
            "user": {"username": user.username, "name": user.get_full_name(), "profile": user.profile},
            "org": {"name": user.org.name},
            "pluginHolder": device.plugin_holder,
            "serverTime": engine.now_ms(),
            **compat.describe(),
        }
    )


@device_api
@require_POST
def push(request):
    body = _json(request)
    if body is None:
        return _error(400, "invalid")
    if isinstance(body.get("changes"), list):
        body["changes"] = [compat.from_client(c, request.schema) if isinstance(c, dict) else c for c in body["changes"]]
    try:
        result = engine.push(request.device, body)
    except engine.ChangeError as e:
        return _error(400, e.code, e.detail)
    for r in result["results"]:
        if r.get("record"):
            r["record"] = compat.to_client(r["record"], request.schema)
    return JsonResponse(result)


@device_api
@require_GET
def pull(request):
    try:
        cursor = int(request.GET.get("cursor", "0"))
        limit = min(max(int(request.GET.get("limit", "500")), 1), 500)
    except ValueError:
        return _error(400, "invalid")
    result = engine.pull(request.device.user, cursor, limit)
    result["records"] = [compat.to_client(r, request.schema) for r in result["records"]]
    if not result["more"]:
        Device.objects.filter(pk=request.device.pk).update(cursor=result["cursor"])
    return JsonResponse(result)


@device_api
@require_GET
def policy(request):
    """The phone's policy (docs/SYNC.md §8): text and optional signature, or nulls."""
    from policies.effective import effective, fingerprint, sign

    text = effective(request.device.user)
    signature = sign(text) if text else None
    return JsonResponse({"policy": text, "signature": signature, "fingerprint": fingerprint(text, signature) if text else None})


@device_api
@require_GET
def head(request):
    return JsonResponse({
        "rev": engine.head(request.device.user.org),
        "serverTime": engine.now_ms(),
        # attachments larger than this are refused (413): the phone does not try
        "filesMaxMb": settings.FILES_MAX_MB,
        # this phone sends to the management systems (ARCHITECTURE.md §8)
        "pluginHolder": request.device.plugin_holder,
        **compat.describe(),
    })


def file_path(att):
    s = str(att.uid)
    return Path(settings.FILES_DIR) / str(att.org_id) / s[:2] / s


def _attachment(user, uid):
    try:
        uid = uuid.UUID(uid)
    except ValueError:
        return None
    return Attachment.objects.filter(org=user.org, uid=uid, deleted=False).filter(engine.visible(user)).first()


@device_api
@require_http_methods(["GET", "PUT"])
def files(request, uid):
    user = request.device.user
    att = _attachment(user, uid)
    if att is None:
        return _error(404, "unknown")
    path = file_path(att)
    if request.method == "GET":
        if not att.has_file or not path.exists():
            return _error(404, "no_file")
        return serve_file(att, path)
    if not engine.can_write(user, att):
        return _error(403, "forbidden")
    # streamed to a temporary file: request.body would stop at Django's 2.5 MB
    limit = settings.FILES_MAX_MB * 1024 * 1024
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent)
    sha, size = hashlib.sha256(), 0
    try:
        with os.fdopen(fd, "wb") as f:
            while chunk := request.read(1024 * 1024):
                size += len(chunk)
                if size > limit:
                    raise ValueError("too_large")
                sha.update(chunk)
                f.write(chunk)
        digest = sha.hexdigest()
        claimed = request.headers.get("X-Sha256", "").lower()
        if (claimed and claimed != digest) or (att.sha256 and att.sha256 != digest):
            raise ValueError("sha256_mismatch")
    except ValueError as e:
        os.unlink(tmp)
        return _error(413, "too_large") if str(e) == "too_large" else _error(400, "sha256_mismatch")
    os.replace(tmp, path)
    with transaction.atomic():
        rev = engine.next_rev(user.org)
        Attachment.objects.filter(pk=att.pk).update(has_file=True, sha256=digest, size=size, rev=rev)
    att.refresh_from_db()
    return JsonResponse({"record": compat.to_client(wire.record(att), request.schema)})


# shown in the browser; anything else is downloaded: the type comes from the
# phone, and an HTML or SVG attachment shown here would run its scripts on
# this site in the reader's session
INLINE = {"image/jpeg", "image/png", "image/webp", "image/gif", "image/heic", "image/heif", "application/pdf"}


def _guarded(response, att):
    mime = (att.mime or "").split(";")[0].strip().lower()
    inline = mime in INLINE or mime.startswith(("audio/", "video/"))
    response["Content-Disposition"] = ("inline" if inline else "attachment") + f'; filename="{att.uid}"'
    if mime != "application/pdf":
        # no scripts, no plugins, even if a browser renders it anyway
        response["Content-Security-Policy"] = "sandbox; default-src 'none'; img-src 'self'; media-src 'self'"
    return response


def serve_file(att, path):
    if settings.FILES_OFFLOAD == "nginx":
        response = HttpResponse(content_type=att.mime)
        response["X-Accel-Redirect"] = f"{settings.FILES_ACCEL_LOCATION.rstrip('/')}/{path.relative_to(settings.FILES_DIR)}"
        return _guarded(response, att)
    if settings.FILES_OFFLOAD == "apache":
        response = HttpResponse(content_type=att.mime)
        response["X-Sendfile"] = str(path)
        return _guarded(response, att)
    return _guarded(FileResponse(open(path, "rb"), content_type=att.mime), att)
