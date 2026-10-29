# SPDX-License-Identifier: GPL-3.0-or-later
"""settings.ini: the only file an administrator edits (docs/INSTALL.md)."""

import configparser
import os
from pathlib import Path

DEFAULT_PATH = "/etc/activitytrack-web/settings.ini"
DEV_PATH = Path(__file__).resolve().parent.parent / "local_settings.ini"


def load():
    path = os.environ.get("ATWEB_CONFIG")
    if not path:
        path = DEFAULT_PATH if os.path.exists(DEFAULT_PATH) else str(DEV_PATH)
    ini = configparser.ConfigParser(interpolation=None)
    ini.read(path, encoding="utf-8")
    return ini


def secret_key(ini, data_dir):
    """The key from the ini, else a file under the data folder made on first start."""
    key = ini.get("server", "secret_key", fallback="").strip()
    if key:
        return key
    path = Path(data_dir) / "secret_key"
    if not path.exists():
        import secrets
        path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w") as f:
            f.write(secrets.token_urlsafe(50))
    return path.read_text().strip()
