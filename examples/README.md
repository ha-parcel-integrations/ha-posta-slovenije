# Examples

Ready-to-paste Home Assistant snippets for the Pošta Slovenije integration.

| Folder | Contents |
|---|---|
| [`automations/`](automations/) | YAML automations — copy them into your `automations.yaml` or paste them into the Automation editor in **raw editor** mode. |
| [`dashboards/`](dashboards/) | Lovelace snippets, including [`add_parcel_card.yaml`](dashboards/add_parcel_card.yaml) — track a new parcel straight from a dashboard via the `posta_slovenije.track_parcel` service. |

The examples assume the tracking hub. Adjust entity IDs to match yours; an account entry's entities carry the account name.

**Feeding Pošta Slovenije from e-mail:** On the tracking hub every parcel must be registered by its tracking code before it can be tracked (a MojaPošta account finds its parcels by itself). [`automations/track_parcels_from_email.yaml`](automations/track_parcels_from_email.yaml) extracts tracking codes from incoming shipping mails (core IMAP integration + regex, with an optional AI fallback) and registers them automatically; setup guide and pitfalls in [`automations/track_parcels_from_email.md`](automations/track_parcels_from_email.md).

## Services

| Service | Description |
|---|---|
| `posta_slovenije.track_parcel` | Start tracking a parcel (`tracking_code`, optional `direction`: `incoming` by default, or `outgoing` for a parcel you sent). |
| `posta_slovenije.untrack_parcel` | Stop tracking a parcel (`tracking_code`). |

## Events used in the examples

The coordinator fires these on the HA event bus:

| Event | When | Payload |
|---|---|---|
| `posta_slovenije_parcel_registered` | A new parcel appears in the active list | The full normalised parcel dict |
| `posta_slovenije_parcel_status_changed` | A parcel's canonical status changes | Same, plus `old_status` / `new_status` |
| `posta_slovenije_parcel_delivered` | A parcel reaches the delivered status | The full normalised parcel dict (fires *instead of* `status_changed` on that final hop) |
| `posta_slovenije_outgoing_parcel_status_changed` / `posta_slovenije_outgoing_parcel_delivered` | A parcel you sent changes status / is delivered | Same as the incoming pair; there is no `registered` or delivery-time event for outgoing |
| `posta_slovenije_parcel_delivery_time_changed` | A parcel's expected delivery time changes (Pošta Slovenije reports none, so this never fires) | Same, plus `old_planned_from` / `new_planned_from` / `old_planned_to` / `new_planned_to` |

Events are suppressed on the first refresh after start-up.
