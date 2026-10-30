# ActivityTrack Web

The web companion and sync server of
[ActivityTrack](https://github.com/zipclaude/activitytrack), the Android
app for freelancers and technicians who work for several companies, on
their clients, in different roles.

The app stays fully usable offline; ActivityTrack Web adds a second copy
of each phone's data on a server of your own, kept in sync both ways, and
the things a single phone cannot do: central master data, several users
with profiles, read-only access for client companies, and policies for the
phones edited from a browser.

Status: under development (phases in [docs/ROADMAP.md](docs/ROADMAP.md)).
Sync, pairing, profiles, central master data, attachments and the policy
editor work; the browser screens for daily work (Home, To-dos, Agenda,
Reports) come next.

## Purpose

- **A cloud for the app, on your server.** Every phone's activities,
  to-dos, expenses, consumables, attachments and master data reach the
  server, and changes made on the server or on another phone reach the
  phone. Losing or replacing a phone loses nothing.
- **Work from the browser.** The same work as the app from a computer:
  start and stop activities, complete the details, plan to-dos, look at
  the agenda and the reports (in progress).
- **One organisation, many users.** Central companies, clients, roles,
  presets and consumables for everyone; each user may still add their own
  when the policy allows it. Managers see their team; a client company's
  administration gets a read-only account for its reports.
- **Phones managed centrally.** The administrator edits the phones' policy
  (which features, fields and settings, locked or hidden) on a web page;
  every phone applies it at its next sync.
- **Management systems stay on the phone.** The app's plugins (Antares,
  ServiceDesk Plus…) run on the phone; their tickets and the activities
  done on them travel through the server like any other data and can be
  worked on the web, then the phone sends them to the management system.

## Features

| Area | What it does |
|---|---|
| Sync | Both ways, offline-first: the phone logs every change (SQLite triggers), pushes only the fields that changed and pulls what changed on the server since its last cursor. The later change wins field by field, with the phone's clock offset corrected. Deletes travel as tombstones. |
| Pairing | The web page shows a one-time QR code (10 minutes); the app reads it with its camera, or the link is pasted. With a self-signed certificate the code carries the server key's fingerprint, and the phone pins it. Phones can be revoked from the web. |
| Profiles | Administrator, Manager, User, Company reader. |
| Master data | Central (made on the web, read-only for users) or personal (made on a phone, synced only to that user's phones), marked in the app's lists. A central record adopts a personal one with the same name or code. |
| Attachments | Photos, receipts, voice notes and files travel after their record, checked by SHA-256; downloads on Wi-Fi or when an attachment is opened. Size limit set on the server. |
| Plugins | One phone per user sends to the management systems (chosen on the Phones page); entries closed or deleted on the web are sent or deleted there by that phone. |
| Conflicts | Two running activities, overlapping segments after a merge ("to check"), and records the server refused are shown on the phone, with "discard the change". |
| Policies | Per organisation, per profile, per user, merged key by key; a graphical editor built from the app's own policy catalog, checked before saving; optional signature with the organisation's certificate. |
| Push | None: the phone syncs after its changes, while the app is open and every 15 minutes (no Firebase). |

What does not travel: location samples (km and travel times do), the call
log, the plugins' secrets and their export bookkeeping.

## Technical specification

| | |
|---|---|
| Platform | Debian 13 "trixie": **only packages from the Debian stable repositories** |
| Language and framework | Python 3.13, Django 4.2 LTS (`python3-django`, kept by Debian security); runs on Django 5.2 too |
| Application server | gunicorn 23 under systemd (socket activation, hardened unit) |
| Web server | nginx 1.26 or Apache 2.4 (reverse proxy); at the root of a host or under a path of an existing site |
| Database | PostgreSQL 17 (`python3-psycopg`) or SQLite 3.46 for small installations |
| Passwords | Argon2id (`python3-argon2`) |
| QR codes | `python3-segno` (SVG) |
| Cryptography | `python3-cryptography`: policy signatures, certificate fingerprints |
| Browser side | server-rendered HTML, plain JavaScript files; no npm, no bundler, no CDN |
| API | JSON over HTTPS, bearer device tokens stored hashed; `api/v1/pair`, `sync/push`, `sync/pull`, `sync/head`, `files/<uid>`, `policy` |
| Attachments | stored under the data folder; sent by Django, or by nginx (`X-Accel-Redirect`) / Apache (`X-Sendfile`) after the permission check |
| Tenancy | one organisation per installation; every record carries its organisation, ready for several |
| Languages | Italian first, English |
| License | GPL-3.0-or-later |

The Android side (schema, triggers, sync engine, pairing, QR reader) lives
in the app's repository; its tests run the sync engine against a real
ActivityTrack Web.

## Documentation

| Document | |
|---|---|
| [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) | requirements, stack, components, profiles, central and personal master data, policies, plugins |
| [docs/SYNC.md](docs/SYNC.md) | sync protocol v1: records, change log, clocks, revisions, endpoints, first sync, pairing, conflicts |
| [docs/manual](docs/manual/it/admin.md) | administrator's manual, Italian and [English](docs/manual/en/admin.md): users and profiles, phones, policies, reports, backups (PDF with each release) |
| [docs/SECURITY.md](docs/SECURITY.md) | what protects the server, what the administrator does, the security review |
| [docs/INSTALL.md](docs/INSTALL.md) | installation: packages for Debian 13/14 and Ubuntu 24.04/26.04, tarball or clone for those and RHEL / Oracle Linux, Arch, FreeBSD; nginx or Apache, at a host's root or under a path |
| [docs/ROADMAP.md](docs/ROADMAP.md) | phases and their state |

Until the first release these documents are on the development branch
(`phase-0-2-foundation`).

## Installation (summary)

```sh
apt install gunicorn python3-django python3-segno python3-argon2 python3-cryptography gettext libjs-jquery
# optional: postgresql python3-psycopg; nginx or apache2
```

Then the code under `/usr/share/activitytrack-web`, the configuration in
`/etc/activitytrack-web/settings.ini`, data in `/var/lib/activitytrack-web`,
the systemd socket and service, the web server site, and
`manage.py setup --org "<name>" --admin <user>` for the organisation and
its first administrator. Every step: [docs/INSTALL.md](docs/INSTALL.md).
A Debian package is planned.

## Development

```sh
apt install python3-django python3-segno python3-argon2 python3-cryptography
cd server
printf '[server]\ndebug = true\n' > local_settings.ini
python3 manage.py migrate
python3 manage.py setup --org Test --admin admin
python3 manage.py runserver
python3 manage.py test
python3 manage.py sync_schema      # the wire schema the app is checked against
```

CI runs the tests in a `debian:trixie` container with apt packages only,
on SQLite and PostgreSQL.

## License

GPL-3.0-or-later.
