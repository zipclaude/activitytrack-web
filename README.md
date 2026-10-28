# ActivityTrack Web

Web companion and sync server of the [ActivityTrack](https://github.com/zipclaude/activitytrack)
Android app: the same work from a browser, each phone's data kept in sync
both ways, central master data, users with profiles, policies for the
phones.

Runs on Debian stable packages only: Python 3, Django, nginx, PostgreSQL or
SQLite.

| Document | |
|---|---|
| [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) | requirements, stack, components, profiles, master data, policies |
| [docs/SYNC.md](docs/SYNC.md) | sync protocol and pairing |
| [docs/ROADMAP.md](docs/ROADMAP.md) | phases and estimates |
| [docs/INSTALL.md](docs/INSTALL.md) | installation on Debian 13 |

## Development

```sh
apt install python3-django python3-segno python3-argon2 python3-cryptography
cd server
printf '[server]\ndebug = true\n' > local_settings.ini
python3 manage.py migrate
python3 manage.py setup --org Test --admin admin
python3 manage.py runserver
python3 manage.py test
```

License: GPL-3.0-or-later.
