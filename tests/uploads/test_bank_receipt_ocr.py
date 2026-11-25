"""Reading a bank-transfer receipt (docs/specs/direct_transfer/receipt-ocr.md): the label parser, the Tesseract
reader's plumbing, and the rule that only a confident read may decide a receipt. No real OCR engine is needed except
in the last test, which runs only where `tesseract` with Vietnamese data is installed (the production image)."""
import datetime as dt
import io
import shutil
import struct
import subprocess
import zlib

import pytest

from app.core.timezone import ICT
from app.uploads.parsers.bank_receipt import (
    OcrLine,
    match_label,
    parse_receipt_lines,
    read_account,
    read_amount,
    read_datetime,
)
from app.uploads.receipt_ocr import ReceiptOcrSettings, TesseractReceiptReader, reading_from_lines, tsv_to_lines

RECEIPT = """Chuyển tiền thành công
10,000,000 VND
Thời gian 14/11/2026 10:23:45
Tài khoản nguồn 0123456789
Tên người chuyển NGUYỄN VĂN AN
Tài khoản nhận 19036789012
Tên người nhận CONG TY CO PHAN ABC
Ngân hàng nhận Techcombank
Nội dung FLAB12CD34 chuyen tien
Mã giao dịch FT25318123456789
Phí giao dịch 0 VND"""


def _lines(text: str, conf: float | None = 95.0) -> list[OcrLine]:
    return [OcrLine(line, conf) for line in text.splitlines()]


def test_a_typical_receipt_is_read_field_by_field():
    parsed = parse_receipt_lines(_lines(RECEIPT))

    f = parsed.fields
    assert f.amount_vnd == 10_000_000  # unlabelled, the only amount with a currency (the fee is 0)
    assert f.transferred_at == dt.datetime(2026, 11, 14, 10, 23, 45, tzinfo=ICT)
    assert f.payer_account == "0123456789" and f.payer_name == "NGUYỄN VĂN AN"
    assert f.payee_account == "19036789012" and f.payee_name == "CONG TY CO PHAN ABC"
    assert f.memo == "FLAB12CD34 chuyen tien" and f.bank_txn_id == "FT25318123456789"
    assert parsed.payee_bank == "Techcombank"
    assert parsed.notes == () and parsed.blocking_notes(80) == []


def test_the_receiving_bank_printed_beside_the_account_number_is_read():
    parsed = parse_receipt_lines(_lines("Tài khoản nhận: 19036789012 - Techcombank\nNội dung FLAB12CD34"))

    assert parsed.fields.payee_account == "19036789012" and parsed.payee_bank == "Techcombank"


def test_a_two_row_layout_reads_the_value_under_its_label():
    parsed = parse_receipt_lines(_lines("Số tiền\n5.000.000 đ\nNội dung:\nFLZZ99YY88\nMã giao dịch: 998877"))

    assert parsed.fields.amount_vnd == 5_000_000
    assert parsed.fields.memo == "FLZZ99YY88"
    assert parsed.fields.bank_txn_id == "998877"


@pytest.mark.parametrize(
    "text, amount",
    [
        ("10,000,000 VND", 10_000_000),
        ("10.000.000 đ", 10_000_000),
        ("-10,000,000.00 VND", 10_000_000),
        ("10 000 000", 10_000_000),
        ("10000000", 10_000_000),
        ("1.5", None),  # VND has no decimals
        ("0 VND", None),
        ("14/11/2026 10:23", None),  # a date is not an amount
    ],
)
def test_amounts_in_every_format_banks_print(text, amount):
    assert read_amount(text) == amount


def test_a_time_without_seconds_is_the_end_of_that_minute():
    # So a transfer made in the same minute as the reservation is not flagged as predating it.
    assert read_datetime("10:23 14/11/2026") == dt.datetime(2026, 11, 14, 10, 23, 59, tzinfo=ICT)
    assert read_datetime("14-11-2026") is None  # no time: not enough


def test_the_payer_account_is_never_taken_for_the_payee_account():
    assert match_label("Tài khoản nguồn: 0123456789") == ("payer_account", "0123456789")
    assert match_label("Tài khoản nhận: 19036789012") == ("payee_account", "19036789012")
    assert match_label("Số tiền bằng chữ: Mười triệu đồng") is None  # the amount in words is not the amount


def test_a_slightly_misread_label_still_matches_but_a_different_one_does_not():
    assert match_label("Thoi gjan giao dich 14/11/2026 10:23")[0] == "transferred_at"
    assert match_label("Ma giao djch FT123456")[0] == "bank_txn_id"
    assert match_label("Ngân hàng nhận: Techcombank") == ("payee_bank", "Techcombank")
    assert match_label("Ngân hàng thụ hưởng Vietcombank") == ("payee_bank", "Vietcombank")


def test_a_masked_payee_account_cannot_decide_the_receipt():
    parsed = parse_receipt_lines(_lines(RECEIPT.replace("Tài khoản nhận 19036789012", "Tài khoản nhận ******9012")))

    assert read_account("******9012") == ("******9012", True)
    assert "MASKED:payee_account" in parsed.blocking_notes(80)


def test_a_masked_payer_account_still_counts_as_read():
    # The payer's account only has to be on the receipt; it is not compared.
    parsed = parse_receipt_lines(_lines(RECEIPT.replace("Tài khoản nguồn 0123456789", "Tài khoản nguồn 0123***789")))

    assert parsed.fields.payer_account == "0123***789"
    assert parsed.blocking_notes(80) == []


def test_a_field_read_two_different_ways_is_left_for_staff():
    parsed = parse_receipt_lines(_lines(RECEIPT + "\nSố tiền 9,000,000 VND\nSố tiền 10,000,000 VND"))

    assert parsed.fields.amount_vnd is None
    assert "AMBIGUOUS:amount_vnd" in parsed.blocking_notes(80)


def test_two_different_unlabelled_amounts_are_ambiguous():
    parsed = parse_receipt_lines(_lines(RECEIPT.replace("Phí giao dịch 0 VND", "Ưu đãi 5,000 VND")))

    assert parsed.fields.amount_vnd is None
    assert "AMBIGUOUS:amount_vnd" in parsed.notes


def test_a_missing_field_or_a_low_confidence_line_is_not_confident():
    missing = reading_from_lines(_lines(RECEIPT.replace("Mã giao dịch FT25318123456789", "")), engine="t", min_confidence=80)
    assert missing.confident is False and "MISSING_FIELD:bank_txn_id" in missing.notes

    lines = _lines(RECEIPT)
    assert lines[8].text.startswith("Nội dung")
    lines[8] = OcrLine(lines[8].text, 41.0)  # the memo line was hard to read
    shaky = reading_from_lines(lines, engine="t", min_confidence=80)
    assert shaky.confident is False and "LOW_CONFIDENCE:memo" in shaky.notes
    assert shaky.fields.memo == "FLAB12CD34 chuyen tien"  # still shown to staff

    exact = reading_from_lines(_lines(RECEIPT, conf=None), engine="pdf-text", min_confidence=80)
    assert exact.confident is True and exact.confidence is None  # a PDF's own text layer is exact


def test_tesseract_tsv_becomes_lines_with_their_mean_confidence():
    header = "level\tpage_num\tblock_num\tpar_num\tline_num\tword_num\tleft\ttop\twidth\theight\tconf\ttext"
    rows = [
        header,
        "4\t1\t1\t1\t1\t0\t0\t0\t0\t0\t-1\t",
        "5\t1\t1\t1\t1\t1\t0\t0\t0\t0\t90\tSố",
        "5\t1\t1\t1\t1\t2\t0\t0\t0\t0\t80\ttiền",
        "5\t1\t1\t1\t1\t3\t0\t0\t0\t0\t70\t10,000,000",
        "5\t1\t1\t1\t2\t1\t0\t0\t0\t0\t95\tVND",
    ]

    assert tsv_to_lines("\n".join(rows)) == [OcrLine("Số tiền 10,000,000", 80.0), OcrLine("VND", 95.0)]


def _png() -> bytes:
    from PIL import Image

    out = io.BytesIO()
    Image.new("RGB", (400, 200), "white").save(out, format="PNG")
    return out.getvalue()


async def test_a_missing_engine_sends_the_receipt_to_staff_instead_of_failing():
    reader = TesseractReceiptReader(ReceiptOcrSettings(enabled=True), binary="/nonexistent/tesseract")

    reading = await reader.read(content=_png(), content_type="image/png")

    assert reading.confident is False and reading.fields is None
    assert reading.notes == ("OCR_FAILED:ENGINE_MISSING",)
    assert await reader.extract(content=_png(), content_type="image/png") is None


async def test_an_unreadable_image_sends_the_receipt_to_staff():
    reader = TesseractReceiptReader(ReceiptOcrSettings(enabled=True))

    reading = await reader.read(content=b"\x89PNG\r\n\x1a\nbroken", content_type="image/png")

    assert reading.confident is False and reading.notes == ("OCR_FAILED:UNREADABLE_IMAGE",)


def _black_png(width: int, height: int) -> bytes:
    """A valid 1-bit black PNG of any size, built row by row: neither the test nor the file is big (64 MP is ~8 KB),
    yet Pillow would decode it to a byte per pixel."""
    def chunk(kind: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))

    row = bytes(1 + (width + 7) // 8)  # filter byte + packed pixels
    packer = zlib.compressobj(9)
    idat = b"".join(packer.compress(row) for _ in range(height)) + packer.flush()
    ihdr = struct.pack(">IIBBBBB", width, height, 1, 0, 0, 0, 0)
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr) + chunk(b"IDAT", idat) + chunk(b"IEND", b"")


@pytest.mark.parametrize("width,height", [
    (8_000, 8_000),  # 64 MP: under Pillow's own limit, so it used to be decoded and handed to Tesseract
    (600, 34_000),  # 20 MP, but 82 MP once upscaled 2x for Tesseract
    (20_000, 20_000),  # 400 MP: Pillow's DecompressionBombError, still only an unreadable image
])
async def test_an_image_too_large_to_read_is_refused_before_it_is_decoded(monkeypatch, width, height):
    from PIL import ImageFile

    decoded = []
    load = ImageFile.ImageFile.load
    monkeypatch.setattr(ImageFile.ImageFile, "load", lambda self: decoded.append(self.size) or load(self))
    content = _black_png(width, height)
    assert len(content) < 64 * 1024
    # No engine here: had the image got as far as Tesseract, the note would be ENGINE_MISSING.
    reader = TesseractReceiptReader(ReceiptOcrSettings(enabled=True), binary="/nonexistent/tesseract")

    reading = await reader.read(content=content, content_type="image/png")

    assert reading.confident is False and reading.fields is None
    assert reading.notes == ("OCR_FAILED:UNREADABLE_IMAGE",)
    assert decoded == []


@pytest.mark.parametrize("width,height", [(4032, 3024), (1290, 9000)])  # a 12 MP phone photo, a long screenshot
async def test_phone_photos_and_long_screenshots_are_still_read(width, height):
    reader = TesseractReceiptReader(ReceiptOcrSettings(enabled=True), binary="/nonexistent/tesseract")

    reading = await reader.read(content=_black_png(width, height), content_type="image/png")

    assert reading.notes == ("OCR_FAILED:ENGINE_MISSING",)  # prepared and handed to the engine (absent here)


def _text_pdf(lines: list[str]) -> bytes:
    """A one-page PDF whose text layer holds `lines` (ASCII, as a bank's generated PDF has text)."""
    body = "BT /F1 11 Tf 40 760 Td 14 TL " + " ".join(f"({line}) Tj T*" for line in lines) + " ET"
    objects = [
        "<< /Type /Catalog /Pages 2 0 R >>",
        "<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        "<< /Type /Page /Parent 2 0 R /MediaBox [0 0 595 842] /Contents 4 0 R /Resources << /Font << /F1 5 0 R >> >> >>",
        f"<< /Length {len(body)} >>\nstream\n{body}\nendstream",
        "<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    out, offsets = b"%PDF-1.4\n", []
    for i, obj in enumerate(objects, start=1):
        offsets.append(len(out))
        out += f"{i} 0 obj\n{obj}\nendobj\n".encode()
    xref = len(out)
    out += f"xref\n0 {len(objects) + 1}\n0000000000 65535 f \n".encode()
    out += "".join(f"{o:010d} 00000 n \n" for o in offsets).encode()
    out += f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode()
    return out


async def test_a_bank_pdf_is_read_from_its_text_layer_without_ocr():
    pdf = _text_pdf([
        "So tien: 10,000,000 VND", "Thoi gian: 14/11/2026 10:23:45", "Tai khoan nguon: 0123456789",
        "Ten nguoi chuyen: NGUYEN VAN AN", "Tai khoan nhan: 19036789012", "Noi dung: FLAB12CD34",
        "Ma giao dich: FT25318123456789",
    ])
    reader = TesseractReceiptReader(ReceiptOcrSettings(enabled=True), binary="/nonexistent/tesseract")

    reading = await reader.read(content=pdf, content_type="application/pdf")

    assert reading.engine == "pdf-text" and reading.confident is True, reading.notes
    assert reading.fields.amount_vnd == 10_000_000 and reading.fields.memo == "FLAB12CD34"


def _has_vietnamese_tesseract() -> bool:
    if shutil.which("tesseract") is None:
        return False
    langs = subprocess.run(["tesseract", "--list-langs"], capture_output=True, text=True).stdout.split()
    return "vie" in langs


@pytest.mark.skipif(not _has_vietnamese_tesseract(), reason="tesseract with Vietnamese data is not installed here")
async def test_tesseract_reads_a_rendered_receipt():
    from PIL import Image, ImageDraw, ImageFont

    font_path = next(
        (p for p in ("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", "/Library/Fonts/Arial Unicode.ttf",
                     "/System/Library/Fonts/Supplemental/Arial Unicode.ttf") if shutil.os.path.exists(p)),
        None,
    )
    if font_path is None:
        pytest.skip("no Unicode font to render the receipt with")
    font = ImageFont.truetype(font_path, 34)
    image = Image.new("RGB", (1100, 900), "white")
    draw = ImageDraw.Draw(image)
    for i, line in enumerate(RECEIPT.splitlines()):
        draw.text((40, 40 + i * 80), line, fill="black", font=font)
    out = io.BytesIO()
    image.save(out, format="PNG")

    reading = await TesseractReceiptReader(ReceiptOcrSettings(enabled=True)).read(
        content=out.getvalue(), content_type="image/png"
    )

    assert reading.fields is not None, reading.notes
    assert reading.fields.amount_vnd == 10_000_000
    assert reading.fields.payee_account == "19036789012"
    assert reading.payee_bank == "Techcombank"
    assert "FLAB12CD34" in (reading.fields.memo or "")
