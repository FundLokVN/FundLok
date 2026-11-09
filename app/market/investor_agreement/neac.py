"""Verifying a signed agreement with NEAC, the National Electronic Authentication Centre (Trung tâm Chứng thực điện
tử quốc gia), the authority that recognises Vietnam's digital-signature providers.

NEAC's public "Kiểm tra văn bản ký số" (https://neac.gov.vn/vi) posts the file to POST /vi/Home/VerifyFileByNeac
(multipart `file`) and answers JSON:

    {"signature": {"data": [{
        "issuer": {"cn", "o", "l", "c", "mst", "cccd"},      the CA, e.g. "FPT Certification Authority SHA256"
        "signer": {"cn", "o", "l", "c", "mst", "cccd"},      e.g. cn "CÔNG TY CỔ PHẦN FUNDLOK", cccd "MST:1501167629"
        "intact": "Không bị thay đổi",                         the signed bytes are unchanged
        "certificateValidity": "... Thời điểm có hiệu lực: ...; Thời điểm hết hiệu lực: ...",
        "validity": "Hợp lệ tại thời điểm ký" | "Không hợp lệ",
        "timestampStatus": "Không" | ...,
        "signedTime": "Ngày 20 tháng 10 năm 2026, 17:16:11 [UTC+07:00]",
        "reasonInvalidity": "" | why it is not valid,
        "ocsp": "Chứng thư số chưa bị thu hồi. [http://ocsp...]" | "",
        "crl": "Chứng thư số chưa bị thu hồi [http://crl...]" | "Không thể kiểm tra CRL ..."
     }], "message": "Success", "status": 200}}

or, for a file it cannot read, only {"message": "<b class='text-danger'>Văn bản không có chữ ký số</b>"}.

The values are NEAC's display strings, so `assess` reads them conservatively: a signature passes only when NEAC
says the document is unchanged and the signature valid, gives no reason for invalidity, and reports no revocation.
NEAC publishes no API contract for this endpoint; if its wording changes, signatures fail closed (refused, with
NEAC's own text shown) rather than pass.

Privacy: the investor's signed agreement (name, ID number, address, bank account) is sent to NEAC, a state
authority, for this check. It must be covered by the data-processing consent (IBL to confirm).
"""
from __future__ import annotations

import dataclasses
import html
import os
import re
import unicodedata

import httpx

VERIFY_URL = os.getenv("NEAC_VERIFY_URL", "https://neac.gov.vn/vi/Home/VerifyFileByNeac")
TIMEOUT_SECONDS = float(os.getenv("NEAC_VERIFY_TIMEOUT_SECONDS", "45"))
USER_AGENT = "FundLok-signature-check/1.0"


class NeacUnavailable(Exception):
    """NEAC could not be reached, or answered something that is not its verification JSON."""


@dataclasses.dataclass(frozen=True)
class NeacSignature:
    issuer: dict
    signer: dict
    intact: str
    certificate_validity: str
    validity: str
    timestamp_status: str
    signed_time: str
    reason_invalidity: str
    ocsp: str
    crl: str

    @classmethod
    def from_json(cls, data: dict) -> "NeacSignature":
        def text_(key: str) -> str:
            value = data.get(key)
            return str(value).strip() if value is not None else ""

        return cls(
            issuer={k: v for k, v in (data.get("issuer") or {}).items()},
            signer={k: v for k, v in (data.get("signer") or {}).items()},
            intact=text_("intact"),
            certificate_validity=text_("certificateValidity"),
            validity=text_("validity"),
            timestamp_status=text_("timestampStatus"),
            signed_time=text_("signedTime"),
            reason_invalidity=text_("reasonInvalidity"),
            ocsp=text_("ocsp"),
            crl=text_("crl"),
        )

    def as_dict(self) -> dict:
        return dataclasses.asdict(self)


@dataclasses.dataclass(frozen=True)
class NeacResult:
    message: str  # NEAC's message, HTML stripped ("Success", or why it could not check)
    signatures: list[NeacSignature]


def _plain(value: str) -> str:
    return re.sub(r"<[^>]+>", "", html.unescape(value or "")).strip()


def _fold(value: str) -> str:
    """Lower-case, without Vietnamese diacritics, single spaces: for comparing NEAC's wording and names."""
    value = unicodedata.normalize("NFD", (value or "").replace("đ", "d").replace("Đ", "D"))
    value = "".join(ch for ch in value if unicodedata.category(ch) != "Mn")
    return re.sub(r"\s+", " ", value).strip().lower()


def parse(payload: dict) -> NeacResult:
    signature = payload.get("signature") if isinstance(payload, dict) else None
    if isinstance(signature, dict):
        data = signature.get("data") or []
        return NeacResult(
            message=_plain(str(signature.get("message") or payload.get("message") or "")),
            signatures=[NeacSignature.from_json(item) for item in data if isinstance(item, dict)],
        )
    if isinstance(payload, dict) and "message" in payload:
        return NeacResult(message=_plain(str(payload["message"])), signatures=[])
    raise NeacUnavailable("NEAC answered without a verification result")


async def verify_document(content: bytes, filename: str) -> NeacResult:
    """Send the PDF to NEAC's verification and parse its answer. Raises NeacUnavailable when NEAC cannot be
    reached or answers something else (tests replace this function: they never call NEAC)."""
    try:
        async with httpx.AsyncClient(timeout=TIMEOUT_SECONDS, headers={"User-Agent": USER_AGENT}) as client:
            resp = await client.post(VERIFY_URL, files={"file": (filename, content, "application/pdf")})
    except httpx.HTTPError as exc:
        raise NeacUnavailable(str(exc)) from exc
    if resp.status_code != 200:
        raise NeacUnavailable(f"NEAC answered HTTP {resp.status_code}")
    try:
        payload = resp.json()
    except ValueError as exc:
        raise NeacUnavailable("NEAC answered something other than JSON") from exc
    return parse(payload)


# --------------------------------------------------------------------------- #
# reading NEAC's verdict
# --------------------------------------------------------------------------- #
def problems(sig: NeacSignature) -> list[str]:
    """Why NEAC does not accept this signature, in NEAC's own words; empty when it is valid."""
    out = []
    intact = _fold(sig.intact)
    if not intact.startswith("khong bi thay doi"):
        out.append(f"Tài liệu đã ký: {sig.intact or 'không rõ'}")
    validity = _fold(sig.validity)
    if not validity.startswith("hop le"):
        out.append(f"Chữ ký: {sig.validity or 'không rõ'}")
    if sig.reason_invalidity.strip():
        out.append(f"Lý do không hợp lệ: {sig.reason_invalidity.strip()}")
    for label, value in (("OCSP", sig.ocsp), ("CRL", sig.crl)):
        folded = _fold(value)
        if "thu hoi" in folded and "chua bi thu hoi" not in folded:
            out.append(f"{label}: {value}")
    return out


def identity_tokens(party: dict) -> set[str]:
    """The ID numbers on a certificate subject: "MST:1501167629" -> {"MST", "1501167629"}, branch codes split."""
    joined = " ".join(str(party.get(k) or "") for k in ("mst", "cccd"))
    return set(re.findall(r"[A-Z0-9]+", joined.upper()))


def same_id(sig: NeacSignature, expected: str | None) -> bool:
    if not expected:
        return False
    wanted = re.sub(r"[^A-Z0-9]", "", expected.upper())
    return bool(wanted) and wanted in identity_tokens(sig.signer)


def same_name(sig: NeacSignature, expected: str | None) -> bool:
    return bool(expected) and _fold(str(sig.signer.get("cn") or "")) == _fold(expected)
