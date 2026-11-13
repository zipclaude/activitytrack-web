# Changelog

Changes in behaviour, compatibility and features of ActivityTrack Web. Bug
fixes are not listed one by one (see `git log` and the issues), except
security fixes, each under "Security" with its reference.

## 1.0.0 (in pre-release)

Pre-releases so far: 1.0.0-alfa-1, 1.0.0-alfa-2, 1.0.0-beta-1. The list is cumulative until the
release. The activity model of 1.0 is the app's `docs/ACTIVITY-MODEL.md`; the breaks
with 0.9 are in its `CHANGELOG.md`.

### Compatibility

- Sync wire schema 3, with the compatibility window reset: the server speaks schema 3
  only, and apps and PC interfaces of 0.9 get `426 upgrade_required` ("Aggiorna l'app
  per sincronizzare"). `sync/head` and the mirrored packages still answer them, so they
  learn of the new version. The server and every device must be on 1.0.
- After its own migration each device's first sync is a full realignment. Where the
  device and the server migrated a to-do differently, the phone's version wins, unless a
  server-signed policy is in force on it (then the server's does); a PC interface never
  wins. Local changes not yet synced are pushed (the app's `docs/ACTIVITY-MODEL.md` §6).
- The data migrations `records` 0007–0010 are one-way: `migrate` refuses to go back to
  0.9. The way back is the backup taken before the upgrade (`docs/INSTALL.md`).
- The to-do stops being a record: wire types `todo` and `absence` are gone (an older
  app pushing them gets `unknown_type`); `entry` drops `todoUid` and gains `kind`,
  `deletedUtcMs`, `closedUtcMs`, `externalKey`, `dueDate`, `dueTimeMin`, `planDate`,
  `planStartMin`, `durationMin`, `priority`, `absenceKind`; its `status` is `TODO`,
  `RUNNING` or `CLOSED` (was ACTIVE, SUSPENDED, TO_COMPLETE, CLOSED); `segment` gains
  `planned` and the kind `ABSENCE` (`docs/SYNC.md` §1.1, §12.1).
- Database: the tables of `Todo`, `Absence` and `TodoAssignment` are gone; who
  assigned a to-do is kept by `ActivityAssignment`, only while the activity is `TODO`.
  Code reading the database directly must follow.
- Priority: the scale is reversed, 0 none, 1 low … 5 emergency; the old P1…P5 (1 the
  highest) are converted once by the migration.
- `externalKey` is unique with the company for each owner; a clash is refused as
  `invalid` / `duplicate`, a blank key is no key.
- Policy: `entries.source = "todosOnly"` becomes `"plannedOnly"`; the editor reads the
  old value as an alias. The features `todos`, `ignoreTodos`, `deleteTodos` keep their
  keys and now apply to `TODO` activities.
- CSV export of the reports: absences are rows of their own (status `ABSENCE`), with
  the new column `absence_min`; the earlier columns keep their positions.

### Features

- To-dos are activities with status `TODO`: plan (day, start, duration), due date and
  time, priority 0–5 with labels, external key; their segments are the plan (planned
  segments, drawn dashed, never counted), deleted when the activity starts or is ticked
  off.
- Absences are activities of kind `ABSENCE` with a kind (Assenza, Ferie, Permesso,
  Malattia), an optional company, and from–to days with optional times: one segment a
  day, over the working hours (lunch breaks cut out, days off skipped) or at the times
  given; one day with times is taken as chosen, also outside the working hours.
- The bin: an activity deleted from the web or a device is restorable; permanent delete
  only from the bin page.
- Displayed states of the activity model (Da fare, Pianificata, In ritardo, Lavoro,
  Viaggio, Pausa, Sospesa, Fatta, Incompleta, Assenza, Eliminata) with one icon and
  colour each, on every page.
- The activity page offers the commands the state allows: start, switch, resume, stop,
  reopen (with confirmation), tick and untick; the travel-from-work question; and the
  stop with a return trip (start the return, stop, or stop and add the return as long as
  the outbound travel).
- Agenda and reports: plans drawn dashed and left out of the totals; absences drawn by
  kind and counted apart from work, travel and pause, by kind (an absence without a
  company counts under any company filter).

### Behaviour

- An activity, its to-do and its absence have one lifecycle: TODO → RUNNING → CLOSED,
  an absence always CLOSED; there is no "Sospendi" button (an activity is suspended only
  when another one starts and concurrent activities are not allowed).
- Without concurrent activities, reopening, resuming and going back to work or travel
  suspend the user's other running activities, as a start does.
- The pages run only the commands the activity's state allows: a stale page or a forged
  request gets "azione non consentita in questo stato".
- Assigning a to-do moves it and its children (planned segments, references…) to the
  new owner; the old owner's devices drop them. Assigning to someone who already has the
  same external key with that company is refused with a message.
- The server refuses records that break the activity model (issue #55): a changed
  `kind`, an absence not `CLOSED`, `ABSENCE` segments outside absences (or other kinds
  inside them), planned segments on an activity that is not `TODO`, more than one open
  segment, a `mode` that is not the open segment's kind, an open segment in a `CLOSED`
  activity, a real segment in a `TODO` one. The push answers `invalid` with the rule as
  `detail` (`kind_changed`, `absence_not_closed`, `segment_kind`, `planned_not_todo`,
  `open_segments`, `closed_open_segment`, `todo_real_segments`, `mode_mismatch`;
  `docs/SYNC.md` §1.1). A push is one transaction and the rules across an activity and
  its segments are judged on what the whole push leaves; data stored before is not
  refused for an old violation. The web's commands follow the same rules.

Various bug fixes.

Earlier versions: see the release notes of each tag.
