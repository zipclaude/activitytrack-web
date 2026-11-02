# SPDX-License-Identifier: GPL-3.0-or-later
"""
The Django admin, for administrators only and through the site's own login:
its login form would skip the second step and the brake on password
guessing (web/auth.py, web/mfa.py).
"""

from django.contrib import admin
from django.contrib.admin.apps import AdminConfig as BaseAdminConfig
from django.core.exceptions import PermissionDenied
from django.shortcuts import redirect
from django.urls import reverse
from django.utils.translation import gettext_lazy as _


class AdminConfig(BaseAdminConfig):
    default_site = "web.adminsite.Site"


class Site(admin.AdminSite):
    site_header = site_title = "ActivityTrack"
    index_title = _("Amministrazione dei dati")

    def has_permission(self, request):
        # the profile decides, not is_staff: the administrators of the web pages
        return request.user.is_active and request.user.is_admin

    def login(self, request, extra_context=None):
        # imported here: auth's views need the app registry ready
        from django.contrib.auth.views import redirect_to_login

        if self.has_permission(request):
            return redirect("admin:index")
        if request.user.is_authenticated:
            # logged in, not an administrator: no switching account through here
            raise PermissionDenied
        return redirect_to_login(request.GET.get("next") or reverse("admin:index"), "login")

    def logout(self, request, extra_context=None):
        from django.contrib.auth.views import LogoutView

        return LogoutView.as_view()(request)
