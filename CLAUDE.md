# CLAUDE.md — notes for the assistant

Technical notes to set up the environment and work on the code. Read
`docs/ARCHITECTURE.md`, `docs/SYNC.md` and `docs/ROADMAP.md` first. The
Android app is `activitytrack/activitytrack` on GitLab; project history and work in
progress live in the GitLab issues and merge requests (until 0.9.7-beta1 they
were on GitHub, see the first merge request of this repository).

## 0. Branches and releases (read first)

The whole workflow is in
[activitytrack-doc/agents/workflow.md](https://gitlab.com/activitytrack/activitytrack-doc/-/blob/main/agents/workflow.md)
(usually cloned next to this repository as `../activitytrack-doc`). In short:

- `main` is the **last released version**, `next` is the development branch. Work happens
  in `feature/<name>` or `issue/<n>-<name>`, cut from `next` and merged back into it with a
  merge request. Nobody pushes to `main` or `next`.
- Stable release: merge request `next` → `main`, merged fast-forward only; tag `vX.Y.Z` on
  `main`; then merge `main` into `next`.
- Beta: tag `vX.Y.Z-beta-N` on `next` (X.Y.Z is the *next* version); Debian version
  `X.Y.Z~beta-N-1` (the CI turns the tag's first hyphen into a tilde,
  the changelog adds the revision). Needs the unit tests and the local Wine jobs, not the paid Windows pipeline.
- Security patch: `security/<name>` cut from `main`, version `X.Y.Z.1`, released at once with
  the security report, then merged into `next`.
- Test locally first. Pipelines on GitLab's shared runners start only when the owner says so;
  jobs with `tags: [local]` run on the owner's runner (see `agents/local-ci.md` in the
  documentation repository).
- The Android application id, the AppStream ids and the package names keep the
  `it.infraconsulting` names; they do not follow the GitLab group.

## 1. Working rules

- English for code, comments, commits and docs; Italian with the maintainer.
  The UI is Italian first (Django i18n, English translation): the message ids
  are the Italian texts, `server/locale/en/LC_MESSAGES/django.po` is the English
  catalog and `django.mo` its compilation, which is committed (the package and
  a tarball install need no `msgfmt`). Changing a UI text:
  ```sh
  cd server
  python3 manage.py makemessages -l en --no-location --no-obsolete
  # translate the empty entries of locale/en/LC_MESSAGES/django.po
  python3 manage.py compilemessages -l en
  ```
  `web/test_i18n.py` fails on an untranslated or fuzzy entry and on a `django.mo`
  that is not the `django.po` compiled. English is British ("organisation",
  "colour"); use the app's terms (To-dos, Master data, Quick picks, management
  system). The language is the browser's, or the one chosen in Preferences
  (the `django_language` cookie).
- Indentation: Python follows PEP 8 (4 spaces); everything else tabs (`.editorconfig`).
- **Never use npm**, not even for tools. No CDN, no bundler: plain JS files
  in `server/web/static`.
- Ask before guessing on ambiguous requirements; answers come as numbered
  lists matching numbered questions.

## 2. Hard constraints

- Server side **only Debian stable (trixie) packages** (`docs/ARCHITECTURE.md` §2).
  Before adding a dependency check it exists in trixie:
  `curl -s "https://api.ftp-master.debian.org/madison?package=<source>&s=trixie&text=on"`.
- Code runs on trixie's Django 4.2 **and** Django 5.2 (Debian testing).
  No Django REST framework: the API is plain views.
- nginx and Apache are both supported (`deploy/`); keep the two site files
  and `[files] offload` in step.
- CI (`.gitlab-ci.yml`, job `test`) runs in `debian:trixie` with apt packages
  only, on SQLite and PostgreSQL 17; keep it green.

## 3. Development environment

On Debian trixie (or newer):

```sh
apt install python3-django python3-segno python3-argon2 python3-cryptography python3-psycopg gettext
cd server
printf '[server]\ndebug = true\n' > local_settings.ini   # ignored by git
python3 manage.py migrate
python3 manage.py setup --org Test --admin admin
python3 manage.py runserver
python3 manage.py test
python3 manage.py sync_schema    # wire schema for the app side
```

Without root or on another distribution: a virtualenv with Python 3.13 and
the trixie versions pinned (Django 4.2.28, segno 1.6.6, argon2-cffi 21.1.0,
psycopg 3.2.6, cryptography 43.0.0); it only mirrors trixie, CI is the
reference. A second virtualenv with Django 5.2 checks the newer release.

Screenshots without Playwright: fetch pages with curl (cookie jar + CSRF
token), point `/static/` to the dev server and render them with
`chromium-headless-shell --screenshot`.

## 4. File map

```
docs/ARCHITECTURE.md   requirements R1–R12, stack, profiles, central/personal master data
docs/SYNC.md           protocol v1: records, triggers on the phone, clocks, revisions, endpoints, pairing
docs/ROADMAP.md        phases and estimates
docs/INSTALL.md        installation on trixie, nginx or Apache
deploy/                settings.ini.example, systemd service + socket, nginx and Apache sites
server/atweb/          settings (from settings.ini), urls
server/accounts/       Organization, User (profile, manager, companies), Device, PairingCode; manage.py setup
server/records/        synced models (Synced base: uid, org, owner, rev, deleted, clock)
server/sync/           wire.py (names/values), engine.py (apply/push/pull), views.py (API), tests.py
server/web/            browser UI (templates, static)
```

## 5. Commit format

End commit messages with:
```
Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>
```
