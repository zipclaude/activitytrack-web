# Roadmap

Estimates in working days of the assistant; device tests and owner reviews
come on top.

| # | Phase | Days | State |
|---|---|---|---|
| 0 | Specification: `ARCHITECTURE.md`, `SYNC.md` | 1–2 | done |
| 1 | App: uids everywhere, `sync_change` triggers, shadow, migration v16, `policy-catalog.json` export | 3–4 | done (app #111, #112, #117) |
| 2 | Server base: models, users and profiles, API skeleton, Debian package, nginx/systemd, CI on trixie | 4–5 | done, Debian package to do; installed on a trixie test host |
| 3 | Sync both ways and QR pairing, attachments; tests offline, with skewed clocks, interrupted pushes | 8–10 | done (app #113, #114, #116); phone test pending |
| 4 | Web UI: Home with filters, start/stop, entry sheet, Da fare, Agenda, master data, expenses, reports and export | 8–12 | Home, activities, entry sheet, to-dos, master data, Agenda (day, week, month), report + CSV rounded as the app, Home list with the app's filters |
| 5 | Several users: team views, to-do assignment, company readers | 3–4 | done: team and reader views, to-do assignment |
| 6 | Policy editor, signing, delivery through the sync | 4–5 | done (app #117) |
| 7 | Management-system records (tested with the SDP plugin) | 2–3 | done (app #118); test with SDP pending |
| 8 | ~~FCM tickle~~ | — | dropped 2026-10-30: no Firebase; periodic sync (SYNC.md §10) |
| 9 | Hardening, security review, admin manual it/en, beta | 4–5 | |

First milestone: phases 0–3 plus a minimal web page (one user, sync
working, entries listed and editable), about 3–4 weeks.

App-side work is tracked in the app's issues #111–#121 (index: #122).
Server items they need: `pluginHolder` in the pair and head answers
(#118), an entry field `needsReview` for overlapping segments (#119).
