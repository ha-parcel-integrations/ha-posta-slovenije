"""Constants for the Pošta Slovenije parcel tracker integration."""

from enum import StrEnum

from homeassistant.const import Platform

DOMAIN = "posta_slovenije"


class ParcelStatus(StrEnum):
    """Carrier-agnostic parcel status.

    **Do not extend or rename these members.** Every integration in the parcel
    suite publishes exactly this vocabulary on the ``status`` field of each
    normalised parcel, so cross-carrier automations and the aggregator can
    target ``status: out_for_delivery`` regardless of carrier. Listed in
    roughly the order a parcel moves through.
    """

    REGISTERED = "registered"  # Sender announced the parcel; not handed over yet
    IN_TRANSIT = "in_transit"  # In the carrier's network
    OUT_FOR_DELIVERY = "out_for_delivery"  # On a delivery vehicle today
    AT_PICKUP_POINT = "at_pickup_point"  # Ready to collect at a pickup location
    DELIVERED = "delivered"  # Handed over
    RETURNING = "returning"  # Failed delivery, going back to sender
    PROBLEM = "problem"  # Carrier reports an exception/issue
    UNKNOWN = "unknown"  # Raw status we have not mapped yet


PLATFORMS = [Platform.BUTTON, Platform.CALENDAR, Platform.SENSOR]

# Every optional key the parcel contract defines. CAPABILITIES below must be a
# subset of this — it exists so a typo in CAPABILITIES fails a test instead of
# silently dropping a carrier off a table on the docs site.
KNOWN_CAPABILITIES = frozenset(
    {"weight", "dimensions", "delivery_window", "pickup_point", "url", "history"}
)

# Every value not listed here must come back as a literal ``None`` from
# normalize_scans() in parcels.py (or, for "history", CONF_INCLUDE_HISTORY
# must not be wired to anything real) — never omit the key, just leave it
# empty. The docs site's carrier comparison table is generated straight from
# this constant, so drift here does not stay a local mistake, it becomes a
# wrong claim on the website.
#
#   "weight"           parcel["weight"] is ever non-null
#   "dimensions"        parcel["dimensions"] is ever non-null
#   "delivery_window"   parcel["planned_from"] and/or ["planned_to"] is ever non-null
#   "pickup_point"       parcel["pickup_point"] is ever non-null (beyond the "pickup" bool)
#   "url"                parcel["url"] is ever non-null
#   "history"            the include_history option is implemented and does something
#
# Neither source reports an ETA, a weight or dimensions.
CAPABILITIES_BY_VARIANT = {
    "Tracking": frozenset({"pickup_point", "url", "history"}),
    "Account": frozenset({"url"}),
}
CAPABILITIES = CAPABILITIES_BY_VARIANT["Tracking"]

# Fields whose support is not confirmed yet. A field is in exactly one of three
# states: in CAPABILITIES_BY_VARIANT (seen populated), here (docs site shows
# "awaiting data"), or in neither (the API never exposes it). The account's
# inbox and detail payloads have never been seen on the wire, so what it
# derives from the scan rows stays pending until a real login confirms it.
PENDING_CAPABILITIES_BY_VARIANT = {
    "Tracking": frozenset(),
    "Account": frozenset({"pickup_point", "history"}),
}
PENDING_CAPABILITIES = PENDING_CAPABILITIES_BY_VARIANT["Tracking"]

# Guest lookup: keyless, but the service refuses any client that does not name
# the app in its User-Agent. ``TRACKING_URL`` is the human-facing deep link
# surfaced on each parcel's ``url`` field.
TRACKING_API_URL = "https://moja.posta.si/api/v1/parcels/track/"
TRACKING_URL = "https://moja.posta.si/tracking?trackingNumber={tracking_code}"
USER_AGENT = "MojaPosta/2.5.0 (android)"

# Account source: Keycloak direct access grant, then the signed-in inbox and
# the per-parcel detail. Bearer token on both.
TOKEN_URL = "https://auth.posta.si/realms/mojaposta/protocol/openid-connect/token"
PARCELS_URL = "https://moja.posta.si/api/v1/parcels"
OAUTH_CLIENT_ID = "mojaposta-ios"
OAUTH_SCOPE = "openid profile email offline_access"
INBOX_PAGE_SIZE = 10

# An entry is either a tracking hub (codes the user enters) or a MojaPošta
# account (parcels discovered from the inbox).
CONF_SOURCE = "source"
SOURCE_TRACKING = "tracking"
SOURCE_ACCOUNT = "account"

CONF_ACCESS_TOKEN = "access_token"
CONF_REFRESH_TOKEN = "refresh_token"
CONF_TOKEN_EXPIRES_AT = "token_expires_at"

# Tracked parcels live in the config entry options of the tracking hub as a
# list of ``{tracking_code}`` dicts. Kept as dicts so future per-parcel fields
# slot in without an options migration.
CONF_PARCELS = "parcels"
CONF_TRACKING_CODE = "tracking_code"

# Which way a tracked parcel travels. On a MojaPošta account the feed says so
# (separate incoming and outgoing lists, and a flag on the row); a tracking code
# carries no such signal and the scans cannot reveal who is sending, so the user
# declares it per parcel on the hub. Stored as ``CONF_DIRECTION`` on the
# ``CONF_PARCELS`` dicts; an entry without one is incoming, so nothing written
# before the option existed needs a migration.
CONF_DIRECTION = "direction"
DIRECTION_INCOMING = "incoming"
DIRECTION_OUTGOING = "outgoing"
DEFAULT_DIRECTION = DIRECTION_INCOMING

# The account inbox is split over two sibling query parameters. They read like
# the values above but are the carrier's own spelling, so renaming one of
# those never silently breaks the fetch.
INBOX_PARAM_BY_DIRECTION = {
    DIRECTION_INCOMING: "incoming",
    DIRECTION_OUTGOING: "outgoing",
}

# Delivered-parcels retention: keep delivered parcels visible for the last N
# days, or keep only the N most recent — identical across the suite.
CONF_DELIVERED_FILTER_TYPE = "delivered_filter_type"
CONF_DELIVERED_FILTER_AMOUNT = "delivered_filter_amount"
DEFAULT_DELIVERED_FILTER_TYPE = "days"
DEFAULT_DELIVERED_FILTER_AMOUNT = 7

# Dynamic, status-driven polling — unconditional across the suite, no
# user-facing interval option (see CLAUDE.md's "Dynamic polling"
# section for the full algorithm and the reasoning behind it).
#
# Quiet window: no polling between these local hours except the two anchors
# below, for overnight / end-of-day catch-up.
QUIET_WINDOW_START_HOUR = 0
QUIET_WINDOW_END_HOUR = 6

# Cadence while polling is active (minutes). Hot = at least one active parcel
# is out_for_delivery within HOT_LOOKAHEAD_HOURS of its planned_from (or has
# no planned_from at all); mid = anything else still in flight. The tracking
# hub stops entirely when nothing is left to track; an account never stops —
# the mid-tier poll is also how a new shipment gets discovered.
HOT_INTERVAL_MINUTES = 15
MID_INTERVAL_MINUTES = 45
HOT_LOOKAHEAD_HOURS = 1

# Small, stable per-install offset added to every computed interval so
# different installs don't all hit an anchor or tier boundary at the same
# second. Deterministic (hash of the config entry id), not random.
STAGGER_MINUTES = 7

# Per-parcel status history is opt-in and off by default, identical across the
# suite. Keep it off by default: it is a large attribute, and on carriers that
# need a second call per parcel the cost is real.
CONF_INCLUDE_HISTORY = "include_history"
DEFAULT_INCLUDE_HISTORY = False

# Cap each parcel's history to the most recent N events so the attribute stays
# well under HA's ~16 KB state-attribute limit.
HISTORY_MAX_EVENTS = 20
