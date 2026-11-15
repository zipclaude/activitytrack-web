# SPDX-License-Identifier: GPL-3.0-or-later
from django.apps import AppConfig
from django.utils.translation import gettext_lazy as _


class AccountsConfig(AppConfig):
    name = "accounts"
    verbose_name = _("Utenti")
