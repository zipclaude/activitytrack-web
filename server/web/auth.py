# SPDX-License-Identifier: GPL-3.0-or-later
"""The web login with a brake on password guessing."""

from datetime import timedelta

from django.contrib.auth.views import LoginView
from django.shortcuts import redirect
from django.utils import timezone

from accounts.models import LoginFailure

# failures of one name from one address, and from one address for any name, in the window
PER_NAME, PER_ADDRESS, WINDOW = 10, 50, timedelta(minutes=15)


def client_address(request):
    """The browser's address: behind the proxy (Unix socket, loopback) the last X-Forwarded-For entry, which our proxy appends."""
    remote = request.META.get("REMOTE_ADDR") or ""
    if remote in ("", "127.0.0.1", "::1"):
        forwarded = request.META.get("HTTP_X_FORWARDED_FOR", "")
        if forwarded:
            return forwarded.split(",")[-1].strip()[:64]
    return remote[:64]


class Login(LoginView):
    template_name = "web/login.html"

    def post(self, request, *args, **kwargs):
        # a new password attempt starts over
        request.session.pop("mfa_pending", None)
        since = timezone.now() - WINDOW
        LoginFailure.objects.filter(at__lt=since).delete()
        name = (request.POST.get("username") or "").strip().lower()[:150]
        address = client_address(request)
        recent = LoginFailure.objects.filter(at__gte=since)
        if recent.filter(username=name, address=address).count() >= PER_NAME or recent.filter(address=address).count() >= PER_ADDRESS:
            return self.render_to_response(self.get_context_data(form=self.get_form(), throttled=True))
        response = super().post(request, *args, **kwargs)
        if not request.user.is_authenticated and "mfa_pending" not in request.session:
            record_failure(request, name)
        return response

    def form_valid(self, form):
        """The password is right: with a second step, the code comes first (web/mfa.py)."""
        user = form.get_user()
        if user.mfa_method:
            from . import mfa

            self.request.session["mfa_pending"] = {"user": user.pk, "next": self.get_success_url(), "tries": 0}
            if user.mfa_method == "email":
                mfa.send_code(self.request, user, "mfa_login_code")
            return redirect("login_code")
        return super().form_valid(form)


def record_failure(request, name):
    LoginFailure.objects.create(username=(name or "").strip().lower()[:150], address=client_address(request))
