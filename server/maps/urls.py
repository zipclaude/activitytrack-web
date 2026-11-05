# SPDX-License-Identifier: GPL-3.0-or-later
from django.urls import path

from . import views

app_name = "maps"
urlpatterns = [
    path("", views.index, name="index"),
    path("abilita/", views.toggle, name="toggle"),
    path("stato/", views.status, name="status"),
    path("stima/", views.estimate, name="estimate"),
    path("nuova/", views.edit, name="new"),
    path("<int:pk>/", views.edit, name="edit"),
    path("<int:pk>/prepara/", views.rebuild, name="rebuild"),
    path("<int:pk>/elimina/", views.delete, name="delete"),
]
