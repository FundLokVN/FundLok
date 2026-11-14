"""Read the fields of a Vietnamese bank-transfer receipt out of its OCR text.

WHAT THE FILE IS
The confirmation an investor's banking app shows after a transfer ("Chuyển tiền thành công"), as a screenshot or
the bank's PDF. Every bank lays it out differently, but they all label the same things in Vietnamese (or English):
"Số tiền", "Thời gian", "Tài khoản nguồn", "Tài khoản nhận", "Nội dung", "Mã giao dịch" ... This parser finds
those labels and reads the value beside or under each one.

WHAT IT PRODUCES
`ReceiptFields` (the shape `app/direct_transfer/receipts.check_receipt` checks), the receiving bank as printed
(`payee_bank`: the "Ngân hàng nhận" line, else the text beside the receiving account number), the OCR confidence of
the line each field came from, and notes on what it could not read cleanly. Pure: no I/O, no clock, no network.

WHY IT IS CAUTIOUS
A confident read can deny an investor's receipt automatically, so this parser would rather say "not sure" (a note,
which sends the receipt to staff) than guess:
  * every label of every field is scored against the start of a line; exact beats fuzzy (OCR noise), fuzzy needs
    a high similarity, and the best label wins, so "Tài khoản nguồn" is never read as "Tài khoản nhận";
  * a value that does not look like its field (no digits in an amount) is skipped, not forced;
  * a field found twice with different values, a masked account number ("****6789") that would be compared, or a
    second different unlabelled amount or time is noted, never chosen;
  * a time shown without seconds is taken as the END of that minute, so a transfer made in the same minute as the
    reservation is not flagged as predating it.
A bank whose receipt uses other words simply goes to staff until its labels are added here.
"""
from __future__ import annotations

import dataclasses
import datetime as dt
import re
import unicodedata
from difflib import SequenceMatcher

from app.core.timezone import ICT
from app.direct_transfer.receipts import REQUIRED_FIELDS, ReceiptFields, normalize_name

PARSER_VERSION = "bank-receipt-1"

# Labels per field, diacritic-folded and upper-cased (see `fold`).
LABELS: dict[str, tuple[str, ...]] = {
    "amount_vnd": (
        "SO TIEN GIAO DICH", "SO TIEN CHUYEN", "SO TIEN", "TONG TIEN", "TRANSACTION AMOUNT", "AMOUNT",
    ),
    "transferred_at": (
        "THOI GIAN GIAO DICH", "THOI GIAN THUC HIEN", "THOI GIAN CHUYEN", "NGAY GIAO DICH", "NGAY THUC HIEN",
        "NGAY GIO GIAO DICH", "THOI GIAN", "NGAY GIO", "TRANSACTION DATE", "TRANSACTION TIME",
    ),
    "payer_name": (
        "TEN NGUOI CHUYEN", "NGUOI CHUYEN", "TEN TAI KHOAN NGUON", "CHU TAI KHOAN NGUON", "TEN NGUOI GUI",
        "NGUOI GUI", "SENDER NAME",
    ),
    "payer_account": (
        "TAI KHOAN NGUON", "SO TAI KHOAN NGUON", "TU TAI KHOAN", "TAI KHOAN CHUYEN", "TAI KHOAN TRICH NO",
        "FROM ACCOUNT", "SOURCE ACCOUNT", "DEBIT ACCOUNT",
    ),
    "payee_name": (
        "TEN NGUOI NHAN", "NGUOI NHAN", "TEN NGUOI THU HUONG", "NGUOI THU HUONG", "TEN THU HUONG",
        "TEN TAI KHOAN NHAN", "TEN TAI KHOAN THU HUONG", "CHU TAI KHOAN NHAN", "BENEFICIARY NAME", "RECEIVER NAME",
    ),
    "payee_account": (
        "TAI KHOAN NHAN", "SO TAI KHOAN NHAN", "TAI KHOAN THU HUONG", "SO TAI KHOAN THU HUONG", "DEN TAI KHOAN",
        "TAI KHOAN DICH", "TAI KHOAN GHI CO", "TAI KHOAN DEN", "BENEFICIARY ACCOUNT", "TO ACCOUNT",
    ),
    "payee_bank": (
        "NGAN HANG NHAN", "NGAN HANG THU HUONG", "NGAN HANG DEN", "NGAN HANG HUONG", "TEN NGAN HANG NHAN",
        "NGAN HANG NGUOI NHAN", "NGAN HANG NGUOI THU HUONG", "BENEFICIARY BANK", "RECEIVING BANK", "TO BANK",
    ),
    "memo": (
        "NOI DUNG CHUYEN KHOAN", "NOI DUNG CHUYEN TIEN", "NOI DUNG GIAO DICH", "NOI DUNG", "LOI NHAN", "DIEN GIAI",
        "DESCRIPTION", "TRANSFER CONTENT",
    ),
    "bank_txn_id": (
        "MA GIAO DICH", "SO GIAO DICH", "MA THAM CHIEU", "SO THAM CHIEU", "MA GD", "SO BUT TOAN", "TRANSACTION ID",
        "TRANSACTION NO", "REFERENCE NO", "REF NO", "TRACE NO",
    ),
}

# Lines that start like a label but are not the field (the amount spelled out in words, the fee, the balance).
NOT_LABELS = ("SO TIEN BANG CHU", "PHI GIAO DICH", "PHI CHUYEN", "TRANSACTION FEE", "SO DU")

FUZZY_THRESHOLD = 0.88

# Note codes (stable strings; the admin UI maps them to plain language).
NOTE_MISSING = "MISSING_FIELD"  # MISSING_FIELD:<field>
NOTE_MASKED = "MASKED"  # MASKED:<field>
NOTE_AMBIGUOUS = "AMBIGUOUS"  # AMBIGUOUS:<field>
NOTE_LOW_CONFIDENCE = "LOW_CONFIDENCE"  # LOW_CONFIDENCE:<field>

# The payer's account only has to be on the receipt (check_receipt does not compare it), so a masked one still
# counts as read.
NON_BLOCKING_NOTES = frozenset({f"{NOTE_MASKED}:payer_account"})


@dataclasses.dataclass(frozen=True)
class OcrLine:
    """One line of text as the OCR engine read it. `confidence` is 0-100, or None when the text is exact (the text
    layer of a PDF the bank generated)."""

    text: str
    confidence: float | None = None


@dataclasses.dataclass(frozen=True)
class ParsedReceipt:
    fields: ReceiptFields
    field_confidence: dict[str, float | None]
    notes: tuple[str, ...]
    payee_bank: str | None = None  # the receiving bank as printed; identified against the bank list by the caller

    def blocking_notes(self, min_confidence: float) -> list[str]:
        """What stops this read deciding the receipt on its own: a required field missing, masked, ambiguous or
        read below `min_confidence`. Empty means the read is confident."""
        blocking = [
            n for n in self.notes if n.split(":", 1)[-1] in REQUIRED_FIELDS and n not in NON_BLOCKING_NOTES
        ]
        for name in REQUIRED_FIELDS:
            conf = self.field_confidence.get(name)
            if getattr(self.fields, name) is not None and conf is not None and conf < min_confidence:
                blocking.append(f"{NOTE_LOW_CONFIDENCE}:{name}")
        return blocking


# --------------------------------------------------------------------------- #
# text helpers
# --------------------------------------------------------------------------- #
def fold(text: str) -> str:
    """Upper-case ASCII with diacritics removed and punctuation collapsed to single spaces."""
    text = unicodedata.normalize("NFKD", text.replace("đ", "d").replace("Đ", "D"))
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    return re.sub(r"\s+", " ", re.sub(r"[^0-9A-Za-z]+", " ", text)).strip().upper()


def match_label(line: str) -> tuple[str, str] | None:
    """(field, the text after the label) for the label this line starts with, or None."""
    # "Số tiền:10,000,000" -> "Số tiền: 10,000,000" (a colon after a letter ends a label; times keep theirs).
    words = re.sub(r"(?<=[^\W\d_]):", ": ", line).split()
    tokens: list[tuple[str, int]] = [(t, i) for i, w in enumerate(words) for t in fold(w).split()]
    if not tokens:
        return None
    folded_line = " ".join(t for t, _ in tokens)
    if any(folded_line.startswith(n) for n in NOT_LABELS):
        return None

    best: tuple[tuple[float, int], str, int] | None = None  # (score, label length), field, last word index
    for field, labels in LABELS.items():
        for label in labels:
            n = len(label.split())
            if len(tokens) < n:
                continue
            candidate = " ".join(t for t, _ in tokens[:n])
            score = 1.0 if candidate == label else SequenceMatcher(None, candidate, label).ratio()
            if score < FUZZY_THRESHOLD:
                continue
            key = (score, n)  # the better match; on a tie the longer, more specific label
            if best is None or key > best[0]:
                best = (key, field, tokens[n - 1][1])
    if best is None:
        return None
    _, field, last_word = best
    return field, " ".join(words[last_word + 1:]).strip(" :;-–—|\t")


# --------------------------------------------------------------------------- #
# value readers: each returns the value, or None when the text does not look like the field
# --------------------------------------------------------------------------- #
_AMOUNT = re.compile(r"(?<![\d.,/:])(\d{1,3}(?:([.,\s])\d{3})(?:\2\d{3})*|\d+)(?:([.,])(\d{1,2}))?(?![\d/:])")


def read_amount(text: str) -> int | None:
    """"10,000,000 VND", "10.000.000 đ", "-10,000,000.00", "10 000 000" -> 10000000. Decimals must be zero."""
    for m in _AMOUNT.finditer(text):
        whole, group_sep, decimal_sep, decimals = m.groups()
        if decimal_sep and group_sep and decimal_sep == group_sep:
            continue
        if decimals and int(decimals) != 0:
            continue
        value = int(re.sub(r"\D", "", whole))
        if value > 0:
            return value
    return None


def has_currency(text: str) -> bool:
    return bool(re.search(r"\b(VND|D|DONG)\b", fold(text))) or "₫" in text


_DATE = re.compile(r"(?<!\d)(\d{1,2})[/\-.](\d{1,2})[/\-.](\d{4})(?!\d)")
_TIME = re.compile(r"(?<![\d/.])(\d{1,2})[:h](\d{2})(?::(\d{2}))?(?!\d)")


def read_datetime(text: str) -> dt.datetime | None:
    """A date (dd/mm/yyyy, dd-mm-yyyy, dd.mm.yyyy) and a time (hh:mm[:ss]) in either order, in Vietnam time."""
    d, t = _DATE.search(text), _TIME.search(text)
    if not d or not t:
        return None
    day, month, year = int(d.group(1)), int(d.group(2)), int(d.group(3))
    hour, minute = int(t.group(1)), int(t.group(2))
    second = int(t.group(3)) if t.group(3) is not None else 59  # the end of the minute shown (module doc)
    try:
        return dt.datetime(year, month, day, hour, minute, second, tzinfo=ICT)
    except ValueError:
        return None


def read_name(text: str) -> str | None:
    """A holder's name: the text before any digit (an account number) or " - " (a bank name) on the same line."""
    name = re.split(r"\d|\s[-–|]\s", text, maxsplit=1)[0]
    name = re.sub(r"\s+", " ", name).strip(" :;-–—|,.")
    return name if sum(ch.isalpha() for ch in name) >= 2 else None


_ACCOUNT = re.compile(r"[0-9*•xX][0-9*•xX\s.\-]{3,40}[0-9]")


def _account_match(text: str) -> re.Match | None:
    for m in _ACCOUNT.finditer(text):
        compact = re.sub(r"[\s.\-]", "", m.group(0))
        if sum(ch.isdigit() for ch in compact) < 4:
            continue
        if not re.search(r"[*•xX]", compact) and len(compact) < 6:
            continue
        return m
    return None


def read_account(text: str) -> tuple[str | None, bool]:
    """(account number, masked). Spaces, dots and dashes inside the number are dropped; a number with a mask
    character ("****6789") comes back with masked=True."""
    m = _account_match(text)
    if m is None:
        return None, False
    compact = re.sub(r"[\s.\-]", "", m.group(0))
    return compact, bool(re.search(r"[*•xX]", compact))


def beside_account(text: str) -> str | None:
    """The words printed with an account number on its line ("19036789012 - Techcombank"): often the bank."""
    m = _account_match(text)
    if m is None:
        return None
    rest = re.sub(r"\s+", " ", f"{text[:m.start()]} {text[m.end():]}").strip(" :;-–—|,()")
    return rest if sum(ch.isalpha() for ch in rest) >= 2 else None


def read_bank(text: str) -> str | None:
    bank = re.sub(r"\s+", " ", text).strip(" :;-–—|,")
    return bank if sum(ch.isalpha() for ch in bank) >= 2 else None


def read_txn_id(text: str) -> str | None:
    for token in re.findall(r"[A-Za-z0-9]{5,}", text):
        if any(ch.isdigit() for ch in token):
            return token.upper()
    return None


def read_memo(text: str) -> str | None:
    return re.sub(r"\s+", " ", text).strip() or None


def _same(field: str, a: object, b: object) -> bool:
    if field in ("payer_name", "payee_name"):
        return normalize_name(str(a)) == normalize_name(str(b))
    if field == "memo":
        return fold(str(a)) == fold(str(b))
    return a == b


def _min_conf(*values: float | None) -> float | None:
    known = [v for v in values if v is not None]
    return min(known) if known else None


# --------------------------------------------------------------------------- #
# the parser
# --------------------------------------------------------------------------- #
def parse_receipt_lines(lines: list[OcrLine]) -> ParsedReceipt:
    lines = [OcrLine(line.text.strip(), line.confidence) for line in lines if line.text and line.text.strip()]
    labelled = [match_label(line.text) for line in lines]

    found: dict[str, list[tuple[object, float | None]]] = {}
    masked: set[str] = set()
    bank_beside_account: str | None = None

    def below(i: int) -> OcrLine | None:
        # The value of a label on the line under it (a two-row layout), if that line is not a label itself.
        return lines[i + 1] if i + 1 < len(lines) and labelled[i + 1] is None else None

    for i, (line, match) in enumerate(zip(lines, labelled)):
        if match is None:
            continue
        field, value_text = match
        conf = line.confidence
        used_below = False
        if not value_text:
            nxt = below(i)
            if nxt is None:
                continue
            value_text, conf, used_below = nxt.text, _min_conf(conf, nxt.confidence), True

        value: object
        if field == "amount_vnd":
            value = read_amount(value_text)
        elif field == "transferred_at":
            value = read_datetime(value_text)
            nxt = below(i) if not used_below else None
            if value is None and nxt is not None:  # "Thời gian: 10:23:45" with the date on the next line
                value = read_datetime(f"{value_text} {nxt.text}")
                conf = _min_conf(conf, nxt.confidence)
        elif field in ("payer_name", "payee_name"):
            value = read_name(value_text)
        elif field in ("payer_account", "payee_account"):
            value, is_masked = read_account(value_text)
            if is_masked:
                masked.add(field)
            if field == "payee_account" and bank_beside_account is None:
                bank_beside_account = beside_account(value_text)
        elif field == "payee_bank":
            value = read_bank(value_text)
        elif field == "bank_txn_id":
            value = read_txn_id(value_text)
        else:  # memo: it may wrap onto the next line; extra text never hides the reference
            value = read_memo(value_text)
            nxt = below(i) if not used_below else None
            if value is not None and nxt is not None:
                value = f"{value} {nxt.text.strip()}"
                conf = _min_conf(conf, nxt.confidence)
        if value is not None:
            found.setdefault(field, []).append((value, conf))

    notes: list[str] = []
    values: dict[str, object] = {}
    confidence: dict[str, float | None] = {}
    for field, candidates in found.items():
        first, conf = candidates[0]
        if any(not _same(field, first, other) for other, _ in candidates[1:]):
            notes.append(f"{NOTE_AMBIGUOUS}:{field}")
            continue
        values[field] = first
        confidence[field] = _min_conf(*(c for _, c in candidates))
    notes.extend(f"{NOTE_MASKED}:{field}" for field in sorted(masked) if field in values)

    # Unlabelled fallbacks: many apps show the amount large at the top and the time under the title, with no label.
    # Only a single unambiguous candidate is used.
    if "amount_vnd" not in found:
        amounts: dict[int, float | None] = {}
        for line, match in zip(lines, labelled):
            if match is None and has_currency(line.text) and (amount := read_amount(line.text)) is not None:
                amounts.setdefault(amount, line.confidence)
        if len(amounts) == 1:
            [(values["amount_vnd"], confidence["amount_vnd"])] = amounts.items()
        elif len(amounts) > 1:
            notes.append(f"{NOTE_AMBIGUOUS}:amount_vnd")
    if "transferred_at" not in found:
        moments: dict[dt.datetime, float | None] = {}
        for line, match in zip(lines, labelled):
            if match is None and (moment := read_datetime(line.text)) is not None:
                moments.setdefault(moment, line.confidence)
        if len(moments) == 1:
            [(values["transferred_at"], confidence["transferred_at"])] = moments.items()
        elif len(moments) > 1:
            notes.append(f"{NOTE_AMBIGUOUS}:transferred_at")

    payee_bank = values.pop("payee_bank", None)
    if payee_bank is None and "payee_bank" not in found:
        payee_bank = bank_beside_account

    fields = ReceiptFields(
        amount_vnd=values.get("amount_vnd"),  # type: ignore[arg-type]
        transferred_at=values.get("transferred_at"),  # type: ignore[arg-type]
        payer_name=values.get("payer_name"),  # type: ignore[arg-type]
        payer_account=values.get("payer_account"),  # type: ignore[arg-type]
        payee_account=values.get("payee_account"),  # type: ignore[arg-type]
        memo=values.get("memo"),  # type: ignore[arg-type]
        bank_txn_id=values.get("bank_txn_id"),  # type: ignore[arg-type]
        payee_name=values.get("payee_name"),  # type: ignore[arg-type]
    )
    for name in REQUIRED_FIELDS:
        if getattr(fields, name) is None and not any(n.endswith(f":{name}") for n in notes):
            notes.append(f"{NOTE_MISSING}:{name}")
    return ParsedReceipt(fields=fields, field_confidence=confidence, notes=tuple(notes), payee_bank=payee_bank)
