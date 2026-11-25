"""Read an investor's transfer receipt with OCR, on our own server (docs/specs/direct_transfer/receipt-ocr.md).

The engine is Tesseract, run as a local process inside the API container with the Vietnamese language data
(`tesseract-ocr`, `tesseract-ocr-vie` in the Dockerfile). The receipt never leaves the platform: no external OCR or
AI service is called (Q-025, transfer-and-evidence.md rule 15). A PDF the bank generated is read from its text
layer instead (exact text, no OCR).

`TesseractReceiptReader.read` never raises: any failure (engine missing, timeout, unreadable or oversized image,
see MAX_OCR_PIXELS) is a reading with no fields and a note, which sends the receipt to staff exactly as before OCR
existed.

Switches (environment, read per request so a Cloud Run revision can flip them):
  RECEIPT_OCR_ENABLED            "1" to read receipts at all (default off)
  RECEIPT_AUTO_VERDICT_ENABLED   "1" to let a confident read accept or deny the receipt (default off: the read
                                 is only shown to staff as the system's suggestion)
  RECEIPT_OCR_MIN_CONFIDENCE     0-100, the lowest line confidence a deciding field may have (default 80)
  RECEIPT_OCR_LANGUAGES          Tesseract languages (default "vie+eng")
  RECEIPT_OCR_PSM                Tesseract page segmentation mode (default 6: one block, label and value stay on
                                 one line)
  RECEIPT_OCR_TIMEOUT_SECONDS    per receipt (default 20)
"""
from __future__ import annotations

import asyncio
import dataclasses
import io
import logging
import os
from typing import Any, Protocol

from app.direct_transfer.receipts import ReceiptFields
from app.uploads.parsers.bank_receipt import PARSER_VERSION, OcrLine, parse_receipt_lines

logger = logging.getLogger("app.uploads.receipt_ocr")

NOTE_OCR_FAILED = "OCR_FAILED"  # OCR_FAILED:<why>
NOTE_NO_TEXT = "NO_TEXT"

# The most pixels a receipt image may have, after the small-image upscale, to be decoded and read. The size comes
# from the header, so a bigger image is refused before a pixel is decoded: a 1 MB PNG can claim 13,000 x 13,000
# pixels (~676 MB decoded). Receipts are bank-app screenshots (3-5 MP, a long scrolling one ~12 MP) or phone photos
# (12 MP); 40 MP leaves room for those. A 48-50 MP "full resolution" photo goes to staff, as an unreadable one does.
MAX_OCR_PIXELS = 40_000_000


def _flag(name: str) -> bool:
    return os.getenv(name, "").strip().lower() in ("1", "true", "yes", "on")


@dataclasses.dataclass(frozen=True)
class ReceiptOcrSettings:
    enabled: bool = False
    auto_verdict: bool = False
    min_confidence: float = 80.0
    languages: str = "vie+eng"
    page_seg_mode: int = 6
    timeout_seconds: float = 20.0

    @classmethod
    def from_env(cls) -> "ReceiptOcrSettings":
        return cls(
            enabled=_flag("RECEIPT_OCR_ENABLED"),
            auto_verdict=_flag("RECEIPT_AUTO_VERDICT_ENABLED"),
            min_confidence=float(os.getenv("RECEIPT_OCR_MIN_CONFIDENCE", "80")),
            languages=os.getenv("RECEIPT_OCR_LANGUAGES", "vie+eng"),
            page_seg_mode=int(os.getenv("RECEIPT_OCR_PSM", "6")),
            timeout_seconds=float(os.getenv("RECEIPT_OCR_TIMEOUT_SECONDS", "20")),
        )


@dataclasses.dataclass(frozen=True)
class ReceiptReading:
    """What the system read off one receipt. `confident` is True only when every required field was read cleanly
    enough to decide the receipt on its own (see bank_receipt.ParsedReceipt.blocking_notes)."""

    engine: str
    fields: ReceiptFields | None
    field_confidence: dict[str, float | None]
    confidence: float | None  # the lowest confidence among the required fields read; None = exact text
    confident: bool
    notes: tuple[str, ...]
    payee_bank: str | None = None  # the receiving bank as printed on the receipt

    def to_json(self) -> dict[str, Any]:
        return {
            "engine": self.engine,
            "parser": PARSER_VERSION,
            "fields": (self.fields.to_json() | {"payee_bank": self.payee_bank}) if self.fields else None,
            "field_confidence": self.field_confidence,
            "confidence": self.confidence,
            "confident": self.confident,
            "notes": list(self.notes),
        }


class ReceiptReader(Protocol):
    async def read(self, *, content: bytes, content_type: str) -> ReceiptReading: ...


def reading_from_lines(lines: list[OcrLine], *, engine: str, min_confidence: float) -> ReceiptReading:
    parsed = parse_receipt_lines(lines)
    blocking = parsed.blocking_notes(min_confidence)
    known = [c for c in parsed.field_confidence.values() if c is not None]
    notes = tuple(dict.fromkeys([*parsed.notes, *blocking]))  # ordered, no repeats
    return ReceiptReading(
        engine=engine,
        fields=parsed.fields,
        field_confidence=dict(parsed.field_confidence),
        confidence=min(known) if known else None,
        confident=not blocking,
        notes=notes,
        payee_bank=parsed.payee_bank,
    )


def _failed(engine: str, why: str) -> ReceiptReading:
    return ReceiptReading(
        engine=engine, fields=None, field_confidence={}, confidence=None, confident=False,
        notes=(f"{NOTE_OCR_FAILED}:{why}",),
    )


def tsv_to_lines(tsv: str) -> list[OcrLine]:
    """Tesseract's TSV output -> one OcrLine per text line, its confidence the mean of its words'."""
    rows = tsv.splitlines()
    if not rows:
        return []
    header = rows[0].split("\t")
    try:
        col = {name: header.index(name) for name in ("level", "page_num", "block_num", "par_num", "line_num", "conf", "text")}
    except ValueError:
        return []
    grouped: dict[tuple[str, ...], list[tuple[str, float]]] = {}
    for row in rows[1:]:
        cells = row.split("\t")
        if len(cells) < len(header) or cells[col["level"]] != "5":
            continue
        text = cells[col["text"]].strip()
        if not text:
            continue
        try:
            conf = float(cells[col["conf"]])
        except ValueError:
            continue
        key = tuple(cells[col[k]] for k in ("page_num", "block_num", "par_num", "line_num"))
        grouped.setdefault(key, []).append((text, conf))
    lines = []
    for words in grouped.values():
        confs = [c for _, c in words if c >= 0]
        lines.append(OcrLine(" ".join(t for t, _ in words), sum(confs) / len(confs) if confs else 0.0))
    return lines


class ImageTooLarge(ValueError):
    """The image would be more than MAX_OCR_PIXELS once upscaled; it was not decoded."""


def _prepare_image(content: bytes) -> bytes:
    """Grayscale PNG, upscaled when small (Tesseract reads phone-size text better at ~2x). Raises ImageTooLarge,
    before decoding, for an image over MAX_OCR_PIXELS (Pillow's own DecompressionBombError, raised by `open` above
    ~179 MP, is an exception like any other here)."""
    from PIL import Image

    with Image.open(io.BytesIO(content)) as img:  # reads the header only
        width, height = img.size
        factor = 1 if width >= 1000 else 2 if width >= 500 else 3
        if width * height * factor * factor > MAX_OCR_PIXELS:
            raise ImageTooLarge(f"{width}x{height}")
        img.load()
        gray = img.convert("L")
    if factor > 1:
        gray = gray.resize((gray.width * factor, gray.height * factor), Image.LANCZOS)
    out = io.BytesIO()
    gray.save(out, format="PNG")
    return out.getvalue()


def _pdf_lines(content: bytes) -> list[OcrLine]:
    from pypdf import PdfReader

    reader = PdfReader(io.BytesIO(content))
    text = "\n".join((page.extract_text() or "") for page in reader.pages[:3])
    return [OcrLine(line, None) for line in text.splitlines() if line.strip()]


class TesseractReceiptReader:
    """Reads receipts with the local `tesseract` binary. Also a `ReceiptExtractor` (direct_transfer/receipts.py):
    `extract` returns the fields only when the read is confident."""

    def __init__(self, settings: ReceiptOcrSettings, *, binary: str = "tesseract"):
        self.settings = settings
        self.binary = binary

    @property
    def engine(self) -> str:
        return f"tesseract:{self.settings.languages}:psm{self.settings.page_seg_mode}"

    async def read(self, *, content: bytes, content_type: str) -> ReceiptReading:
        s = self.settings
        if content_type == "application/pdf":
            try:
                lines = await asyncio.to_thread(_pdf_lines, content)
            except Exception:  # a broken PDF is staff's to read
                logger.warning("receipt OCR: unreadable PDF")
                return _failed("pdf-text", "UNREADABLE_PDF")
            if not lines:
                # A scanned PDF without a text layer: staff read it (no rasteriser in the image).
                return dataclasses.replace(_failed("pdf-text", "NO_TEXT_LAYER"), notes=(NOTE_NO_TEXT,))
            return reading_from_lines(lines, engine="pdf-text", min_confidence=s.min_confidence)

        try:
            image = await asyncio.to_thread(_prepare_image, content)
        except ImageTooLarge as exc:
            logger.warning("receipt OCR: image too large to read (%s)", exc)
            return _failed(self.engine, "UNREADABLE_IMAGE")
        except Exception:  # also Pillow's DecompressionBombError (and its warning, where warnings are errors)
            logger.warning("receipt OCR: unreadable image")
            return _failed(self.engine, "UNREADABLE_IMAGE")
        try:
            proc = await asyncio.create_subprocess_exec(
                self.binary, "stdin", "stdout", "-l", s.languages, "--psm", str(s.page_seg_mode), "tsv",
                stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            )
        except FileNotFoundError:
            logger.error("receipt OCR: the tesseract binary is not installed")
            return _failed(self.engine, "ENGINE_MISSING")
        try:
            stdout, _stderr = await asyncio.wait_for(proc.communicate(image), timeout=s.timeout_seconds)
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
            logger.warning("receipt OCR: timed out after %ss", s.timeout_seconds)
            return _failed(self.engine, "TIMEOUT")
        if proc.returncode != 0:
            logger.warning("receipt OCR: tesseract exited %s", proc.returncode)  # stderr may quote the image
            return _failed(self.engine, "ENGINE_ERROR")
        lines = tsv_to_lines(stdout.decode("utf-8", errors="replace"))
        if not lines:
            return dataclasses.replace(_failed(self.engine, "NO_TEXT"), notes=(NOTE_NO_TEXT,))
        return reading_from_lines(lines, engine=self.engine, min_confidence=s.min_confidence)

    async def extract(self, *, content: bytes, content_type: str) -> ReceiptFields | None:
        reading = await self.read(content=content, content_type=content_type)
        return reading.fields if reading.confident else None


@dataclasses.dataclass(frozen=True)
class ReceiptOcr:
    """What the upload route needs: the reader, and whether a confident read decides the receipt."""

    reader: ReceiptReader
    auto_verdict: bool


def receipt_ocr() -> ReceiptOcr | None:
    """FastAPI dependency: None while RECEIPT_OCR_ENABLED is off (staff read every receipt, as before)."""
    settings = ReceiptOcrSettings.from_env()
    if not settings.enabled:
        return None
    return ReceiptOcr(reader=TesseractReceiptReader(settings), auto_verdict=settings.auto_verdict)
