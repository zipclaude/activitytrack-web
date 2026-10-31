# SPDX-License-Identifier: GPL-3.0-or-later
"""
Two-step login (docs/SECURITY.md): after the password, a code from an
authenticator app (TOTP, RFC 6238) or sent by email; recovery codes for a
lost phone. The organisation may require it for admins, managers or all.
"""

import base64
import hashlib
import hmac
import secrets
import struct
import time
from urllib.parse import quote

import segno
from django.conf import settings
from django.contrib import messages
from django.contrib.auth import login as auth_login
from django.contrib.auth.decorators import login_required
from django.core.mail import send_mail
from django.shortcuts import redirect, render
from django.urls import Resolver404, resolve
from django.utils.translation import gettext as _

from accounts.models import Profile, User

STEP, DIGITS, CODE_TTL, MAX_TRIES, RECOVERY = 30, 6, 600, 5, 10


# TOTP (RFC 6238: HMAC-SHA1, 30 s, 6 digits), one step of clock skew allowed

def new_secret():
    return base64.b32encode(secrets.token_bytes(20)).decode().rstrip("=")


def totp(secret, step):
    key = base64.b32decode(secret + "=" * (-len(secret) % 8))
    digest = hmac.new(key, struct.pack(">Q", step), hashlib.sha1).digest()
    offset = digest[-1] & 0x0F
    value = struct.unpack(">I", digest[offset:offset + 4])[0] & 0x7FFFFFFF
    return f"{value % 10 ** DIGITS:0{DIGITS}d}"


def totp_step(secret, code, after=0, now=None):
    """The step [code] belongs to (now ±1), when later than [after] (a code works once); else None."""
    now = time.time() if now is None else now
    current = int(now // STEP)
    for step in (current - 1, current, current + 1):
        if step > after and hmac.compare_digest(totp(secret, step), code):
            return step
    return None


def totp_uri(user, secret):
    label = quote(f"ActivityTrack:{user.username}")
    return f"otpauth://totp/{label}?secret={secret}&issuer=ActivityTrack&digits={DIGITS}&period={STEP}"


# codes by email: kept hashed in the session, 10 minutes

def mail_available():
    return bool(settings.EMAIL_HOST)


def send_code(request, user, key):
    code = f"{secrets.randbelow(10 ** DIGITS):0{DIGITS}d}"
    request.session[key] = {"hash": _hash(code), "until": time.time() + CODE_TTL}
    send_mail(
        _("Codice di accesso ActivityTrack"),
        _("Il codice per accedere ad ActivityTrack è %(code)s. Vale 10 minuti. Se non stai accedendo tu, avvisa l'amministratore.") % {"code": code},
        None, [user.email],
    )


def check_mail_code(request, key, code):
    sent = request.session.get(key)
    if not sent or sent["until"] < time.time() or not hmac.compare_digest(sent["hash"], _hash(code)):
        return False
    del request.session[key]
    return True


def _hash(code):
    return hashlib.sha256(code.encode()).hexdigest()


def new_recovery(user):
    """Ten fresh recovery codes, shown once; only their hashes stay."""
    codes = [f"{secrets.token_hex(2)}-{secrets.token_hex(2)}" for _ in range(RECOVERY)]
    user.mfa_recovery = [_hash(c) for c in codes]
    return codes


def use_recovery(user, code):
    h = _hash(code.strip().lower())
    if h in user.mfa_recovery:
        user.mfa_recovery = [x for x in user.mfa_recovery if x != h]
        user.save(update_fields=["mfa_recovery"])
        return True
    return False


def required(user):
    """The organisation asks this user for a second step."""
    level = user.org.mfa_required if user.org else ""
    return (level == "all" or (level == "managers" and (user.is_admin or user.profile == Profile.MANAGER))
            or (level == "admins" and user.is_admin))


def verify(request, user, code):
    """A code of the user's method, or a recovery code."""
    code = code.strip().replace(" ", "")
    if user.mfa_method == "totp":
        step = totp_step(user.totp_secret, code, after=user.totp_last)
        if step is not None:
            user.totp_last = step
            user.save(update_fields=["totp_last"])
            return True
    elif user.mfa_method == "email" and check_mail_code(request, "mfa_login_code", code):
        return True
    return use_recovery(user, code)


# the second login step

def login_step(request):
    """After the password (web/auth.py): the code; the session holds who and for how many tries."""
    pending = request.session.get("mfa_pending")
    user = User.objects.filter(pk=pending["user"], is_active=True).first() if pending else None
    if user is None:
        return redirect("login")
    if request.method == "POST":
        if request.POST.get("resend") and user.mfa_method == "email":
            send_code(request, user, "mfa_login_code")
            messages.info(request, _("Nuovo codice inviato a %(email)s.") % {"email": _masked(user.email)})
            return redirect("login_code")
        if verify(request, user, request.POST.get("code", "")):
            del request.session["mfa_pending"]
            auth_login(request, user, backend="django.contrib.auth.backends.ModelBackend")
            return redirect(pending.get("next") or settings.LOGIN_REDIRECT_URL)
        pending["tries"] = pending.get("tries", 0) + 1
        if pending["tries"] >= MAX_TRIES:
            del request.session["mfa_pending"]
            from .auth import record_failure

            record_failure(request, user.username)
            messages.error(request, _("Troppi codici sbagliati: accedi di nuovo."))
            return redirect("login")
        request.session["mfa_pending"] = pending
        messages.error(request, _("Codice non valido."))
    return render(request, "web/login_code.html", {"method": user.mfa_method, "email": _masked(user.email)})


def _masked(email):
    name, _at, domain = (email or "").partition("@")
    return f"{name[:2]}…@{domain}" if domain else ""


# the user's own page: set up, change, turn off

@login_required
def security(request):
    user = request.user
    p = request.POST
    shown_codes = None
    if request.method == "POST":
        action = p.get("action")
        if action == "totp_start":
            request.session["mfa_setup"] = {"method": "totp", "secret": new_secret()}
        elif action == "email_start":
            if not user.email or not mail_available():
                messages.error(request, _("Serve un indirizzo email nel tuo utente e la posta configurata sul server."))
            else:
                request.session["mfa_setup"] = {"method": "email"}
                send_code(request, user, "mfa_setup_code")
                messages.info(request, _("Codice inviato a %(email)s.") % {"email": _masked(user.email)})
        elif action == "confirm":
            setup = request.session.get("mfa_setup") or {}
            code = p.get("code", "").strip().replace(" ", "")
            ok = False
            if setup.get("method") == "totp":
                step = totp_step(setup["secret"], code)
                if step is not None:
                    user.totp_secret, user.totp_last, ok = setup["secret"], step, True
            elif setup.get("method") == "email":
                ok = check_mail_code(request, "mfa_setup_code", code)
                if ok:
                    user.totp_secret, user.totp_last = "", 0
            if ok:
                user.mfa_method = setup["method"]
                shown_codes = new_recovery(user)
                user.save(update_fields=["mfa_method", "totp_secret", "totp_last", "mfa_recovery"])
                del request.session["mfa_setup"]
                messages.success(request, _("Verifica in due passaggi attiva."))
            else:
                messages.error(request, _("Codice non valido."))
        elif action == "cancel":
            request.session.pop("mfa_setup", None)
        elif action == "recovery" and user.mfa_method:
            if user.check_password(p.get("password", "")):
                shown_codes = new_recovery(user)
                user.save(update_fields=["mfa_recovery"])
            else:
                messages.error(request, _("La password non è corretta."))
        elif action == "off" and user.mfa_method:
            if required(user):
                messages.error(request, _("L'organizzazione chiede la verifica in due passaggi: puoi cambiare metodo, non toglierla."))
            elif user.check_password(p.get("password", "")):
                reset(user)
                messages.success(request, _("Verifica in due passaggi disattivata."))
            else:
                messages.error(request, _("La password non è corretta."))
        if shown_codes is None:
            return redirect("security")
    setup = request.session.get("mfa_setup")
    qr = None
    if setup and setup.get("method") == "totp":
        qr = segno.make(totp_uri(user, setup["secret"]), error="m").svg_inline(scale=5, dark="#000", light="#fff")
    return render(request, "web/security.html", {
        "setup": setup, "qr": qr, "secret": " ".join(setup["secret"][i:i + 4] for i in range(0, len(setup["secret"]), 4)) if qr else "",
        "codes": shown_codes, "required": required(user), "mail": mail_available(), "left": len(user.mfa_recovery),
    })


def reset(user):
    user.mfa_method, user.totp_secret, user.totp_last, user.mfa_recovery = "", "", 0, []
    user.save(update_fields=["mfa_method", "totp_secret", "totp_last", "mfa_recovery"])


def enforce(get_response):
    """Middleware: someone the organisation requires a second step of, without one, only sets it up."""
    allowed = ("security", "logout", "login", "login_code")

    def middleware(request):
        user = getattr(request, "user", None)
        if user is not None and user.is_authenticated and not user.mfa_method and required(user):
            try:
                name = resolve(request.path_info).url_name
            except Resolver404:
                name = None
            if name not in allowed and not request.path_info.startswith(("/static/", "/api/")):
                messages.info(request, _("L'organizzazione chiede la verifica in due passaggi: attivala per continuare."))
                return redirect("security")
        return get_response(request)
    return middleware
