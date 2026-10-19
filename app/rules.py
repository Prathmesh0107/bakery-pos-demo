"""Pure checkout rules (no database, no web framework) so they can be unit-tested anywhere."""
from typing import Dict, Optional


def requested_qty(lines) -> Dict[int, int]:
    """Merge duplicate product lines: [{id,q}, ...] -> {id: total_q}."""
    qty: Dict[int, int] = {}
    for i in lines:
        qty[i["id"]] = qty.get(i["id"], 0) + i["q"]
    return qty


def same_request(old_items, old_pay: str, old_payments: Optional[dict], qty: Dict[int, int],
                 pay: str, cash: Optional[int], upi: Optional[int]) -> bool:
    """True if an already-stored sale was made from this very request (same products, quantities and payment method).

    Used when an idempotency key is seen again: same bill -> return the stored sale; different bill under the same
    key -> refuse, never silently hand back an unrelated receipt.
    """
    if requested_qty(old_items) != qty or old_pay != pay:
        return False
    if pay == "Split":
        want = {k: v for k, v in (("Cash", cash), ("UPI", upi)) if v and v > 0}
        return (old_payments or {}) == want
    return True
