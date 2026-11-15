# SPDX-License-Identifier: GPL-3.0-or-later
from django.apps import AppConfig
from django.utils.translation import gettext_lazy as _


class RecordsConfig(AppConfig):
    name = "records"
    verbose_name = _("Dati")
