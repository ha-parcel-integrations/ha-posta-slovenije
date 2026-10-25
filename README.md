# Pošta Slovenije Parcel Tracker

[![Release](https://img.shields.io/github/v/release/ha-parcel-integrations/ha-posta-slovenije.svg)](https://github.com/ha-parcel-integrations/ha-posta-slovenije/releases)
[![Downloads](https://img.shields.io/github/downloads/ha-parcel-integrations/ha-posta-slovenije/total.svg)](https://github.com/ha-parcel-integrations/ha-posta-slovenije/releases)
[![HACS](https://img.shields.io/badge/HACS-Custom-41BDF5.svg)](https://github.com/hacs/integration)
[![License](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)

> 📦 Part of [**ha-parcel-integrations**](https://ha-parcel-integrations.github.io/), a family of package-tracking integrations for Home Assistant that all publish the same parcel format, statuses and events, so they plug into cross-carrier automations and the optional [Parcel Aggregator](https://github.com/ha-parcel-integrations/ha-parcel-aggregator). Browse the other carriers on the website.
>
> 💬 Questions or feedback? Join the discussion on the [Home Assistant community](https://community.home-assistant.io/t/packages-postnl-dhl-nl-dpd-and-gls-parcel-integration/112433/).

A custom Home Assistant integration that tracks your [Pošta Slovenije](https://www.posta.si) parcels in Slovenia. Two ways to set it up:

- **MojaPošta account** — sign in once and the parcels on your account are found for you, the ones you send included, with the sender and receiver where the account reports them.
- **Tracking codes** — no account: you enter the tracking number (two letters, nine digits and two more letters — Slovenian codes end in `SI`) yourself.

> **Known gaps.** Pošta Slovenije reports no expected delivery window, weight or dimensions, so the calendar and the next-delivery sensor stay empty. Some of the status codes are not seen yet (parcel lockers, post-office pickup and export scans); a parcel that reports one logs a warning with a link to report it and may show an earlier status or `unknown`. The MojaPošta account route has not been tried against a real parcel yet, and no parcel you sent has been seen: the first one may log warnings asking you to report what the inbox looks like. The integration uses unofficial routes that can change, so it can stop working until an update ships.

## Contents

- [Features](#features)
- [Requirements](#requirements)
- [Installation](#installation)
- [Configuration](#configuration)
- [Options](#options)
- [Removal](#removal)
- [Sensors](#sensors)
- [Parcel status reference](#parcel-status-reference)
- [Events](#events)
- [Services](#services)
- [Examples](#examples)
- [Debugging](#debugging)
- [Troubleshooting](#troubleshooting)
- [Related integrations](#related-integrations)
- [Disclaimer](#disclaimer)
- [Contributing](#contributing)
- [License](#license)

## Features

- Sign in with your MojaPošta account and have every parcel on it found automatically, incoming and sent
- Or track any number of Pošta Slovenije parcels by tracking code — no account needed
- Per-parcel sensor with the canonical status (`registered` / `in_transit` / `out_for_delivery` / `at_pickup_point` / `delivered` / `returning` / `problem`), the carrier's own status text, the post office to collect from and a tracking deep-link
- Summary sensors: incoming parcels, parcels awaiting pickup, next delivery, recently delivered parcels, and the parcels you sent with the ones already delivered
- Read-only **Deliveries** calendar (stays empty: Pošta Slovenije reports no delivery window)
- `posta_slovenije.track_parcel` / `posta_slovenije.untrack_parcel` services, so a dashboard button can add a parcel
- Events + device triggers for no-code automations (parcel registered, status changed, delivered, delivery time changed, and status changed / delivered for parcels you sent)
- Opt-in per-parcel status history
- Manual refresh button and a diagnostic last-update sensor

## Requirements

- Home Assistant 2025.12 or newer
- Either a MojaPošta account (the username and password you use on the
  MojaPošta website or app), or a Pošta Slovenije parcel and its tracking
  number (from the shipping confirmation or the sender)

## Installation

### HACS (recommended)

Click the button below to open this repository in HACS, then install **Pošta Slovenije**:

[![Open your Home Assistant instance and open a repository inside the Home Assistant Community Store.](https://my.home-assistant.io/badges/hacs_repository.svg)](https://my.home-assistant.io/redirect/hacs_repository/?owner=ha-parcel-integrations&repository=ha-posta-slovenije&category=integration)

Or add it by hand: in HACS, choose the three-dot menu → **Custom repositories**, add `https://github.com/ha-parcel-integrations/ha-posta-slovenije` as an **Integration** and install **Pošta Slovenije**.

Restart Home Assistant if HACS asks you to.

### Manual

Copy `custom_components/posta_slovenije` into your `config/custom_components/` folder and restart Home Assistant.

## Configuration

[![Open your Home Assistant instance and start setting up a new integration.](https://my.home-assistant.io/badges/config_flow_start.svg)](https://my.home-assistant.io/redirect/config_flow_start/?domain=posta_slovenije)

Add the integration via **Settings → Devices & Services → Add Integration → Pošta Slovenije** and pick one:

- **MojaPošta account** — enter the username and password you use for MojaPošta. The password is stored so the integration can sign in again on its own when its session ends; you are only asked again if MojaPošta stops accepting it. You can add more than one account.
- **Tracking codes** — there is nothing to fill in: the hub is created immediately. One hub holds all your codes.

With tracking codes, add parcels via the integration's **Configure** dialog, the [`posta_slovenije.track_parcel`](#services) service, or a [dashboard button](examples/dashboards/add_parcel_card.yaml). The tracking number is on your shipping confirmation or the missed-delivery notice. Spaces are ignored and the number is checked against the UPU check digit, so a mistyped one is rejected straight away.

### Incoming and outgoing parcels

With a MojaPošta account the direction comes from the account: parcels you are receiving and parcels you sent are listed apart, and you are never asked. With tracking codes the lookup cannot tell a parcel you sent from one you are receiving, so you say which it is: **Configure** has an **Incoming parcels** and an **Outgoing parcels** list, and `posta_slovenije.track_parcel` takes a `direction`. Filing a code in the other list (or calling the service again with the other direction) moves it.

## Options

Open **Configure** on the integration entry:

| Section | Option | Default | Description |
|---|---|---|---|
| Incoming parcels / Outgoing parcels | Add / remove | — | Tracking codes only: manage the codes you are receiving and the ones you sent, in a list each. Changes apply immediately, no restart. |
| Delivered parcels | Filter by / amount | last 7 days | How long delivered parcels stay visible on the delivered sensor. |
| Parcel history | Include status history | off | Adds a `history` attribute per parcel with each status update. |

Polling isn't one of these settings: the integration polls on a dynamic,
status-driven schedule with nothing to configure.

## Dynamic polling

Polling isn't a setting here — the integration adjusts its own cadence to
what your tracked parcels are actually doing:

- **Quiet hours** — no polling between 00:00–06:00 local time, aside from one
  catch-up check at each end of that window (around midnight and around 6
  AM), so an overnight update is never missed.
- **Hot (every 15 minutes)** — while any tracked parcel is out for delivery
  today, starting an hour before its delivery window opens (or immediately if
  no window is known yet).
- **Normal (every 45 minutes)** — for anything else still on its way.
- **Fully paused** (tracking codes only) — once every tracked parcel has been
  delivered, or nothing is tracked at all, polling stops until you add a
  parcel back (adding one always triggers an immediate check, regardless of
  the pause). An account keeps polling on the normal cadence, since that is
  how new parcels on it are found.
- A small, fixed per-hub offset is added on top, so not every Pošta Slovenije
  hub out there polls at exactly the same second.

Pošta Slovenije reports no expected delivery window, so a parcel that is out for delivery is polled on the hot cadence until it is delivered. An account reads each parcel's history only when the parcel changed, and at least every three hours while it is still on its way.

## Removal

Standard HA removal applies: **Settings → Devices & Services → Pošta Slovenije → ⋮ → Delete**. Nothing is stored on Pošta Slovenije's side. Removing an account entry also deletes the stored password.

## Sensors

| Entity | Description |
|---|---|
| `sensor.posta_slovenije_incoming_parcels` | Number of active tracked parcels, full list under the `parcels` attribute |
| `sensor.posta_slovenije_parcel_<code>` | One per tracked parcel; state is the canonical status, attributes carry the full normalised parcel |
| `sensor.posta_slovenije_next_delivery` | Earliest expected delivery; stays empty because Pošta Slovenije reports none |
| `sensor.posta_slovenije_awaiting_pickup` | Parcels waiting at a post office |
| `sensor.posta_slovenije_delivered_parcels` | Recently delivered parcels (see the retention option) |
| `sensor.posta_slovenije_outgoing_parcels` | Number of active parcels you sent, full list under the `parcels` attribute |
| `sensor.posta_slovenije_outgoing_delivered_parcels` | Parcels you sent that have since been delivered (see the retention option) |
| `sensor.posta_slovenije_last_successful_update` | Diagnostic: when Pošta Slovenije was last polled successfully |

An account entry's entities carry the account in their names, for example `sensor.posta_slovenije_<account>_incoming_parcels`. A delivered parcel moves from its per-parcel sensor to the delivered sensor automatically.

## Parcel status reference

The `status` field is the carrier-agnostic enum shared by the whole integration family:

| Status | Meaning |
|---|---|
| `registered` | Pošta Slovenije has the item's information |
| `in_transit` | Arrived at Pošta Slovenije or leaving a sorting office |
| `out_for_delivery` | Expected to be delivered today, or on its way to the chosen parcel locker |
| `at_pickup_point` | Waiting for you at a post office |
| `delivered` | Delivered to the mailbox or the door |
| `returning` | Being returned to the sender, or already back with them |
| `problem` | A delivery attempt failed |
| `unknown` | A status we have not mapped yet |

A failed attempt is a `problem` unless Pošta Slovenije says the item goes back to the sender, in which case it is `returning` and stays that way even when the item is then "delivered" to the sender.

The carrier's own human-readable text is always available as `raw_status`.

## Events

The integration fires these on the event bus (also available as device triggers on the Pošta Slovenije device):

| Event | When |
|---|---|
| `posta_slovenije_parcel_registered` | A new parcel appears in the active list |
| `posta_slovenije_parcel_status_changed` | A parcel's canonical status changes (`old_status` / `new_status` in the payload), except the final hop to delivered |
| `posta_slovenije_parcel_delivered` | A parcel is delivered |
| `posta_slovenije_outgoing_parcel_status_changed` | A parcel you sent changes status, except the final hop to delivered |
| `posta_slovenije_outgoing_parcel_delivered` | A parcel you sent is delivered |
| `posta_slovenije_parcel_delivery_time_changed` | The expected delivery window changes (Pošta Slovenije reports none, so this never fires) |

Every payload is the full normalised parcel plus the entry's `device_id`. Events are suppressed on the first refresh after start-up. There is no `registered` event for a parcel you sent: you already know you handed it over.

## Services

| Service | Fields | Description |
|---|---|---|
| `posta_slovenije.track_parcel` | `tracking_code`, `direction` | Start tracking a parcel on the tracking hub. `direction` is `incoming` (default) or `outgoing`; calling it again with the other direction moves an already tracked parcel |
| `posta_slovenije.untrack_parcel` | `tracking_code` | Stop tracking a parcel on the tracking hub |

An account entry finds its parcels itself, so the services only act on the tracking hub.

## Examples

Ready-to-paste automations and dashboard snippets live in [`examples/`](examples/), including tracking a new parcel straight from a dashboard.

### Community Lovelace cards

Third-party cards that work with this integration's sensors:

- [jonisnet/hki-parcels-card](https://github.com/jonisnet/hki-parcels-card)
- [klaptafel/ha-package-tracker-card](https://github.com/klaptafel/ha-package-tracker-card)

## Debugging

```yaml
logger:
  logs:
    custom_components.posta_slovenije: debug
```

## Troubleshooting

- **A parcel shows `unknown`** — Pošta Slovenije does not know the number (yet), or the record is too old: old parcels disappear from the lookup, and a parcel that was found before keeps its last known status. It will pick up automatically once Pošta Slovenije has it.
- **MojaPošta asks for the password again** — the stored password stopped working, usually because it was changed. Enter the new one when Home Assistant asks.
- **A status logs "Unrecognised Pošta Slovenije status"** — please [open an issue](https://github.com/ha-parcel-integrations/ha-posta-slovenije/issues/new?template=unrecognised_status.yml) with the logged line so the mapping can be extended. The same goes for any other warning that carries that link.

## Related integrations

This integration is part of [**ha-parcel-integrations**](https://ha-parcel-integrations.github.io/) — a family of
parcel-carrier integrations that all publish the same canonical parcel format,
statuses and events.

- [**Parcel Aggregator**](https://github.com/ha-parcel-integrations/ha-parcel-aggregator) rolls every installed carrier
  up into one set of sensors.
- Browse [the organisation](https://ha-parcel-integrations.github.io/) for the current list of supported carriers.

## Disclaimer

This is an independent, community-built project. It is not affiliated with, endorsed by, sponsored by, or supported by Pošta Slovenije, Home Assistant, or any other third party referenced in this project. Please don't contact Pošta Slovenije for support with this integration.

All third-party trademarks, trade names, product names, logos, and other brand assets are the property of their respective owners. References to them are solely to identify the relevant carrier or service and do not imply affiliation, sponsorship, or endorsement. Nothing in this project grants or implies any licence or right to use third-party brand assets.

This integration may rely on public, unofficial, or undocumented carrier interfaces, accessed with your own account or API key where required. These may change or be withdrawn without notice and may be subject to Pošta Slovenije's terms. Data is sent only to Pošta Slovenije's own services or those of its group; this project operates no servers of its own. You are responsible for ensuring that your use complies with applicable law and those terms. Use is at your own risk; see the [licence](LICENSE) for warranty limitations.

With tracking codes the integration uses the same lookup as the MojaPošta app and website; with an account it signs in the way the app does, with your own username and password.

## Contributing

Pull requests and issues are welcome. Please open an issue before
submitting a large change.

## License

[MIT](LICENSE)
