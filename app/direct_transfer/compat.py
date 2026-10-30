"""Compatibility seam for the legacy omnibus order path (HANDOFF-04 B15).

`app/market` (Phat's) still exposes `place_order`, which fills an order immediately with no payment step. Under
ADR-009 that path is deprecated: new money goes through the funding window. The flag defaults to ENABLED so the old
tests and any pilot data keep working; set `LEGACY_PLACE_ORDER_ENABLED=false` to retire it. Phat's router should call
`assert_legacy_place_order_enabled()` (returns 410 `{"detail": ...}` when disabled), see HANDOFF-04-phat-tickets.md.
"""
from __future__ import annotations

import os

from app.direct_transfer.errors import DirectTransferError

LEGACY_FLAG = "LEGACY_PLACE_ORDER_ENABLED"


def legacy_place_order_enabled() -> bool:
    return os.getenv(LEGACY_FLAG, "true").strip().lower() not in ("0", "false", "no", "off")


def assert_legacy_place_order_enabled() -> None:
    if not legacy_place_order_enabled():
        raise DirectTransferError(
            410, "Direct investing is retired: reserve a ticket on the listing's funding window instead", "LEGACY_ORDER_RETIRED"
        )
