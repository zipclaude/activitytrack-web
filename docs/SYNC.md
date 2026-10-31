# Sync protocol (v1)

Phone and server are both full copies: the phone works offline as today,
the server is another node. The server decides every conflict, the phone
applies what the server returns.

## 1. Records

Every synced row is a **record**: `type`, `uid` (UUID, the same on every
node), `fields`, `deleted`. References between records are uids
(`companyUid`, `entryUid`…), never local ids.

| type | App table | Scope | Notes |
|---|---|---|---|
| `company` | company | central or personal | without `connector_*` (device-local) |
| `companySite` | company_site | as its company | |
| `client` | client | central or personal | |
| `clientSite` | client_site | as its client | |
| `contact` | contact + contact_phone | as its client | `phones` is a list field |
| `role` | role | central or personal | |
| `tag` | tag | central or personal | name is the natural key; colour 0xRRGGBB (app #133) |
| `preset` | activity_preset | central or personal | `lastUsed` is device-local |
| `material` | material | central or personal | |
| `place` | place | user | personal places (home…) |
| `todo` | todo | user (assignee) | managers may create them for their team |
| `entry` | time_entry + entry_tag | user | `tags` is a list field |
| `segment` | segment | as its entry | |
| `expense` | expense | as its entry | |
| `entryMaterial` | entry_material | as its entry | |
| `attachment` | attachment | as its entry | file transferred apart (§6) |
| `externalRef` | external_ref | as its entry | |
| `absence` | absence | user | |
| `dayOff` | day_off | user | uid derived from the date (UUIDv5) |
| `workSchedule` | work_schedule | user | |
| `lunchBreak` | lunch_break | user | |
| `remoteItem` | remote_item | user, read-only on the server | plugin lists for the entry form on the web |
| `policy` | policy_record | server → phone only | §8 |

Not synced: location_sample, call_log, remote_record, remote_deleted
(`docs/ARCHITECTURE.md` §9).

Field names follow the app's Kotlin properties: references end in `Uid`,
instants in `UtcMs` (epoch milliseconds UTC), local dates are
`yyyy-mm-dd` strings (`dueDate`, `planDate`, `date`), times of day are
minutes (`…Min`), amounts cents (`…Cents`). `manage.py sync_schema` prints
every type with its fields, kinds and nullability: the app's mapping is
checked against it.

## 2. Changes on the phone

- Migration of the app (v16) adds `uid` to every synced table that lacks
  it and a table `sync_change(seq, type, uid, op, changed_utc_ms)`.
- SQLite triggers on INSERT/UPDATE/DELETE of every synced table append to
  `sync_change`, except while the sync itself writes (a one-row
  `sync_flag.applying` read by the triggers' `WHEN`). Every write path —
  screens, widgets, imports, plugins, restore — is captured without
  touching the repositories.
- `sync_shadow(type, uid, json)` holds each record as last agreed with the
  server. A push sends only the fields that differ from the shadow.

## 3. Clocks

Each changed field carries the time of the change (`changed_utc_ms` of the
trigger), corrected by the phone's clock offset: the difference between
the request's `deviceTime` and the server's clock when it arrives. The server keeps per record the
time of each field's last change (`clock` map) and applies an incoming
field only when its time is later than the stored one: **last writer wins,
field by field**. Times in the future beyond the server's clock are
clamped to it.

## 4. Revisions

The server has one counter per organisation. Each record write takes the
next value as the record's `rev`, in the same transaction, holding the
counter's row lock until commit, so revisions become visible in order and
a pull by `rev > cursor` never skips a record committed late.

## 5. Endpoints

All under `/api/v1/`, JSON, `Authorization: Bearer <device token>`.

### `POST sync/push`

```json
{ "deviceTime": 1761650000000,
  "changes": [
    { "type": "entry", "uid": "6f1c…", "op": "upsert",
      "fields": { "activity": "Patched 3 servers", "status": "CLOSED" },
      "times":  { "activity": 1761649990000, "status": 1761649990000 } },
    { "type": "segment", "uid": "a2…", "op": "delete", "time": 1761649980000 } ] }
```

At most 500 changes per call, in the order of `sync_change` (parents
before children). The answer lists, per change, the record as the server
now has it (`applied`, or `conflict` with the server's values when a
field was refused) or an `error` (unknown type, reference to a record the
phone cannot see, permission). The phone writes the answered records into
its tables and shadow, deletes the pushed `sync_change` rows, and leaves
errors in a list shown under Settings → Sync.

### `GET sync/pull?cursor=<rev>&limit=500`

```json
{ "serverTime": 1761650000123, "cursor": 4711, "more": false,
  "records": [ { "type": "todo", "uid": "…", "rev": 4702, "deleted": false, "fields": { … } } ],
  "gone": [ { "type": "todo", "uid": "…" } ] }
```

Records visible to the device's user with `rev > cursor`, oldest first.
`gone` lists records that left the user's scope (a to-do assigned to
someone else, a personal record merged into a central one): the phone
removes them without echoing a delete. A field still pending in the
phone's `sync_change` is not overwritten: the pending push will settle it.

### `GET sync/head`

`{ "rev": 4711, "serverTime": …, "filesMaxMb": 25 }`: cheap check while
the app is in the foreground; the phone does not upload files above the limit.

### Files

`PUT files/<attachment uid>` (body = file, `Content-Type`, header
`X-Sha256`) and `GET files/<attachment uid>`. The `attachment` record
carries `sha256`, `size`, `mime` and the server's `hasFile`; the phone
pushes the record first, then uploads the file (the upload sets `hasFile`
and gives the record a new revision); until then the web shows "file in
arrivo". Phones download files lazily (on open,
or all on Wi-Fi after a restore). Limit: `files.max_mb` (default 25).

## 6. Deletes

A delete leaves a tombstone (`deleted = true`, the fields cleared) that is
pulled like any record; a deleted record cannot be updated again (an
update arriving later is answered as `conflict`, deleted). Children follow
their parent (entry → segments, expenses…), as on the phone. Tombstones are
purged after 180 days; a phone whose cursor is older than the purge does a
full resync (§7).

## 7. First sync of a phone

1. Pairing (§9) gives the device token.
2. The phone pulls everything (cursor 0).
3. **Adoption**: for each personal master record of the phone matching a
   pulled central record by natural key (`ARCHITECTURE.md` §6), the phone
   re-points its references to the central record and deletes the local
   copy; both changes are pushed like any other. Adoption runs at every
   pull, not only the first: a central record created later absorbs the
   personal ones of the same name. To-dos and entries already on the
   server (same uid, from a restored backup) are simply updated.
4. The phone pushes the rest of its data as new records.

A phone already synced that re-pairs (new token) just continues from its
cursor; a full resync repeats 2–4 and is idempotent.

## 8. Policy

The `policy` record (one per user, read-only for the phone) carries the
signed policy text and its signature; the app applies it as a
`policy.json` read from the data folder (same checks, same pinned CN).
When a sync policy is in force the file in the data folder is ignored.
A phone keeps its policy until another replaces it, so deleting the
organisation's last policy in the editor leaves an empty one (next
revision): the phones then drop the locks they had.

## 9. Pairing

1. On the web (Profile → Phones → "Associa un telefono") the server makes a
   one-time code: 160 random bits, valid 10 minutes, shown as a QR code:

   `activitytrack://pair?v=1&u=https%3A%2F%2Fat.example.com&c=<code>&f=<sha256 of the server's public key>`

   `f` is present only when the server's certificate is not trusted by
   the system (self-signed): the app then pins that key.
2. The app (Settings → Sync → "Associa a un server") reads the QR code with
   the camera (ZXing; no Google services needed) or accepts the link
   pasted by hand.
3. `POST api/v1/pair { code, deviceName, model, appVersion }` →
   `{ deviceId, token, user, org }`. The token (256 random bits) is stored
   hashed on the server and in the app's `SecretStore` on the phone.
4. The page that showed the QR code reports the paired phone; the admin
   and the user see the phones and can revoke them (the token stops working
   at once; the phone keeps its data and shows "associazione revocata").

## 10. When the phone syncs

- a few seconds after any local change (WorkManager, network required);
- when the app comes to the foreground, then `sync/head` every 30 s while it
  stays there;
- every 15 minutes in the background (WorkManager's minimum; a run missed
  without network goes as soon as the network is back).

No push (owner, 2026-10-30): no Firebase or other wake-up service, as the
app depends on no Google service (app A4). A change made on the web or on
another phone reaches a phone within 15 minutes, at once while the app is
open.

## 11. Conflicts that need a person

Most conflicts are settled field by field (§3). Two cases are kept and
shown instead (web: banner on the entry; app: notification):

- **two running activities** of one user (one started on the web while the
  phone was offline and started another): both kept; the phone applies
  its usual rule for a second start (`features.concurrentEntries`);
- **segments overlapping** after a merge: both kept, the entry is marked
  "da verificare". Commands on it keep working; the mark goes when the
  segments no longer overlap (phone or web). With the times locked by the
  policy only an admin or the owner's manager fixes them, on the web.

## 12. Versions and compatibility

Rules (owner, 2026-10-31), from release 1.0 on:

1. Within a major release, app and server stay compatible with the **two
   previous versions** of each other. A version is a minor release
   (1.3 → 1.4); a patch (1.3.0 → 1.3.1) never changes what travels.
2. When keeping compatibility is too costly, a **major** release; and even
   then, in this order: keep compatibility if it is not too penalising;
   else a transition update of the old version that speaks the new one;
   else both are updated together.
3. The app checks GitHub for newer releases (§12.4); an app installed from
   a store leaves it to the store.

### 12.1 The wire schema

An integer, independent of the version numbers: it grows only when what
travels changes (a type, a field, its meaning, a rule of the engine). A
release that does not touch the sync keeps it. Today: **schema 1** (since
0.9.0; the 0.9.x releases are compatible, not by rule).

The server speaks its schema and the **two before it** (`sync/compat.py`:
`SCHEMA`, `WINDOW`), so rule 1 holds and is wider when releases leave the
schema alone. Between schema *n* and *n + 1* a `Step` translates both ways:
`up` brings a change of an older app to the current schema, `down` brings
a record to the app's schema (pull, push answers, file uploads). CI tests
every step.

### 12.2 Headers and answers

Every request of the app names its version and schema:

```
X-ActivityTrack-App: 1.0.0
X-ActivityTrack-Schema: 1
```

An app without them (0.9.x) speaks schema 1. The server records the app's
version and schema of each phone at every request (the Phones and Users
pages show them). `pair` and `sync/head` answer, besides their fields,
`server` (its version), `schema` and `minSchema`.

Outside the window the server answers **426** with
`{"error": "app_too_old" | "server_too_old", "server", "schema", "minSchema"}`;
the app stops syncing and tells what to update. The data stay on the
phone and sync once the versions match again.

### 12.3 Writing a change

- A new field: optional or with a default, on both sides; an older peer
  ignores it (the server ignores unknown fields, the app too) and keeps
  the value it does not know (a push carries only the fields it changed).
- A new type: an older app skips it; an older server refuses it per
  change (`unknown_type`, shown in the app's "Non sincronizzati").
- Removing or renaming a field, changing its meaning or a rule: a new
  schema with its `Step`.
- New endpoints: the app treats a 404 as "server too old" for that
  feature and goes on with the rest.
- Policy keys: the app refuses a policy with a key it does not know (and
  keeps the previous one). The catalog's entries carry `since` (the app
  version that knows them); the policy editor warns of the paired phones
  whose app is older.

### 12.4 Update check of the app

Installed from GitHub (no store as installer), the app checks the public
releases of `zipclaude/activitytrack` once a day: a newer **stable**
release, or a newer **pre-release** when the user subscribed to the betas
(Settings; a policy can fix both), shows in Settings and as a notification
that opens the release page. Betas are published as GitHub pre-releases.
