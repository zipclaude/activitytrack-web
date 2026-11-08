# SPDX-License-Identifier: GPL-3.0-or-later
from django.urls import path

from . import views

app_name = "updates"
urlpatterns = [
    path("", views.index, name="index"),
    path("salva/", views.save, name="save"),
    path("controlla/", views.check, name="check"),
    path("azzera/", views.reset, name="reset"),
    path("chiavi/aggiungi/", views.signer_add, name="signer_add"),
    path("chiavi/<int:pk>/togli/", views.signer_remove, name="signer_remove"),
]
