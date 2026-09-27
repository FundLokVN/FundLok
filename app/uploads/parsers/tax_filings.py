"""The step-3 upload: an SME's tax filings, as one .zip of the folder or as
the year-end statement XML alone.

The folder holds what HTKK produced: one .zip per monthly VAT declaration
(each wrapping a single signed XML, read by app/uploads/parsers/vat.py) and the
year-end financial statement package (app/uploads/parsers/statements.py).

  * The statements are REQUIRED here: they are what step 3's figures come
    from. No statement file is a refusal.
  * The VAT series is a bonus: 24 signed monthly revenue figures, the one
    grading input neither the typed figures nor the 12-month e-invoice export
    can fully supply. A declaration that cannot be read (a quarterly filer, a
    form we do not understand) is a WARNING, not a refusal — it costs the
    series, not the step.

Nothing here refuses a filing because its tax code differs from the
application's company; that is recorded, per the product decision of
2026-09-27 (see app/uploads/service.py).
"""
from __future__ import annotations

import zipfile
from dataclasses import dataclass, field
from io import BytesIO
from typing import Any

from app.uploads.parsers.statements import (
    StatementParseError,
    looks_like_statements,
    parse_statements_xml,
)
from app.uploads.parsers.vat import VatParseError, build_series, parse_declaration_zip

PARSER_VERSION = "tax-filings-1.0"
MAX_ENTRIES = 60
MAX_ENTRY_BYTES = 20 * 1024 * 1024
MAX_TOTAL_BYTES = 100 * 1024 * 1024


class TaxFilingsParseError(ValueError):
    def __init__(self, message: str, code: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass
class TaxFilings:
    statements: dict[str, Any]
    vat: dict[str, Any] | None
    warnings: list[dict[str, str]] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {"statements": self.statements, "vat": self.vat, "warnings": self.warnings}


def _statements(xml: str):
    try:
        return parse_statements_xml(xml)
    except StatementParseError as exc:
        raise TaxFilingsParseError(str(exc), exc.code) from exc


def parse_tax_filings(raw: bytes) -> TaxFilings:
    # A bare statement XML is accepted as-is: some SMEs only have that file.
    if not raw.startswith(b"PK"):
        statements = _statements(raw.decode("utf-8", errors="replace"))
        return TaxFilings(statements=statements.as_dict(), vat=None, warnings=list(statements.warnings))

    try:
        archive = zipfile.ZipFile(BytesIO(raw))
    except zipfile.BadZipFile as exc:
        raise TaxFilingsParseError("The upload is not a readable .zip", "NOT_A_ZIP") from exc

    warnings: list[dict[str, str]] = []
    statement_files: list[tuple[str, str]] = []
    declarations = []
    with archive:
        entries = [
            i for i in archive.infolist()
            if not i.is_dir()
            and not i.filename.startswith("__MACOSX/")
            and not i.filename.rsplit("/", 1)[-1].startswith((".", "~$"))
        ]
        if len(entries) > MAX_ENTRIES:
            raise TaxFilingsParseError(f"The .zip holds {len(entries)} files; at most {MAX_ENTRIES}", "TOO_MANY_FILES")
        if sum(i.file_size for i in entries) > MAX_TOTAL_BYTES:
            raise TaxFilingsParseError("The .zip is too large once extracted", "ARCHIVE_TOO_LARGE")

        for info in entries:
            name = info.filename.rsplit("/", 1)[-1]
            if info.file_size > MAX_ENTRY_BYTES:
                raise TaxFilingsParseError(f"{name} is too large", "ARCHIVE_TOO_LARGE")
            with archive.open(info) as handle:
                data = handle.read(MAX_ENTRY_BYTES + 1)
            if len(data) > MAX_ENTRY_BYTES:
                raise TaxFilingsParseError(f"{name} is too large", "ARCHIVE_TOO_LARGE")
            lower = name.lower()
            if lower.endswith(".zip"):
                # One monthly VAT declaration. The inner zip is opened by the
                # VAT parser, which reads exactly one XML from it.
                try:
                    declarations.append(parse_declaration_zip(data))
                except VatParseError as exc:
                    warnings.append({"code": "VAT_UNREADABLE", "detail": f"{name}: {exc}"})
            elif lower.endswith(".xml"):
                xml = data.decode("utf-8", errors="replace")
                if looks_like_statements(xml):
                    statement_files.append((name, xml))
                else:
                    warnings.append({"code": "IGNORED_FILE", "detail": f"{name}: not a year-end statement"})
            else:
                warnings.append({"code": "IGNORED_FILE", "detail": name})

    if not statement_files:
        raise TaxFilingsParseError(
            "No year-end financial statement (B02 package XML) in the .zip",
            "NO_STATEMENTS",
        )
    # The latest fiscal year wins when an SME includes several.
    parsed = sorted((_statements(xml) for _, xml in statement_files), key=lambda s: s.fiscal_year)
    statements = parsed[-1]
    warnings.extend(statements.warnings)
    if len(parsed) > 1:
        warnings.append({"code": "SEVERAL_STATEMENTS", "detail": f"used fiscal year {statements.fiscal_year}"})

    vat = None
    if declarations:
        try:
            series = build_series(declarations)
        except VatParseError as exc:
            warnings.append({"code": "VAT_UNREADABLE", "detail": str(exc)})
        else:
            vat = {
                "months_covered": series.months_covered,
                "period_start": series.declarations[0].period_label,
                "period_end": series.declarations[-1].period_label,
                "monthly_revenue": [
                    {"period": d.period_label, "revenue_vnd": d.revenue_total} for d in series.declarations
                ],
                "all_signed": series.all_signed,
                "has_gaps": series.has_gaps(),
                "tax_codes": sorted(series.tax_codes),
            }
            if series.has_gaps():
                warnings.append({"code": "VAT_MONTH_GAP", "detail": "the VAT declarations are not consecutive months"})
            if series.tax_codes != {statements.tax_code}:
                warnings.append({
                    "code": "TAX_CODES_DIFFER",
                    "detail": f"VAT under {', '.join(sorted(series.tax_codes))}; statements under {statements.tax_code}",
                })
    return TaxFilings(statements=statements.as_dict(), vat=vat, warnings=warnings)
