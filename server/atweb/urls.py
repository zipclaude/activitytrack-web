# SPDX-License-Identifier: GPL-3.0-or-later
from django.contrib import admin
from django.urls import include, path

urlpatterns = [
    path("admin/", admin.site.urls),
    path("api/v1/", include("sync.urls")),
    path("policy/", include("policies.urls")),
    path("mappe/", include("maps.urls")),
    path("aggiornamenti/", include("updates.urls")),
    # the language chosen on the Preferences page (a cookie; without one the browser's language is followed)
    path("i18n/", include("django.conf.urls.i18n")),
    path("", include("web.urls")),
]
