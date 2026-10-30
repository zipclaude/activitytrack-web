# ActivityTrack Web — architecture

The web companion of the [ActivityTrack](https://github.com/zipclaude/activitytrack)
Android app: the same work as the app from a browser, a cloud copy of each
phone's data kept in sync both ways, central master data, user management
with profiles, and a graphical editor for the phones' policies.

## 1. Owner requirements (2026-10-28)

| # | Requirement |
|---|---|
| R1 | Server side only software from the Debian stable repositories (Debian 13 "trixie") |
| R2 | Web server nginx or Apache; no PHP, no Ruby; Python or Rust |
| R3 | Phone and server stay in sync while both are reachable; after a disconnection they resync both ways as soon as possible |
| R4 | Pairing a phone is simple: the app reads a QR code shown on the web page |
| R5 | Several users on one server, with profiles (permissions) |
| R6 | The admin defines graphically the policies applied to the phones |
| R7 | Management-system plugins do not run on the server; their to-dos and entries are synced and can be worked on the web; the phone keeps syncing with the management system |
| R8 | Master data are central, but a user may add their own when the policy allows; the app and the web show which records are central and which are personal |
| R9 | One server per organisation for now; the design leaves room for multi-tenancy |
| R10 | No push: periodic sync and sync on changes (decided 2026-10-30, instead of FCM: no Firebase dependency) |
| R11 | Attachments are synced (a read-only account per company lets its administration see the reports on its own); location samples are not (km and travel times are enough) |
| R12 | PostgreSQL, with SQLite as an option for small installations |

## 2. Stack (all in trixie)

| Layer | Package (trixie version) | Why |
|---|---|---|
| Language | `python3` 3.13 | complete web stack packaged in Debian; Rust crates in Debian (`librust-*`) are incomplete for a web app with a database |
| Framework | `python3-django` 4.2 LTS | users, permissions, sessions, CSRF, forms, migrations, i18n, admin; the phones' JSON API is plain Django views (no extra framework) |
| App server | `gunicorn` 23 under systemd | |
| Web server | `nginx` 1.26 or `apache2` 2.4 | TLS, static files, attachments via `X-Accel-Redirect` (nginx) or `X-Sendfile` (Apache with `libapache2-mod-xsendfile`); without offload Django sends them |
| TLS | `certbot` (`python3-certbot-nginx` / `python3-certbot-apache`) or own certificate | a self-signed certificate works too: the QR carries its fingerprint |
| Database | `postgresql` 17 (`python3-psycopg` 3.2) or SQLite 3.46 | |
| Passwords | `python3-argon2` | Argon2id hasher of Django |
| QR codes | `python3-segno` | SVG QR codes, no image library |
| Signatures | `python3-cryptography` 43 | policy signing (ECDSA P-256) |
| Static files | `python3-whitenoise` (optional) | when nginx does not serve `/static` |

Django 4.2 is past its upstream end of life (April 2026); Debian keeps
fixing it for trixie (`4.2.28-0+deb13u2`). When Debian 14 becomes stable the
code moves to its Django (5.2 LTS); nothing deprecated in 5.x is used.

The browser side is server-rendered HTML with plain JavaScript files in the
repository: no npm, no bundler, no CDN.

## 3. Components

```
 phone (ActivityTrack)                         server (one per organisation)
┌───────────────────────┐   HTTPS JSON   ┌─────────────────────────────────────────┐
│ Room DB + triggers    │ ─────────────▶ │ nginx/Apache ─▶ gunicorn ─▶ Django      │
│ sync_change (outbox)  │ ◀───────────── │   api/v1/sync   push · pull · files     │
│ SyncWorker            │                │   api/v1/pair   QR pairing               │
│ plugins ⇄ mgmt system │ ◀───────────── │   web UI        same screens as the app  │
└───────────────────────┘                │   admin         users, devices, policies │
                                          │ PostgreSQL / SQLite · files/ (attachments)│
                                          └─────────────────────────────────────────┘
```

Django apps (`server/`):

| App | Content |
|---|---|
| `accounts` | organisation, users and profiles, company grants of the readers, devices, pairing codes |
| `records` | the synced data: master data, to-dos, entries and their parts, calendar data |
| `sync` | the sync protocol (`docs/SYNC.md`): push, pull, files, revision counter, conflicts |
| `policies` | policy catalog (exported by the app), policies, assignments, signing |
| `web` | the browser UI |

## 4. Tenancy

Every record carries `org`. One installation serves one organisation
(created by `manage.py setup`), and every query filters by the request's
organisation, so serving several organisations later means resolving the
organisation from the host name or the login, not changing the schema.

## 5. Users and profiles

The word "role" is taken: it is master data in the app (Sysadmin,
Developer…). Permissions on the server are **profiles**.

| Profile | Can |
|---|---|
| Admin | everything: users, devices, policies, central master data, every user's data |
| Manager | central master data; read their team's entries and everyone's entries done for the companies they are responsible for (their `companies`); create and assign to-dos to the team; team and company reports. Edits (times) only their team's |
| User | own entries and to-dos, own personal master data when the policy allows it; read central master data |
| Company reader | read only, for the companies granted to them: the closed entries of every user done for those companies, with expenses, consumables and attachments, and the reports. Meant for a client company's administration |

A manager gives to-dos to their team, an admin to anyone with data: the
to-do becomes the assignee's (their phones pull it, the previous owner's
drop it as "gone", SYNC.md §5), the server alone remembers who assigned it.
The assignee works it as their own; the manager still reads and edits it
on the web.

A user may pair several phones; each phone belongs to one user (no account
switching in the app).

Times locked by the policy (`settings.editableTimes` false and locked or
hidden) hold on the web as on the phone: the owner starts activities now
only and adds forgotten travel and pauses, but does not edit or remove
segments. An admin, or the owner's manager, edits them on the web (the
change is written as the owner's), e.g. to fix segments a sync left
overlapping (SYNC.md §11).

## 6. Master data: central and personal

Companies, company sites, clients, client sites, contacts, roles, tags,
activity presets and consumables have an `owner`:

- `owner = null` → **central**: created on the web by an admin or a manager,
  read-only for users, synced to every phone of the organisation;
- `owner = <user>` → **personal**: created by that user (phone or web) when
  the policy allows it, synced only to that user's phones, shown with a
  "personal" mark in the app and on the web.

An admin can promote a personal record to central, or merge it into an
existing central one: references move to the central record and the
personal one is deleted (the phones receive both changes).

Imports and plugins on the phone create personal records; when one matches
a central record by natural key (company name or external code; client
company + name or external code; role name; tag name) the phone adopts the
central record instead (`docs/SYNC.md` §7).

Tags (app issue #133) carry a colour; entries keep their tags by name, so
the name is unique among the central tags and among each user's personal
ones. A same-named local tag adopts the server's (the server's colour
wins). Renaming a tag on the web rewrites the entries carrying it (a
central one: every user's; a personal one: its owner's); deleting it
leaves the name on the entries as a free tag. `settings.tagsFreeText` false
(locked) allows only the master data's tags on new edits.

## 7. Policies

The app's `PolicyCatalog` is the single source of the policy keys. A Gradle
task of the app writes it as `policy-catalog.json` (key, kind, default,
allowed values, label, group); the server imports that file and builds the
editor's form from it, so the editor and the app cannot drift.

For every key the admin chooses: not set / value / value locked / hidden.
A policy is assigned to the organisation, to a profile or to single users;
the most specific one wins key by key. The server signs the resulting
policy with its key (the certificate's CN is pinned by the app, as for
`policy.json` files) and delivers it through the sync (`policy` record).

## 8. Management systems

Plugins run only on the phone. What they import (to-dos, personal master
data, remote lists such as services and activity types) is synced like any
other record; their fields (`pluginFields`, imported-field markers) travel
with the records and stay read-only on the web where the plugin made them
read-only. Entries worked on the web reach the phone with the next sync,
and the phone's plugin sends them to the management system as today.

A user with several phones: only the phone marked as plugin holder sends to
the management system (`device.plugin_holder`).

## 9. What does not travel

Location samples, call log, the plugins' export bookkeeping
(`remote_record`, `remote_deleted`), plugin secrets and connector settings,
the app's own settings (except what the policy fixes).

## 10. Deployment

A Debian package `activitytrack-web` (built in CI on `debian:trixie`):

- depends only on trixie packages (§2);
- installs the code under `/usr/share/activitytrack-web`, configuration in
  `/etc/activitytrack-web/settings.ini`, data (SQLite database,
  attachments) in `/var/lib/activitytrack-web`;
- a systemd unit for gunicorn (socket under `/run`), a timer for the
  nightly backup (`pg_dump` or SQLite `.backup` + attachments, 14 kept);
- an nginx site and an Apache site to adapt (`deploy/nginx-site.conf`,
  `deploy/apache-site.conf`): both proxy to gunicorn's Unix socket;
- `activitytrack-web-manage setup` creates the organisation and the first admin.
