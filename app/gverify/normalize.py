"""Normalisation of identifiers typed by people (ID numbers, tax codes).

Python's ``\d`` matches every Unicode decimal digit (fullwidth, Arabic-Indic,
...). Left alone, ``"０１２…"`` passes validation yet never equals ``"012…"``
in the one-ID-one-account comparison — a lookalike that evades duplicate
detection. Everything is folded to plain ASCII 0-9 before it is validated,
compared or stored, and validation regexes use explicit ``[0-9]``.
"""

import unicodedata


def to_ascii_digits(value: str) -> str:
    """NFKC-fold ``value`` and map any remaining Unicode decimal digit to 0-9."""
    folded = unicodedata.normalize("NFKC", value)
    out: list[str] = []
    for ch in folded:
        if ch.isdigit() and not ch.isascii():
            try:
                out.append(str(unicodedata.decimal(ch)))
                continue
            except (TypeError, ValueError):
                pass
        out.append(ch)
    return "".join(out)


def normalize_identifier(value: str) -> str:
    """ASCII digits, all whitespace removed — the canonical comparison form."""
    return "".join(to_ascii_digits(value).split())
