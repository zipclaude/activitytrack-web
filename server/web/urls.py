# SPDX-License-Identifier: GPL-3.0-or-later
from django.contrib.auth import views as auth
from django.urls import path

from . import views

urlpatterns = [
	path("", views.home, name="home"),
	path("login/", auth.LoginView.as_view(template_name="web/login.html"), name="login"),
	path("logout/", auth.LogoutView.as_view(), name="logout"),
	path("telefoni/", views.phones, name="phones"),
	path("telefoni/associa/", views.pair_new, name="pair_new"),
	path("telefoni/associa/<int:pk>/stato/", views.pair_status, name="pair_status"),
	path("telefoni/<uuid:pk>/revoca/", views.device_revoke, name="device_revoke"),
]
