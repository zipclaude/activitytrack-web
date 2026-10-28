# Roadmap

Estimates in working days of the assistant; device tests and owner reviews
come on top.

| # | Phase | Days | State |
|---|---|---|---|
| 0 | Specification: `ARCHITECTURE.md`, `SYNC.md` | 1–2 | draft, owner review |
| 1 | App: uids everywhere, `sync_change` triggers, shadow, migration v16, `policy-catalog.json` export | 3–4 | |
| 2 | Server base: models, users and profiles, API skeleton, Debian package, nginx/systemd, CI on trixie | 4–5 | started |
| 3 | Sync both ways and QR pairing, attachments; tests offline, with skewed clocks, interrupted pushes | 8–10 | |
| 4 | Web UI: Home with filters, start/stop, entry sheet, Da fare, Agenda, master data, expenses, reports and export | 8–12 | |
| 5 | Several users: team views, to-do assignment, company readers | 3–4 | |
| 6 | Policy editor, signing, delivery through the sync | 4–5 | |
| 7 | Management-system records (tested with the SDP plugin) | 2–3 | |
| 8 | FCM tickle | 1–2 | |
| 9 | Hardening, security review, admin manual it/en, beta | 4–5 | |

First milestone: phases 0–3 plus a minimal web page (one user, sync
working, entries listed and editable), about 3–4 weeks.
