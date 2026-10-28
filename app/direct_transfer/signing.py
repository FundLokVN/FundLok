"""Remote-signing interface and mock (HANDOFF-04 B6). Spec: funding-window.md rules 1, 8.

No vendor code (I-13): `Signer` is the seam; `MockSigner` is deterministic and offline. The signed artefact,
its hash and the certificate fields are stored with the standing offer (SME) and the tranche (investor
acceptance) so what each party signed can be reconstructed.
"""
from __future__ import annotations

import dataclasses
import datetime as dt
import hashlib
import json
from typing import Protocol
from uuid import UUID


@dataclasses.dataclass(frozen=True)
class SignerIdentity:
    user_id: UUID
    display_name: str
    role: str  # 'SME' | 'INVESTOR'


@dataclasses.dataclass(frozen=True)
class SignatureResult:
    provider: str
    signed_content: bytes
    sha256: str
    certificate: dict
    signed_at: dt.datetime


class Signer(Protocol):
    async def sign(self, *, document: bytes, signer: SignerIdentity, now: dt.datetime) -> SignatureResult: ...


class MockSigner:
    """Deterministic fake: appends a signature block, never contacts a provider."""

    provider = "MOCK"

    async def sign(self, *, document: bytes, signer: SignerIdentity, now: dt.datetime) -> SignatureResult:
        doc_hash = hashlib.sha256(document).hexdigest()
        certificate = {
            "provider": self.provider,
            "subject": signer.display_name,
            "subject_user_id": str(signer.user_id),
            "role": signer.role,
            "serial": doc_hash[:16],
            "signature_type": "MOCK_REMOTE_SIGNATURE",
            "signed_at": now.isoformat(),
        }
        block = b"\n--MOCK-SIGNATURE--\n" + json.dumps(certificate, sort_keys=True).encode("utf-8")
        signed = document + block
        return SignatureResult(
            provider=self.provider,
            signed_content=signed,
            sha256=hashlib.sha256(signed).hexdigest(),
            certificate=certificate,
            signed_at=now,
        )


def render_offer_document(terms: dict) -> bytes:
    """Canonical bytes of the terms the SME signs (the legal text is rendered by the front end / counsel;
    this is the machine-readable snapshot that is hashed and stored)."""
    return json.dumps(terms, sort_keys=True, ensure_ascii=False, default=str).encode("utf-8")
