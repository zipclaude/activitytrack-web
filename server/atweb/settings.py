# SPDX-License-Identifier: GPL-3.0-or-later
from pathlib import Path
from urllib.parse import urlsplit

from . import config

BASE_DIR = Path(__file__).resolve().parent.parent
INI = config.load()

DATA_DIR = Path(INI.get("server", "data_dir", fallback=str(BASE_DIR.parent / "var")))
DEBUG = INI.getboolean("server", "debug", fallback=False)
SECRET_KEY = config.secret_key(INI, DATA_DIR)
ALLOWED_HOSTS = [h.strip() for h in INI.get("server", "allowed_hosts", fallback="localhost,127.0.0.1").split(",") if h.strip()]
# public address written in the pairing QR code
BASE_URL = INI.get("server", "base_url", fallback="http://localhost:8000").rstrip("/")
# sha256 of the server's public key, for a certificate the phones do not trust (docs/SYNC.md §9)
TLS_PIN = INI.get("server", "tls_pin", fallback="").strip()
# installed under a path (https://host/activitytrack): the proxy strips it,
# Django writes it back into every URL
_base = urlsplit(BASE_URL)
URL_PREFIX = _base.path.rstrip("/")
FORCE_SCRIPT_NAME = URL_PREFIX or None
# forms posted from any of the server's names over https
CSRF_TRUSTED_ORIGINS = sorted({f"{_base.scheme}://{_base.netloc}"} | {f"https://{h}" for h in ALLOWED_HOSTS if not h.startswith(".")})

FILES_DIR = DATA_DIR / "files"
FILES_MAX_MB = INI.getint("files", "max_mb", fallback=25)
# a push carries up to 500 changes (sync.engine.MAX_CHANGES) with free texts: more
# than Django's 2.5 MB; files are streamed and checked against FILES_MAX_MB
DATA_UPLOAD_MAX_MEMORY_SIZE = 16 * 1024 * 1024
# who sends the attachments' bytes: Django itself, nginx (X-Accel-Redirect to an
# internal location) or Apache with mod_xsendfile (X-Sendfile)
FILES_OFFLOAD = INI.get("files", "offload", fallback="none").strip()
if FILES_OFFLOAD not in ("none", "nginx", "apache"):
    raise ValueError(f"[files] offload: {FILES_OFFLOAD!r}, expected none, nginx or apache")
FILES_ACCEL_LOCATION = INI.get("files", "accel_location", fallback="/_files/").strip()

# optional: phone policies signed with this certificate (the app's policy.json.sig);
# needed only for phones whose policy CN is already pinned
POLICY_KEY = INI.get("policy", "key", fallback="").strip()
POLICY_CERT = INI.get("policy", "cert", fallback="").strip()
POLICY_CHAIN = INI.get("policy", "chain", fallback="").strip()

INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "accounts",
    "records",
    "sync",
    "policies",
    "web",
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "atweb.middleware.content_security_policy",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.locale.LocaleMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]

ROOT_URLCONF = "atweb.urls"
WSGI_APPLICATION = "atweb.wsgi.application"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
                "web.context.tags",
            ],
        },
    },
]


def _database():
    engine = INI.get("database", "engine", fallback="sqlite")
    if engine == "sqlite":
        return {
            "ENGINE": "django.db.backends.sqlite3",
            "NAME": INI.get("database", "name", fallback=str(DATA_DIR / "activitytrack.sqlite3")),
            # writers wait instead of failing while another holds the lock
            "OPTIONS": {"timeout": 20},
        }
    if engine == "postgresql":
        return {
            "ENGINE": "django.db.backends.postgresql",
            "NAME": INI.get("database", "name", fallback="activitytrack"),
            "USER": INI.get("database", "user", fallback="activitytrack"),
            "PASSWORD": INI.get("database", "password", fallback=""),
            # empty host = Unix socket, peer authentication
            "HOST": INI.get("database", "host", fallback=""),
            "PORT": INI.get("database", "port", fallback=""),
            "CONN_MAX_AGE": 60,
        }
    raise ValueError(f"[database] engine: {engine!r}, expected sqlite or postgresql")


DATABASES = {"default": _database()}
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"
AUTH_USER_MODEL = "accounts.User"

PASSWORD_HASHERS = [
    "django.contrib.auth.hashers.Argon2PasswordHasher",
    "django.contrib.auth.hashers.PBKDF2PasswordHasher",
]
AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"},
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator", "OPTIONS": {"min_length": 10}},
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
]
LOGIN_URL = "login"
LOGIN_REDIRECT_URL = "home"
LOGOUT_REDIRECT_URL = "login"

LANGUAGE_CODE = "it"
LANGUAGES = [("it", "Italiano"), ("en", "English")]
LOCALE_PATHS = [BASE_DIR / "locale"]
TIME_ZONE = INI.get("server", "time_zone", fallback="Europe/Rome")
USE_I18N = True
USE_TZ = True

STATIC_URL = f"{URL_PREFIX}/static/"
STATIC_ROOT = INI.get("server", "static_root", fallback=str(DATA_DIR / "static"))

SESSION_COOKIE_SECURE = BASE_URL.startswith("https://")
CSRF_COOKIE_SECURE = SESSION_COOKIE_SECURE
# other applications under the same host keep their own cookies
SESSION_COOKIE_PATH = URL_PREFIX or "/"
CSRF_COOKIE_PATH = SESSION_COOKIE_PATH
SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")
SECURE_CONTENT_TYPE_NOSNIFF = True
X_FRAME_OPTIONS = "DENY"

LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "handlers": {"console": {"class": "logging.StreamHandler"}},
    "root": {"handlers": ["console"], "level": INI.get("server", "log_level", fallback="INFO")},
}
