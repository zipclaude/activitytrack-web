# Security

What protects an ActivityTrack Web server, what the administrator has to
do, and the review of 2026-10-30 (ROADMAP phase 9).

## 1. What the server does

| Area | Protection |
|---|---|
| Web login | Django sessions over HTTPS (`Secure` cookies when `base_url` is https), CSRF on every form, passwords hashed with Argon2, at least 10 characters, not common, not like the name |
| Two-step login | optional per user, required by the organisation for admins, managers or everyone (Utenti page): after the password a code from an authenticator app (TOTP, RFC 6238, implemented in `web/mfa.py`, each code once) or sent by email (`[mail]` in settings.ini), 5 tries; ten single-use recovery codes, stored hashed; an admin resets it for a lost phone. Phones are not concerned: they use their device token |
| Password guessing | 10 failures of one name from one address, or 50 from one address, in 15 minutes hold the login back (`web/auth.py`) |
| Phones | pairing codes of 160 random bits, single use, 10 minutes; device tokens of 256 bits stored as SHA-256 only; a revoked phone or a disabled user is refused at once |
| Self-signed certificates | the pairing QR code carries the key's fingerprint and the app pins it (`[server] tls_pin`) |
| Data | every query is filtered by organisation and by what the profile may read (ARCHITECTURE §5); the sync engine checks ownership on every change |
| Attachments | sent by the server only to whom may read the entry; shown in the browser only for images, PDF, audio and video, anything else downloaded, with a `sandbox` Content-Security-Policy |
| Pages | a Content-Security-Policy (scripts and styles from the site only, no frames, no plugins, forms to the site), `X-Frame-Options: DENY`, `X-Content-Type-Options: nosniff`, `Referrer-Policy: same-origin`, templates escaped; no inline script (`web/static/web/app.js` reads data attributes) |
| Policies | signed with the organisation's key when configured; the app checks the signature |
| Secret key | generated on first start in the data folder, mode 0600 |

## 2. What the administrator does

- HTTPS only, with a valid certificate or the pinned self-signed one
  (INSTALL.md §4); the HTTP site only redirects.
- HSTS in the web server once HTTPS works for good:
  `add_header Strict-Transport-Security "max-age=31536000" always;` (nginx) or
  `Header always set Strict-Transport-Security "max-age=31536000"` (Apache).
- Keep the proxy's `X-Forwarded-For` (both example sites do): the login brake
  counts the browser's address.
- `/etc/activitytrack-web` readable by root and the service group only
  (the package does it), the data folder `0711`, its `files/` and database
  private to the service user.
- Backups of the database and of `files/`; the secret key and the policy
  key with them, kept as secret as the database.
- Updates of the distribution (`unattended-upgrades` on Debian/Ubuntu): Django,
  gunicorn and the libraries come from it.
- Optional: fail2ban on the web server's log for `POST /login/` answered 200
  (a failed login) for an extra brake per address.

## 3. Review of 2026-10-30

Read: settings, login, the sync API (pairing, push, pull, files), the web
views and their permissions, templates, the Debian package and the units.

Fixed:
- **Attachments shown inline with the phone's type**: an HTML or SVG file
  would have run its scripts in the session of whoever opened it (a manager,
  an admin). Now only images, PDF, audio and video are shown, with a
  `sandbox` policy; the rest is downloaded.
- **No brake on password guessing** at the web login: added (§1).
- **Uploads above 2.5 MB refused**: Django's body limit stopped photos well
  below `[files] max_mb`; uploads are now streamed to disk and checked
  against `max_mb`.
- **Large pushes refused**: 500 changes with long notes can pass 2.5 MB; the
  limit for a request is 16 MB.

Checked, nothing to change: pairing codes and tokens (entropy, hashing,
expiry, single use), CSRF and cookies, the organisation filter of every
query, open redirects (`next` checked against the host), the users page
(an admin cannot lock themselves out), template escaping (`|safe` only on
the QR code made by segno).

Afterwards (#24): a Content-Security-Policy on every page; the inline
scripts and `on…` handlers moved to `app.js`, a test keeps them out.

Afterwards (#25): two-step login, by authenticator app or email (§1).
