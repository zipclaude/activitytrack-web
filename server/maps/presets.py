# SPDX-License-Identifier: GPL-3.0-or-later
"""Areas an admin can add with one click: (slug, name, latitude, longitude), 100 km around."""

from django.utils.translation import gettext_lazy as _

PRESETS = [
    ("torino", _("Torino e Piemonte"), 45.07, 7.69),
    ("aosta", _("Aosta e Valle d'Aosta"), 45.74, 7.32),
    ("milano", _("Milano e Lombardia"), 45.46, 9.19),
    ("genova", _("Genova e Liguria"), 44.41, 8.93),
    ("trento", _("Trento e Alto Adige"), 46.07, 11.12),
    ("venezia", _("Venezia e Veneto"), 45.44, 12.32),
    ("udine", _("Udine e Friuli"), 46.06, 13.24),
    ("bologna", _("Bologna e Emilia"), 44.49, 11.34),
    ("firenze", _("Firenze e Toscana"), 43.77, 11.25),
    ("ancona", _("Ancona e Marche"), 43.62, 13.52),
    ("perugia", _("Perugia e Umbria"), 43.11, 12.39),
    ("roma", _("Roma e Lazio"), 41.90, 12.50),
    ("pescara", _("Pescara e Abruzzo"), 42.46, 14.21),
    ("napoli", _("Napoli e Campania"), 40.85, 14.27),
    ("bari", _("Bari e Puglia"), 41.12, 16.87),
    ("lecce", _("Lecce e Salento"), 40.35, 18.17),
    ("potenza", _("Potenza e Basilicata"), 40.64, 15.80),
    ("cosenza", _("Cosenza e Calabria"), 39.30, 16.25),
    ("reggio-calabria", _("Reggio Calabria e stretto"), 38.11, 15.65),
    ("catania", _("Catania e Sicilia orientale"), 37.50, 15.09),
    ("palermo", _("Palermo e Sicilia occidentale"), 38.12, 13.36),
    ("cagliari", _("Cagliari e Sardegna meridionale"), 39.22, 9.12),
    ("sassari", _("Sassari e Sardegna settentrionale"), 40.73, 8.56),
]
