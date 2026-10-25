"""Scrubbed Pošta Slovenije payloads shared by the test modules.

Shaped after the real guest-lookup replies. Numbers, ids and trace ids are made
up (the numbers satisfy the S10 check digit), and no name, address, PIN or
token appears anywhere.
"""

from __future__ import annotations

from typing import Any

from custom_components.posta_slovenije.const import INBOX_PAGE_SIZE

RETURNED_CODE = "CH000000005SI"
DELIVERED_CODE = "CH100000003SI"
PICKUP_CODE = "CH200000006SI"

POST_OFFICE = {
    "postalId": "3320",
    "postalName": "Velenje",
    "pickupAddress": "Example street 1, 3320 Velenje",
}


def row(code: str, when: str, **changes: Any) -> dict[str, Any]:
    """One scan row with every key the service sends, blank unless overridden."""
    base: dict[str, Any] = {
        "postalId": "",
        "postalName": "",
        "code": code,
        "subCode": "",
        "codeDescriptionSI": f"{code} SI",
        "codeDetailedDescriptionSI": "",
        "codeDescriptionEN": f"{code} EN",
        "codeDetailedDescriptionEN": "",
        "nondeliveryId": 0,
        "nondeliveryReasonSI": "",
        "nondeliveryActionSI": "",
        "nondeliveryReasonEN": "",
        "nondeliveryActionEN": "",
        "recipient": "",
        "signatory": "",
        "note": "",
        "canceled": 0,
        "statusId": 2,
        "statusName": "Na poti",
        "statusDateTime": when,
        "pickupAddress": "",
        "unitType": 0,
        "unitId": "",
    }
    return {**base, **changes}


def envelope(
    number: str,
    statuses: list[dict[str, Any]],
    *,
    outgoing: Any = False,
    **parcel: Any,
) -> dict:
    """A full 200 reply for ``number`` with the given scan rows."""
    return {
        "result": {
            "parcel": {
                "parcelId": 1000001,
                "trackingNumber": number,
                "statusId": 3,
                "currentStatusDateTime": "2026-10-09T05:22:10.957Z",
                "senderName": None,
                "receiverName": None,
                "parcelType": "Tracked",
                "parcelDestinations": [],
                "eligibleForRedirect": False,
                "createdDate": "2026-09-29T05:48:11.09Z",
                "isArchived": False,
                "codeDescriptionSI": "Pošiljka ni bila prevzeta",
                "codeDescriptionEN": "Unsuccessful delivery attempt",
                "pickupQRCode": None,
                "pickupPINCode": None,
                "source": 0,
                "payByLinkUrl": None,
                "pickupUnitAddress": None,
                **parcel,
            },
            "statuses": statuses,
            "inMyParcels": False,
            "isOutgoingParcel": outgoing,
            "availableDeliveryTypes": None,
        },
        "errorCode": None,
        "errorMessage": None,
        "invalidField": None,
        "timeGenerated": "2026-10-23T21:26:10.0000000Z",
        "traceId": "00000000000000000000000000000000",
    }


RETURN_STEP = {
    "nondeliveryId": 1,
    "nondeliveryReasonSI": "Naslovnik ni prevzel pošiljke",
    "nondeliveryActionSI": "Vrača se pošiljatelju",
    "nondeliveryActionEN": "Item is being returned to the sender",
}


def returned_statuses() -> list[dict[str, Any]]:
    """Failed delivery, a post-office wait, a returning ``V``, back to the sender."""
    return [
        row("NAJ", "2026-09-29T05:48:11.09Z", statusId=1, statusName="V sistemu"),
        row("S", "2026-09-29T14:53:03.547Z", postalId="1102", postalName="Ljubljana"),
        row("N", "2026-09-30T04:39:35.483Z", subCode="NPIS", postalId="3320"),
        row(
            "VDO",
            "2026-09-30T05:58:59Z",
            nondeliveryActionEN="The item will be redirected to the post office",
            **POST_OFFICE,
        ),
        row(
            "PVI",
            "2026-09-30T12:59:10.52Z",
            unitType=99,
            unitId="3320",
            **POST_OFFICE,
        ),
        row("V", "2026-10-09T05:22:10.957Z", **RETURN_STEP, **POST_OFFICE),
        row("N", "2026-10-12T02:44:12.593Z", subCode="NPIS", postalId="1002"),
        row("N", "2026-10-12T04:56:10.763Z", subCode="NPIS", postalId="1002"),
        row(
            "VRP",
            "2026-10-12T08:41:41Z",
            statusId=3,
            statusName="Dostavljena",
            recipient="Signer Name",
            signatory="Signer Name",
            subCode="VRPIS",
            postalId="1002",
            postalName="Ljubljana - poštni center",
        ),
    ]


def returned_payload(number: str = RETURNED_CODE) -> dict:
    """The returned-to-sender parcel (its summary lags at the failed attempt)."""
    return envelope(number, returned_statuses())


def delivered_statuses() -> list[dict[str, Any]]:
    """NAJ, S, N, VL."""
    return [
        row("NAJ", "2026-09-28T05:00:00.5Z", statusId=1),
        row("S", "2026-09-28T14:00:00Z"),
        row("N", "2026-09-29T04:30:00.25Z", subCode="NPIS"),
        row(
            "VL",
            "2026-09-29T08:15:30Z",
            statusId=3,
            statusName="Dostavljena",
            recipient="Signer Name",
            signatory="Signer Name",
            note="Left at the door",
            subCode="VRPIS",
        ),
    ]


def delivered_payload(number: str = DELIVERED_CODE, *, outgoing: Any = False) -> dict:
    """The delivered parcel."""
    return envelope(number, delivered_statuses(), outgoing=outgoing)


def pickup_payload(number: str = PICKUP_CODE) -> dict:
    """A parcel waiting at a post office after a failed delivery."""
    return envelope(number, returned_statuses()[:5])


def in_transit_payload(number: str = PICKUP_CODE) -> dict:
    """A parcel that has just been scanned in."""
    return envelope(number, returned_statuses()[:2])


def not_found_payload(number: str) -> dict:
    """The service's not-found envelope."""
    return {
        "result": None,
        "errorCode": "error.not.found",
        "errorMessage": f"Record not found for Id '{number}'",
        "invalidField": None,
        "timeGenerated": "2026-10-25T08:16:10.9760211Z",
        "traceId": "00000000000000000000000000000000",
    }


GUARD_PAYLOAD = {
    "result": None,
    "errorCode": "error.not.allowed",
    "errorMessage": "User doesn't have necessary permissions.",
    "invalidField": None,
    "timeGenerated": "2026-10-25T08:16:10.7348596Z",
    "traceId": "00000000000000000000000000000000",
}


# -- account (shapes assumed, never seen on the wire) -------------------------


def inbox_item(
    parcel_id: int, number: str, stamp: str = "2026-10-12T08:41:41Z", **changes: Any
) -> dict[str, Any]:
    """A synthetic inbox row: the parcel object with the parties filled in."""
    return {
        "parcelId": parcel_id,
        "trackingNumber": number,
        "statusId": 2,
        "currentStatusDateTime": stamp,
        "senderName": "Example Shop",
        "receiverName": "Example Receiver",
        "parcelType": "Tracked",
        "eligibleForRedirect": False,
        "createdDate": "2026-09-29T05:48:11.09Z",
        "isArchived": False,
        "isOutgoingParcel": False,
        "codeDescriptionSI": "Na poti",
        "codeDescriptionEN": "On its way",
        "pickupQRCode": "QR-EXAMPLE",
        "pickupPINCode": "4817",
        "payByLinkUrl": None,
        **changes,
    }


def sent_item(parcel_id: int, number: str, **changes: Any) -> dict[str, Any]:
    """A synthetic row of the outgoing list: the same object, flagged outgoing."""
    return inbox_item(parcel_id, number, **{"isOutgoingParcel": True, **changes})


def inbox_page(
    items: list[dict[str, Any]],
    total: int,
    *,
    page: int = 1,
    total_pages: int | None = None,
) -> dict[str, Any]:
    """One page of the inbox.

    ``total_pages`` defaults to the count this page's size implies, so a test
    that hands out two pages of two gets ``totalPages`` to match without
    saying so.
    """
    if total_pages is None:
        total_pages = max(1, -(-total // len(items))) if items else 1
    return {
        "result": {
            "items": items,
            "totalCount": total,
            "pageNumber": page,
            "pageSize": len(items) or INBOX_PAGE_SIZE,
            "totalPages": total_pages,
        },
        "errorCode": None,
        "errorMessage": None,
        "invalidField": None,
        "timeGenerated": "2026-10-25T08:00:00Z",
        "traceId": "00000000000000000000000000000000",
    }


def token_response(
    access: str = "access-1", refresh: str | None = "refresh-1", expires_in: int = 300
) -> dict[str, Any]:
    """A successful token-endpoint answer."""
    body: dict[str, Any] = {
        "access_token": access,
        "expires_in": expires_in,
        "token_type": "Bearer",
    }
    if refresh is not None:
        body["refresh_token"] = refresh
    return body


def out_for_delivery_payload(
    number: str = PICKUP_CODE, *, outgoing: Any = False
) -> dict:
    """A parcel out with the postman today."""
    return envelope(number, delivered_statuses()[:3], outgoing=outgoing)
