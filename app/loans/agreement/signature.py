"""Whether an uploaded agreement carries a PDF digital signature at all.

A presence check, not a validation: it does not verify the certificate chain,
that the signer is the SME's legal representative, or that the signed file is
this application's agreement. FundLok staff check those in review. What it
catches at upload is the likeliest mistake, the unsigned PDF sent straight
back.

Every PDF digital signature (ISO 32000 §12.8, PAdES, and the Vietnamese CA
tools that produce them: VNPT-CA, Viettel-CA, BKAV, FPT-CA ...) is a signature
dictionary with a /ByteRange: the byte spans the signature covers, around the
/Contents hole that holds it. That dictionary cannot sit in a compressed
object stream, because the hole has to be at a fixed byte offset in the file,
so /ByteRange is always present in the raw bytes of a signed PDF. FundLok's
own unsigned download never contains it.
"""
import re

_BYTE_RANGE = re.compile(rb"/ByteRange\s*\[")


def is_pdf(raw: bytes) -> bool:
    # The header may follow up to 1 KB of junk (ISO 32000 §7.5.2 readers allow it).
    return b"%PDF-" in raw[:1024]


def has_digital_signature(raw: bytes) -> bool:
    return is_pdf(raw) and _BYTE_RANGE.search(raw) is not None
