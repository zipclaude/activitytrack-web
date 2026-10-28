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

from . import engine, wire


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
		now = timezone.now()
		if device.last_seen is None or (now - device.last_seen).total_seconds() > 60:
			Device.objects.filter(pk=device.pk).update(last_seen=now)
		request.device = device
		return view(request, *args, **kwargs)

	return wrapped


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
			app_version=str(body.get("appVersion") or "")[:40],
			fcm_token=str(body.get("fcmToken") or ""),
		)
		pairing.device = device
		pairing.save(update_fields=["device"])
	return JsonResponse(
		{
			"deviceId": str(device.pk),
			"token": token,
			"user": {"username": user.username, "name": user.get_full_name(), "profile": user.profile},
			"org": {"name": user.org.name},
			"serverTime": engine.now_ms(),
		}
	)


@device_api
@require_POST
def push(request):
	body = _json(request)
	if body is None:
		return _error(400, "invalid")
	try:
		return JsonResponse(engine.push(request.device, body))
	except engine.ChangeError as e:
		return _error(400, e.code, e.detail)


@device_api
@require_GET
def pull(request):
	try:
		cursor = int(request.GET.get("cursor", "0"))
		limit = min(max(int(request.GET.get("limit", "500")), 1), 500)
	except ValueError:
		return _error(400, "invalid")
	result = engine.pull(request.device.user, cursor, limit)
	if not result["more"]:
		Device.objects.filter(pk=request.device.pk).update(cursor=result["cursor"])
	return JsonResponse(result)


@device_api
@require_GET
def head(request):
	return JsonResponse({"rev": engine.head(request.device.user.org), "serverTime": engine.now_ms()})


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
	data = request.body
	if len(data) > settings.FILES_MAX_MB * 1024 * 1024:
		return _error(413, "too_large")
	digest = hashlib.sha256(data).hexdigest()
	claimed = request.headers.get("X-Sha256", "").lower()
	if (claimed and claimed != digest) or (att.sha256 and att.sha256 != digest):
		return _error(400, "sha256_mismatch")
	path.parent.mkdir(parents=True, exist_ok=True)
	fd, tmp = tempfile.mkstemp(dir=path.parent)
	with os.fdopen(fd, "wb") as f:
		f.write(data)
	os.replace(tmp, path)
	with transaction.atomic():
		rev = engine.next_rev(user.org)
		Attachment.objects.filter(pk=att.pk).update(has_file=True, sha256=digest, size=len(data), rev=rev)
	att.refresh_from_db()
	return JsonResponse({"record": wire.record(att)})


def serve_file(att, path):
	if settings.FILES_OFFLOAD == "nginx":
		response = HttpResponse(content_type=att.mime)
		response["X-Accel-Redirect"] = f"{settings.FILES_ACCEL_LOCATION.rstrip('/')}/{path.relative_to(settings.FILES_DIR)}"
		return response
	if settings.FILES_OFFLOAD == "apache":
		response = HttpResponse(content_type=att.mime)
		response["X-Sendfile"] = str(path)
		return response
	return FileResponse(open(path, "rb"), content_type=att.mime)
