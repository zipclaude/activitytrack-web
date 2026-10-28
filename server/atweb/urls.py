# SPDX-License-Identifier: GPL-3.0-or-later
from django.contrib import admin
from django.urls import include, path

urlpatterns = [
    path("admin/", admin.site.urls),
    path("api/v1/", include("sync.urls")),
    path("", include("web.urls")),
]
