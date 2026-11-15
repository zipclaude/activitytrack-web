# SPDX-License-Identifier: GPL-3.0-or-later
from django.apps import AppConfig
from django.utils.translation import gettext_lazy as _


class SyncConfig(AppConfig):
    name = "sync"
    verbose_name = _("Sincronizzazione")
