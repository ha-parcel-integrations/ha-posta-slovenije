# Working in this repository

Home Assistant custom integration for **Pošta Slovenije** parcel tracking.
Distributed via HACS; not part of HA core. One carrier in the
[ha-parcel-integrations](https://github.com/ha-parcel-integrations) suite,
**generated from ha-carrier-template** — everything outside *Carrier-specific
notes* is suite-wide; when in doubt check the template or a sibling repo.
No DTO layer.

API mechanics — endpoints, parameters, status vocabularies — live in the
private `carrier-research/posta-slovenije/api/` and are **never** copied here.

## Shared conventions — fetch when relevant

Suite-wide rules live in
[`.github/CONVENTIONS.md`](https://github.com/ha-parcel-integrations/.github/blob/main/CONVENTIONS.md)
and are **not** repeated here. Don't fetch it every session — fetch it **before**
you act in one of these areas:

| Before you … | Fetch `CONVENTIONS.md` § |
|---|---|
| touch entities, sensors, config/options flow, coordinator, diagnostics, translations | *Home Assistant developer docs* (its table points on to the canonical HA page — don't rely on memory) |
| add/rename a parcel field, a `ParcelStatus`, or a bus event; change the sort/first-refresh; touch unmapped-status logging | *Parcel contract* — exact key set, units, sort, events + suppression; `test_parcels.py::test_normalize_publishes_exactly_the_canonical_keys` guards the key set |
| change which optional field this carrier populates vs. always returns `None` | Update `const.py`'s `CAPABILITIES` in the same commit — it feeds the comparison table on the docs site, so a field that starts (or stops) coming back non-null and isn't reflected there is a wrong claim on the website, not just a stale comment. If this carrier has more than one backend (a country-specific transport, not just a config option) with genuinely different field support, `CAPABILITIES` should be a `CAPABILITIES_BY_VARIANT` dict instead — one frozenset per backend, so a field only some backends populate doesn't get silently intersected away or overclaimed for the rest. A field you could not verify (no real parcel yet) goes in `PENDING_CAPABILITIES` instead of being left out — the site then says "awaiting data" rather than claiming the API never exposes it; move it into `CAPABILITIES` once confirmed |
| ship anything while below 1.0.0 (unconfirmed data) | *Pre-1.0 releases* — one-shot WARNINGs for every guessed shape/code |
| consider "fixing" a lint/pattern the skill flags (poll interval, inline client, sync requests) | *Deliberate skill divergences* — likely intentional, don't re-flag |
| commit, bump, tag, release, or write release notes; add a feature without a test | *Workflow / Commits / Versioning / Testing* |

**Suite-wide tripwires, kept inline on purpose:**
- **First refresh in `__init__.py`, before `async_forward_entry_setups`** — from
  a forwarded platform HA can't catch `ConfigEntryNotReady` and half-sets-up the
  entry. Runtime-only; tests don't catch a regression.
- **Setup stale-entity sweep is scoped to `domain == "sensor"` and skips
  `non_parcel_unique_ids`** — else it deletes the refresh button / the
  summary+diagnostic sensors. Add a new non-parcel sensor's unique_id to the set.
- **Per-parcel sensors are removed by the summary sensor** via
  `entity_registry.async_remove` (self-removal races and leaves ghosts).
- **The optional pickup-point summaries are a pair.** A carrier that can tell
  a parcel is destined for a pickup point exposes
  `en_route_to_pickup_point` for `pickup is true` before it arrives; one that
  can reach `ParcelStatus.AT_PICKUP_POINT` also exposes `awaiting_pickup`.
  See *Parcel contract* in `CONVENTIONS.md`. Say "pickup point", not
  "ServicePoint"/"parcel shop"/"locker", for the generic concept; the
  example carrier demonstrates both canonical sensors.

## Carrier-specific notes

**API mechanics live in `carrier-research/posta-slovenije/api/` (private research
repo)** — endpoints, the sign-in exchange, payload shapes and the status
vocabulary. Do not duplicate them here; this section is integration-level
decisions and tripwires only.

**Two sources in one domain — `tracking/` and `account/` packages.** An
entry's source is `entry.data[CONF_SOURCE]` (`tracking` / `account`); an entry
without one is a tracking hub, so **every** read defaults to `SOURCE_TRACKING`
(`source_of()` in `parcels.py` — not `__init__.py`, which imports
`services.py` and so cannot host a function it needs). The setup flow opens on a menu picking one.
Each package owns its client, coordinator subclass and normaliser; `account/`
also owns the token lifecycle (`auth.py`). The domain root holds only
dispatch (`__init__.py`, `config_flow.py`, `services.py`) and what both share:
- `parcels.py` — the **status map and the scan-row normalizer**. Both sources
  return the same per-scan rows, so each source only shapes what surrounds
  them (`normalize_scans` takes the barcode, rows, parties and `raw`).
- `coordinator.py` — `PostaSlovenijeBaseCoordinator`: cadence tiers, stagger,
  429 backoff, the event contract and the publish step. Subclasses implement
  `_async_poll`; an account sets `_stops_when_idle = False`.
- `transport.py` — capped body read, redirects never followed, edge block-page
  detection, the shared error classes and the client-guard refusal.
- One tracking hub per install (unique id `DOMAIN`) holds all of a
  household's codes; accounts are one entry each, any number. **No
  `single_config_entry`** — that is why.
- `track_parcel` (with its `direction`) / `untrack_parcel` act on the tracking hub only; an
  account-only install registers no services, and they leave with the last
  loaded tracking hub.
- An account entry has no parcel list: its options go straight to settings and
  reload on submit (no update listener); the hub keeps its listener.

**Status comes from the scan code, never from a summary.** Keyed on each
row's `code` with two sequence rules: a failed attempt (`V`) whose next step
sends the item back is `returning` (any other `V` is a `problem`), and a
delivery after it is `returning`. The delivery scans are `VL` (into the
mailbox) and `VR` (handed over in person) — both carry subCode `VRPIS` and the
*Dostavljena* bucket, both mean delivered, and both stamp `delivered_at`. The back-at-sender scan (`VRP`)
carries the *delivered* bucket in the row's own `statusId`/`statusName` — never
map on those, nor on the parcel-level summary, which lags the history.
`N` is `out_for_delivery` by code even on the way back. Unmapped code: newest
mappable earlier scan, then `unknown`, one-shot WARNING either way. A `V` is
`returning` only when `nondeliveryActionEN` says the item goes back to the
sender; the carrier fills its English fields unevenly, so an empty or
unmatched one is a `problem` **plus** a one-shot WARNING naming the field and
`nondeliveryId`, never a silent downgrade. Several
unmapped-shape warnings (new keys, a non-zero `canceled`, an unreadable scan
clock, a number mismatch, the client guard refusing us, a block page) are
one-shot too and name keys and codes only.

**Pre-1.0 — what is not confirmed.** Nine codes were seen on real parcels
(`VR` was added 2026-10-25 from a real parcel that logged it as unrecognised);
`EMC`, `VLPAKP`, `POTPKTM`, `IZR` and the pickup variant of `V` are known only
from a third-party adapter, and foreign-post scans are unseen. Each of the five
**reports itself once even though it maps** (`_UNCONFIRMED_CODES`, and the `V`
branch warns when `nondeliveryActionEN` is empty or says nothing about a
return), so a real parcel carrying one produces a log line to report rather
than a silent guess. Move a code out of that set once a real parcel confirms
it, the way `VR` was.

**Account source: payloads are partly unverified (tripwire).** The account was
built without a real login, from the app's own routes. A real sign-in on
2026-10-25 confirmed the token exchange and the **inbox envelope**:
`result.items` and `result.totalCount` are as assumed, alongside `pageNumber`,
`pageSize` and `totalPages` (the three that first logged as unrecognised).
That inbox was empty, so **an inbox row, the per-parcel detail and the
delivered/pickup fields are still unseen**. Every field shape in `account/` is
an assumption and is read defensively; the first real login must confirm them
(inbox row keys, whether a row carries `statuses`, the detail envelope, token
lifetimes, whether a refresh token is issued, and that the API accepts the
sign-in's token). Until then the account's `pickup_point` and `history` sit in
`PENDING_CAPABILITIES_BY_VARIANT`, and any new key warns once. Do not move
them, or release 1.0.0, before a real account confirms the shapes.
- **Reauth only for a rejected password.** A `401` is answered with one token
  refresh, then one fresh sign-in from the stored password; a third `401`
  raises `ConfigEntryAuthFailed` once. A refused app login, a client-guard
  refusal or any outage is a transport error that keeps last-good, never a
  reauth. The password is stored (the sign-in falls back to it); tokens are
  rewritten into `entry.data` as they rotate.
- **A poll uses one token.** The coordinator calls `async_prepare()` before
  reading, so expiry is handled once, not inside the per-parcel loop.
- **Detail is read per parcel, only when needed:** when the inbox row's
  summary stamp moved, when nothing is stored yet, and for a parcel still on
  its way every three hours — because the summary can lag the scans, a stamp
  that never moves must not freeze a parcel. A delivered parcel is never read
  again. A detail that fails keeps last-good; a parcel with neither detail nor
  scans yet is left out until it has one. An incomplete inbox fails the poll
  rather than dropping parcels that would later be announced as new.
- **Archived rows are not tracked** (`isArchived`); a parcel that leaves the
  list simply disappears.
- **Outgoing parcels, direction by source.** The account reads two lists, one
  route per direction, and takes each parcel's direction from
  `isOutgoingParcel`. The only response seen on the wire carries that field on
  the **detail's `result`, beside `parcel` rather than inside it**, so the
  detail is asked first and the inbox row second; the list a parcel arrived in
  is only the third fallback, and the direction is therefore resolved where
  the detail is in hand, not while paging the list. A flag that contradicts
  its list logs a one-shot WARNING (the field name only) and the flag wins;
  neither place carrying it takes the list's direction (also one WARNING); a
  flag that is neither true nor false counts as incoming (one WARNING). A parcel listed twice is shown once. The user is
  never asked. **Outgoing rows have never been seen on the wire** (the sent
  list answered empty on the maintainer's account), so everything above is as
  unconfirmed as the incoming rows: the first real sent parcel must confirm the
  flag, the list route and that its scans read the same.
  The tracking hub cannot know: the guest lookup carries no party and the scans
  never say who is sending, so the **user declares the direction per parcel**
  (`CONF_DIRECTION` on the `CONF_PARCELS` dicts, default incoming so older
  entries need no migration; an `Incoming parcels` and an `Outgoing parcels`
  options entry opening the same list editor; a `direction` field on
  `track_parcel`). Re-filing a code under the other direction, in the options
  or by calling the service again, **moves** it, with no duplicate error.
  Never infer direction from scan wording.
  Above the split it is the suite's normal outgoing shape, implemented once in
  the base coordinator: `outgoing` / `delivered_outgoing`, the
  `outgoing_parcels` and `outgoing_delivered_parcels` sensors, the
  `_outgoing_parcel_status_changed` / `_outgoing_parcel_delivered` pair with no
  `registered` and no delivery-time event, per-parcel sensors for both
  directions, and `awaiting_pickup`, `next_delivery` and the calendar staying
  incoming-only. An outgoing parcel counts for the cadence tier and keeps a
  tracking hub polling.

**Tracking-code validation is stricter than the suite default** (see
*Tracking-code validation* below): the hub accepts only a UPU S10 number —
two letters, nine digits, two letters — with a valid check digit, after
trimming, removing spaces and upper-casing. **Any** two trailing letters pass,
not only `SI`, so the user-facing copy says exactly that and names the check
digit: a foreign S10 code must not be rejected with a message about Slovenia.
That is a build-plan decision for this carrier, not drift; real codes verify.

**`raw` is shaped per source.** A tracking parcel's `raw` is the whole
response envelope; an account parcel's is `{"inbox": …, "detail": …}`. One
domain serves both, so an automation reading `raw` has to know which entry it
came from. `sender` and `receiver` on the account come from `senderName` /
`receiverName`, both still guesses: a parcel with neither logs one WARNING
rather than quietly publishing two empty fields.

**Fields that are `None` on purpose:** `planned_from`/`planned_to` (no ETA on
either source), `weight`, `dimensions`, and on the tracking hub `sender` and
`receiver` (withheld from a guest). `pickup` is true only while the parcel
waits at a post office; there is no `en_route_to_pickup_point` sensor because
nothing is known about a pickup point before arrival. Because there is no ETA,
`next_delivery`, the calendar and `delivery_time_changed` stay empty and an
out-for-delivery parcel is polled on the hot tier until delivered.
`awaiting_pickup` exists (`IZR`/`PVI` reach `at_pickup_point`).

**Privacy.** A delivered or returned scan carries the signer's real name, scans
carry free-text notes, and an account's rows carry sender, receiver and the
parcel's pickup QR code and PIN (a bearer credential for collecting it).
None of it is read into any field, entity or log line. `raw` keeps the whole
record per the suite rule, so it travels with the parcel into attributes and
bus events; only diagnostics redact it (names, notes, ids, pickup codes, every
`sender*`/`receiver*` field, the username and both tokens). Never log a
username, password or token at any level.

## Options and reloads

For code-based carriers, the options flow starts with exactly `Parcels` and
`Settings` — or, where the carrier supports outgoing parcels, `Incoming
parcels` / `Outgoing parcels` / `Settings` (see the next section).
`Parcels` is one editable multi-code list; `Settings` is
a flat form — some carriers use one sectioned form
(`data_entry_flow.section`) instead; both are generator variants, not carrier
decisions. Changes apply without a restart. Two models, **do not mix them**:
- **Account-less carriers** (the default, and the `--auth byo-key` build) apply
  changes live: an update listener calls `async_request_refresh()`, so
  added/removed parcel sensors appear immediately (this is also the resume
  path after polling has fully suspended — see "Dynamic polling" below).
- **Account-based carriers** call `async_schedule_reload` on submit and register
  **no** update listener. Combining a listener with a reload-on-update flow is
  deprecated, an error in HA 2026.12+.

## Auth models

Three `--auth` builds, one axis: **what the config flow asks for and
validates**, not how tracking works. `none` and `byo-key` both key tracking on
codes the user types in (`Parcels`/`Settings` options, `track_parcel` /
`untrack_parcel` services, the account-less coordinator and its full-stop /
delivered-skip behaviour below) — `byo-key` only adds a required key field
validated against the carrier's **official** API at setup, `CONF_API_KEY` in
`entry.data`, and a reauth flow for when the key is rotated or revoked outside
Home Assistant (`PostaSlovenijeAuthError` from any per-parcel fetch raises
`ConfigEntryAuthFailed` for the whole poll — one credential covers every
tracked code, so a rejected key is never treated as one parcel's problem).
`credentials` is the only one that changes the *tracking* model too (an
account feed, not user-entered codes) — see the split above.

Reach for `byo-key` only when carrier-research has already established that
the carrier's *unauthenticated/consumer* surface is unusable (bot-walled,
requires a session a script can't hold) and that the *official* developer key
is reachable by a private individual — `key_access: consumer` in the research
doc's front matter, not `business`. A `business`-gated key is a wall, not a
BYO key, whatever the portal's own copy claims (see
`carrier-research/CLAUDE.md`'s "Standing rulings").

## Incoming and outgoing parcels

**Add outgoing support whenever the carrier lets a consumer send a parcel** —
a C2C shipment, a locker drop-off, a marketplace or returns label. It is not
an optional extra to bolt on later: without it a parcel the user sent counts
towards `incoming_active` and sits on their dashboard next to the ones they
are waiting for. A carrier that genuinely has no consumer-sending surface is
exempt — say so in *Carrier-specific notes* so the gap reads as a decision.

Where the direction comes from has exactly two answers, and which one applies
follows from the carrier, not from taste:

- **Account-based** — the account feed distinguishes them, so derive it and
  never ask the user. A shipment matching neither side logs a one-shot
  warning and defaults to incoming rather than disappearing from every list.
  References: `ha-ppl-cz` (one call, split on a field), `ha-dhl-nl` (a
  separate "sent" endpoint).
- **Account-less** — the payload cannot reveal it (the user's own parcel and
  a stranger's look alike, and there is no account identity to compare a
  party against), so the **user declares it per parcel**: two menu entries in
  the options flow, each the same multi-code list, plus a `direction` field on
  `track_parcel`. Store it as `CONF_DIRECTION` on the `CONF_PARCELS` dicts,
  defaulting to incoming, so entries written before the option existed need no
  migration. Re-filing a code under the other direction moves it instead of
  erroring — that is the correction path. Reference: `ha-packeta`.
  **Never infer direction from free-text event wording**: a handover sentence
  that happens to name the drop-off point is not a structured field, says
  nothing before handover, and silently ties the split to one locale.

Above that split the shape is identical either way, and is suite-wide:
`coordinator.outgoing` / `coordinator.delivered_outgoing` alongside `data` /
`delivered`; `outgoing_parcels` + `outgoing_delivered_parcels` summary
sensors (their unique_ids belong in `non_parcel_unique_ids`); per-parcel
sensors spawned for **both** directions; the
`<domain>_outgoing_parcel_status_changed` / `_outgoing_parcel_delivered`
event pair, with **no** `registered` and no delivery-time event for outgoing;
`awaiting_pickup`, `next_delivery` and the calendar staying incoming-only;
and both new lists in `diagnostics.py`. The aggregator needs no change — it
buckets on the sensor suffix and the event prefix. Also set
`directions: incoming+outgoing` for the carrier in the docs site's
`data/carriers.yml`.

## Tracking-code validation

*This carrier is the exception: see the S10 note under* Carrier-specific notes.
In code-based carriers (account-based sources have no code entry),
`valid_tracking_code` in `config_flow.py` accepts every non-empty code — no
format regex. This is a suite-wide convention, not a per-carrier TODO: real
tracking-number formats vary too much across carriers, and are often not
fully confirmed even for this one, to gate on a guessed shape. A too-strict
regex risks rejecting a genuinely valid code; an actually-bad code just comes
back "not found" on the next poll, which is a far cheaper failure mode. Do
not add one back in, even once the format is confirmed.

## Dynamic polling

There is no user-facing polling interval — this is a deliberate suite-wide
choice, not a gap. `coordinator.py`'s `_hottest_tier_minutes` /
`_next_update_interval` recompute `update_interval` at the end of every
refresh. The reference carrier in `ha-carrier-template` (its `coordinator.py`)
is the canonical implementation every carrier mirrors; the design rationale (quiet window, tiers, stagger,
backoff, delivered-skip) is spelled out below.

- **Quiet window:** no polling 00:00–06:00 local time, except two daily
  anchors (~00:00 and ~06:00) for overnight / end-of-day catch-up.
- **Tiers while polling:** *hot* (15 min) when a tracked, not-yet-delivered
  parcel is `out_for_delivery` within an hour of its `planned_from` (or has no
  `planned_from` at all); *mid* (45 min) for anything else still in flight —
  `problem`/`returning` included, deliberately not hot. Account-based carriers
  never fully stop even with nothing hot or in transit: the mid-tier poll is
  also how a new shipment gets discovered.
- **Full stop (account-less carriers only):** `update_interval = None` when
  nothing is tracked or every tracked parcel is delivered. Resumes the moment
  a parcel is added back, via the options-flow refresh above.
- **Failed polls:** `_async_update_data` wraps `_async_poll` and recomputes `update_interval` on any failure (never to `None`, falling back to the mid tier), so a failure after the overnight poll retries at the daytime cadence instead of waiting out the quiet window.
- **Stagger:** a small, stable per-install offset (hash of the config entry
  id) is added to every computed interval so installs don't all hit an anchor
  or tier boundary at the same second.
- **429 backoff:** a 429 anywhere in a poll raises `UpdateFailed` with
  `retry_after` — the carrier's own `Retry-After` header if present, otherwise
  an exponential backoff tracked per-coordinator. `api.py`'s
  `…ApiError.status_code` / `.retry_after` carry this from the HTTP layer.
- **Delivered codes are skipped from the fetch (account-less carriers only):**
  once a tracking code's payload comes back `delivered`, `coordinator.py`
  excludes it from the next cycle's fetch — its payload can never change
  again. `self._delivered_codes` (keyed on the tracking code, not the barcode)
  is rebuilt from each cycle's results and intersected with the tracked set on
  untrack. The code stays in the options list, keeps its sensor and its
  cached payload, and still shows under the retention window — it just costs
  no more requests. `coordinator.delivered_codes` surfaces the count in
  diagnostics. Account-based carriers have nothing to skip here — one account
  call already returns everything, so they either have no `delivered_codes` or
  it is always empty.

A carrier that genuinely throttles or soft-bans traffic harder than the 429
backoff handles is a documented, local divergence from this in that one
repo's own `CLAUDE.md` — not a generator flag.

## Module layout

| File | Carrier-specific? |
|---|---|
| `api.py` (HTTP client, error types; `account/` and `tracking/` packages instead when a repo has two sources) | **yes** |
| `const.py` (domain, URLs, `ParcelStatus`, option keys) | partly (URLs) |
| `parcels.py` (status map, `normalize_parcel`, history, sort, filters — pure, no I/O) | partly (the status map, `normalize_parcel`) |
| `coordinator.py` (fetch, cache, event firing) | mostly not |
| `config_flow.py` | partly (code validation; key/credential validation on `--auth byo-key`/`credentials`) |
| `sensor.py` / `button.py` / `calendar.py` / `device_trigger.py` | no |
| `device.py` (shared device-info helper) | no |
| `diagnostics.py` | partly (`TO_REDACT`) |
| `services.py` (`track_parcel` / `untrack_parcel`, account-less only) | no |

`parcels.py` is deliberately free of I/O and HA objects so the per-carrier part
stays unit-testable without Home Assistant. Config: `ConfigEntry.runtime_data`
(typed, no `hass.data`), `PARALLEL_UPDATES = 0`, coordinator takes
`config_entry=entry`. `aiohttp.ClientError` is caught **per parcel** in the gather
loop (one bad parcel doesn't fail the poll) but **not** around the whole update
(the coordinator wraps that). Entities: `has_entity_name` + `translation_key`,
`icons.json`, translated units, `_attr_attribution`, `_unrecorded_attributes` on
anything with a parcel list or `raw`. Over-redact diagnostics — they get pasted
into public issues.

## Running tests

```
python -m pytest tests/ --cov=custom_components.posta_slovenije
```

Coverage must stay **above 95%** (silver `test-coverage` rule). Run before
committing. A code change updates the README + this file + `docs/` in the same
commit; the API reference lives in your own private research notes, never in
this repo.
