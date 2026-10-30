"""GET /admin/users/{id} — the admin user preview.

The users LIST was always readable; what this endpoint adds is the per-account
picture: the profile columns, the companies the account owns, its investor
activity and its security posture. Like the project preview, the role check is
the feature — this aggregates one account's world for whoever calls it.
"""
import uuid

from sqlalchemy import select

from app.users.models import User


async def test_returns_the_profile_and_owned_projects(
    client, make_admin, make_user, make_project
):
    sme = await make_user(role="SME")
    project = await make_project(sme, legal_name="CÔNG TY TNHH TEST")
    admin = await make_admin()

    resp = await client.get(f"/admin/users/{sme['id']}", headers=admin["headers"])

    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["email"] == sme["email"]
    assert data["role"] == "SME"
    assert [p["id"] for p in data["projects"]] == [project["id"]]
    assert data["projects"][0]["legal_name"] == "CÔNG TY TNHH TEST"
    assert data["projects"][0]["ownership_role"] == "OWNER"


async def test_has_password_reflects_the_credential_column(
    client, make_admin, make_user, db_session
):
    """Derived, not stored: a Google-OAuth user or a fresh admin invite has NO
    password at all. An operator about to suspend such an account should know
    its owner could not simply sign back in."""
    admin = await make_admin()

    with_password = await make_user(role="INVESTOR")
    resp = await client.get(
        f"/admin/users/{with_password['id']}", headers=admin["headers"]
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["has_password"] is True

    row = (
        await db_session.execute(select(User).where(User.id == with_password["id"]))
    ).scalar_one()
    row.password_hash = None
    await db_session.commit()

    resp = await client.get(
        f"/admin/users/{with_password['id']}", headers=admin["headers"]
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["has_password"] is False


async def test_investor_activity_is_aggregated(client, make_admin, funded_contract):
    """One request answers what the list view cannot: what this account has
    ordered. Amount is integer VND — the column is Numeric(20, 0)."""
    setup = await funded_contract()
    admin = await make_admin()

    resp = await client.get(
        f"/admin/users/{setup['investor']['id']}", headers=admin["headers"]
    )

    assert resp.status_code == 200, resp.text
    investments = resp.json()["investments"]
    assert investments["orders_count"] == 1
    assert investments["orders_amount"] == 10000
    assert isinstance(investments["orders_amount"], int)


async def test_an_account_with_no_activity_is_zeros_not_an_error(
    client, make_admin, make_user
):
    """An SME has no orders; the panel must render empty sections, not fail."""
    sme = await make_user(role="SME")
    admin = await make_admin()

    resp = await client.get(f"/admin/users/{sme['id']}", headers=admin["headers"])

    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["projects"] == []
    assert data["investments"] == {
        "orders_count": 0,
        "orders_amount": 0,
        "holdings_count": 0,
        "holdings_principal": 0,
    }


async def test_unknown_user_is_404(client, make_admin):
    admin = await make_admin()
    resp = await client.get(
        f"/admin/users/{uuid.uuid4()}", headers=admin["headers"]
    )
    assert resp.status_code == 404


async def test_a_non_admin_cannot_read_a_user_detail(client, make_user):
    sme = await make_user(role="SME")
    other = await make_user(role="INVESTOR")

    resp = await client.get(f"/admin/users/{other['id']}", headers=sme["headers"])

    assert resp.status_code == 403, resp.text


async def test_an_anonymous_caller_cannot_read_a_user_detail(client, make_user):
    user = await make_user(role="INVESTOR")

    resp = await client.get(f"/admin/users/{user['id']}")

    assert resp.status_code in (401, 403), resp.text


async def test_user_detail_includes_all_profile_columns_and_security_metrics(
    client, make_admin, make_user, db_session
):
    from datetime import datetime, timezone, timedelta
    from app.banking.models import LinkedAccount
    from app.gverify.models import GVerifyVerification
    from app.users.models import RefreshToken, TotpRecoveryCode, WebAuthnCredential

    admin = await make_admin()
    user_data = await make_user(role="INVESTOR")
    user_id = user_data["id"]

    now = datetime.now(timezone.utc)
    # Populate extra columns in users table
    row = (await db_session.execute(select(User).where(User.id == user_id))).scalar_one()
    row.avatar_key = "avatars/test-avatar.png"
    row.email_signature = "Kind regards,\nTest Investor"
    row.totp_enabled = True
    row.totp_confirmed_at = now
    row.signin_alerts_enabled = True

    # Add related security / activity records
    token = RefreshToken(
        user_id=user_id,
        token_hash="sample_token_hash",
        expires_at=now + timedelta(days=7),
        last_used_at=now,
    )
    db_session.add(token)

    cred = WebAuthnCredential(
        user_id=user_id,
        credential_id="cred_id_123",
        public_key="public_key_pem",
        name="MacBook Touch ID",
    )
    db_session.add(cred)

    recovery = TotpRecoveryCode(
        user_id=user_id,
        code_hash="hashed_recovery_code",
    )
    db_session.add(recovery)

    kyc = GVerifyVerification(
        user_id=user_id,
        verification_type="KYC",
        status="APPROVED",
    )
    db_session.add(kyc)

    account = LinkedAccount(
        user_id=user_id,
        account_type="BANK",
        provider="MOCK",
        account_ref_masked="••••1234",
        status="ACTIVE",
    )
    db_session.add(account)

    await db_session.commit()

    resp = await client.get(f"/admin/users/{user_id}", headers=admin["headers"])
    assert resp.status_code == 200, resp.text
    data = resp.json()

    assert data["avatar_key"] == "avatars/test-avatar.png"
    assert data["email_signature"] == "Kind regards,\nTest Investor"
    assert data["totp_enabled"] is True
    assert data["totp_confirmed_at"] is not None
    # make_user creates 1 session on login; adding token above makes 2
    assert data["active_sessions_count"] == 2
    assert data["last_sign_in_at"] is not None
    assert data["passkeys_count"] == 1
    assert data["unused_recovery_codes_count"] == 1
    assert data["kyc_status"] == "APPROVED"
    assert data["linked_accounts_count"] == 1


async def test_user_detail_includes_project_application_aggregates(
    client, make_admin, make_user, make_project, make_loan_application
):
    sme = await make_user(role="SME")
    project = await make_project(sme, legal_name="CÔNG TY CỔ PHẦN INSIGHT", tax_id="0109999999")
    await make_loan_application(sme, project["id"], requested_amount="30000000")
    await make_loan_application(sme, project["id"], requested_amount="50000000")
    admin = await make_admin()

    resp = await client.get(f"/admin/users/{sme['id']}", headers=admin["headers"])
    assert resp.status_code == 200, resp.text
    data = resp.json()

    assert len(data["projects"]) == 1
    proj = data["projects"][0]
    assert proj["legal_name"] == "CÔNG TY CỔ PHẦN INSIGHT"
    assert proj["tax_id"] == "0109999999"
    assert proj["applications_count"] == 2
    assert proj["total_requested_amount"] == 80000000
    assert isinstance(proj["total_requested_amount"], int)


async def test_user_detail_lists_the_bank_accounts_the_user_saved(client, make_admin, make_user):
    admin = await make_admin()
    investor = await make_user(role="INVESTOR")
    nobody = await make_user(role="SME")
    for body in (
        {"holder_type": "INDIVIDUAL", "bank_bin": "970415", "account_number": "123123123123", "account_name": "HUYNH TAN PHAT"},
        {"holder_type": "INDIVIDUAL", "bank_bin": None, "other_bank_name": "Quỹ tín dụng nhân dân Phú Thọ",
         "account_number": "555000111", "account_name": "HUYNH TAN PHAT"},
    ):
        saved = await client.post("/payment-accounts", json=body, headers=investor["headers"])
        assert saved.status_code == 201, saved.text

    data = (await client.get(f"/admin/users/{investor['id']}", headers=admin["headers"])).json()
    accounts = data["payment_accounts"]
    # Oldest first, in full: an admin checks the account an investor is paid into.
    assert [(a["bank_name"], a["account_number"]) for a in accounts] == [
        ("VietinBank", "123123123123"),
        ("Quỹ tín dụng nhân dân Phú Thọ", "555000111"),
    ]
    assert accounts[0]["bank_bin"] == "970415" and accounts[1]["bank_bin"] is None
    assert all(a["status"] == "PENDING" and a["account_name"] == "HUYNH TAN PHAT" for a in accounts)
    assert all(a["user_email"] == investor["email"] and a["approved_by_email"] is None for a in accounts)

    none = (await client.get(f"/admin/users/{nobody['id']}", headers=admin["headers"])).json()
    assert none["payment_accounts"] == []
