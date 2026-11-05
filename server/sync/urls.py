# SPDX-License-Identifier: GPL-3.0-or-later
from django.urls import path

from . import views

urlpatterns = [
    path("pair", views.pair),
    path("sync/push", views.push),
    path("sync/pull", views.pull),
    path("sync/head", views.head),
    path("policy", views.policy),
    path("files/<str:uid>", views.files),
    path("maps", views.maps),
    path("maps/<slug:slug>", views.map_file),
]
