# SPDX-License-Identifier: GPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 Flavio Visentin <flavio-activitytrack@infraconsulting.it>
from django.urls import path

from . import views

app_name = "policies"
urlpatterns = [
    path("", views.index, name="index"),
    path("<str:scope>/", views.edit, name="edit"),
]
