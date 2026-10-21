"""A KYB is approved automatically only for the business's own representative.

Every KYB check before this one proves the certificate is genuine and the
company real, which is just as true of someone ELSE'S certificate: they are
shared with banks and partners, and the tax code is public in the registry.
With GVERIFY_KYB_REQUIRE_REP_MATCH off (the default) nothing tied the applicant
to the business, so an SME got another company's KYB approved, and grading then
read it as the project's KYC/AML pass. Now an automatic approval needs the
applicant's approved KYC to be a legal representative on the certificate, and
the tax code not already verified on another account; otherwise the attempt is
parked for an operator, who can still approve it.
"""
from app.gverify.kyb_service import (
    NOT_A_VERIFIED_REPRESENTATIVE_REVIEW,
    TAX_CODE_TAKEN_REVIEW,
)
from tests.gverify.test_kyb_verify import (  # noqa: F401
    _body,
    make_sme,
    make_verified_sme,
    mock_gverify_kyb,
)


async def _kyb(client, sme):
    resp = await client.post("/gverify/kyb/verify", json=_body(), headers=sme["headers"])
    assert resp.status_code == 201, resp.text
    return resp.json()


async def test_the_verified_representative_is_approved(
    client, make_verified_sme, mock_gverify_kyb  # noqa: F811
):
    data = await _kyb(client, await make_verified_sme())

    assert data["status"] == "APPROVED"


async def test_an_applicant_without_an_approved_identity_is_parked(
    client, make_sme, mock_gverify_kyb  # noqa: F811
):
    data = await _kyb(client, await make_sme())

    assert data["status"] == "MANUAL_REVIEW"
    assert data["rejection_reason"] == NOT_A_VERIFIED_REPRESENTATIVE_REVIEW


async def test_someone_who_is_not_on_the_certificate_is_parked(
    client, make_verified_sme, mock_gverify_kyb  # noqa: F811
):
    data = await _kyb(client, await make_verified_sme(person_number="001099009999"))

    assert data["status"] == "MANUAL_REVIEW"
    assert data["rejection_reason"] == NOT_A_VERIFIED_REPRESENTATIVE_REVIEW


async def test_a_tax_code_verified_on_another_account_is_parked(
    client, make_verified_sme, mock_gverify_kyb  # noqa: F811
):
    first = await _kyb(client, await make_verified_sme())
    assert first["status"] == "APPROVED"

    # Same certificate, same representative, a second account.
    second = await _kyb(client, await make_verified_sme())

    assert second["status"] == "MANUAL_REVIEW"
    assert second["rejection_reason"] == TAX_CODE_TAKEN_REVIEW


async def test_a_differently_spaced_tax_code_is_still_the_same_business(
    client, make_verified_sme, mock_gverify_kyb  # noqa: F811
):
    first = await _kyb(client, await make_verified_sme())
    assert first["status"] == "APPROVED"

    # OCR spacing varies between scans of one certificate; the exact-string
    # comparison let the second account through as a different business.
    code = mock_gverify_kyb.ocr["tax_code"]
    mock_gverify_kyb.ocr = {**mock_gverify_kyb.ocr, "tax_code": f" {code[:4]} {code[4:]} "}
    second = await _kyb(client, await make_verified_sme())

    assert second["status"] == "MANUAL_REVIEW"
    assert second["rejection_reason"] == TAX_CODE_TAKEN_REVIEW
