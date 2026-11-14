"""Vietnamese mobile numbers: one canonical form, and the forms already on file.

Codes go to Vietnamese mobiles only (spec rule 2): the platform's users are
Vietnam-registered, and every code sent costs a ZBS message.

users.phone predates verification and holds whatever the registration form
accepted (0912…, 84912…, +84912…), so uniqueness checks match all three.
"""
import re

# Mobile ranges only (03x, 05x, 07x, 08x, 09x); landlines cannot receive SMS.
# [0-9], not \d: \d also matches non-ASCII digits, which would reach the SMS gateway.
_VN_MOBILE = re.compile(r"(?:\+84|84|0)([35789][0-9]{8})")
_SEPARATORS = re.compile(r"[\s().-]")


def canonical_vn_mobile(raw: str) -> str | None:
    """+84xxxxxxxxx for a Vietnamese mobile number, else None."""
    match = _VN_MOBILE.fullmatch(_SEPARATORS.sub("", raw.strip()))
    return f"+84{match.group(1)}" if match else None


def equivalent_forms(canonical: str) -> list[str]:
    """Every spelling of `canonical` that users.phone may hold."""
    national = canonical[3:]
    return [canonical, f"84{national}", f"0{national}"]


def same_number(a: str | None, b: str | None) -> bool:
    if a is None or b is None:
        return a is b
    ca, cb = canonical_vn_mobile(a), canonical_vn_mobile(b)
    if ca is None or cb is None:
        return a == b
    return ca == cb


def zalo_number(canonical: str) -> str:
    """The wire form Zalo ZBS expects: 84xxxxxxxxx, no plus."""
    return canonical[1:]
