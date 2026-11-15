"""Admin > Transactions > Service fees: each SME activation-fee invoice with
the investment package it is charged on, and staff marking it paid, which
lifts the block on the business's next round. Spec:
docs/specs/admin/fee-invoices-admin.md.

The invoices come from Edward's own round closing (tests/direct_transfer
helpers): 10 investments of 10M, fee 3,000,000 + 300,000 VAT.
"""
import datetime as dt
from uuid import UUID

import pytest
from sqlalchemy import func, select

from app.admin import fees as admin_fees
from app.direct_transfer import fees
from app.direct_transfer.models import FeeInvoice, Transfer
from app.lending.models import AuditLog
from tests.direct_transfer.helpers import at, close_now, fund_window, make_world

M = 1_000_000
LIST = "/admin/funding/fee-invoices"


def _mark(invoice_id) -> str:
    return f"{LIST}/{invoice_id}/mark-paid"


@pytest.fixture(autouse=True)
def before_due(monkeypatch):
    """The fixtures' dates are November 2026; judge "overdue" a day after
    closing, not by the wall clock."""
    monkeypatch.setattr(admin_fees, "_now", lambda: at(days=2))


async def _closed_round(db):
    world = await make_world(db)
    await fund_window(world, [10 * M] * 10)
    await close_now(world, now=at(days=1))
    await db.commit()
    invoice = (
        await db.execute(
            select(FeeInvoice).where(
                FeeInvoice.kind == "SME_ACTIVATION", FeeInvoice.window_id == world.window.id
            )
        )
    ).scalar_one()
    return world, invoice


async def test_staff_see_each_fee_with_its_investment_package(client, db_session, make_admin):
    admin = await make_admin()
    world, invoice = await _closed_round(db_session)

    resp = await client.get(LIST, headers=admin["headers"])
    assert resp.status_code == 200, resp.text
    [row] = resp.json()
    assert row["id"] == str(invoice.id) and row["status"] == "ISSUED"
    assert (row["base_vnd"], row["fee_vnd"], row["vat_vnd"], row["total_vnd"]) == (
        100 * M, 3 * M, 300_000, 3_300_000,
    )
    assert row["reference_code"] == invoice.reference_code
    assert row["is_overdue"] is False and row["email_status"] is None
    # The package: the round, its business, and the investments the 3% is on.
    assert row["window_id"] == str(world.window.id) and row["round_status"] == "REPAYING"
    assert row["project_name"] == world.project.legal_name
    assert row["sme_email"] == world.sme.email
    assert len(row["investments"]) == 10
    assert sum(i["amount_vnd"] for i in row["investments"]) == row["base_vnd"]
    assert all(i["investor_email"] and i["reference_code"] for i in row["investments"])

    # Reading the list is itself on record (SEC-02).
    views = (
        await db_session.execute(select(AuditLog).where(AuditLog.action == "VIEW"))
    ).scalars().all()
    assert any(v.after_state["path"] == LIST for v in views)


async def test_an_unpaid_fee_past_its_due_date_reads_overdue(
    client, db_session, make_admin, monkeypatch
):
    admin = await make_admin()
    await _closed_round(db_session)
    monkeypatch.setattr(admin_fees, "_now", lambda: at(days=30))
    [row] = (await client.get(LIST, headers=admin["headers"])).json()
    assert row["status"] == "ISSUED" and row["is_overdue"] is True


async def test_each_round_shows_its_service_fee(client, db_session, make_admin):
    admin = await make_admin()
    world, invoice = await _closed_round(db_session)
    rounds = (
        await client.get("/admin/funding/rounds", params={"status": "ALL"}, headers=admin["headers"])
    ).json()
    [round_] = [r for r in rounds if r["id"] == str(world.window.id)]
    assert round_["fee_invoice"]["id"] == str(invoice.id)
    assert round_["fee_invoice"]["status"] == "ISSUED"
    assert round_["fee_invoice"]["total_vnd"] == 3_300_000


async def test_marking_paid_records_the_payment_and_lifts_the_block(
    client, db_session, make_admin
):
    admin = await make_admin()
    world, invoice = await _closed_round(db_session)
    after_grace = invoice.due_at + dt.timedelta(seconds=1)  # the block starts after the 7 days (D-047)
    assert await fees.sme_has_unpaid_invoice(db_session, world.sme.id, now=after_grace) is True

    body = {"bank_txn_id": "TCB-20261112-0001", "reason": "Seen on the Techcombank statement"}
    resp = await client.post(_mark(invoice.id), json=body, headers=admin["headers"])
    assert resp.status_code == 200, resp.text
    paid = resp.json()
    assert paid["status"] == "PAID" and paid["paid_bank_txn_id"] == "TCB-20261112-0001"
    assert paid["paid_at"] is not None and paid["is_overdue"] is False

    # Edward's mark_invoice_paid recorded the transfer; the next round may open.
    transfers = (
        await db_session.execute(
            select(Transfer).where(Transfer.kind == "FEE_PAYMENT", Transfer.fee_invoice_id == invoice.id)
        )
    ).scalars().all()
    assert len(transfers) == 1 and transfers[0].amount_vnd == 3_300_000
    assert await fees.sme_has_unpaid_invoice(db_session, world.sme.id, now=after_grace) is False
    audit = (
        await db_session.execute(select(AuditLog).where(AuditLog.action == "FEE_INVOICE_MARKED_PAID"))
    ).scalar_one()
    assert str(audit.actor_id) == admin["id"] and audit.entity_id == invoice.id
    assert audit.after_state["bank_txn_id"] == "TCB-20261112-0001"

    # Pressed again: nothing more is recorded.
    again = await client.post(_mark(invoice.id), json=body, headers=admin["headers"])
    assert again.status_code == 200 and again.json()["status"] == "PAID"
    count = await db_session.scalar(
        select(func.count()).select_from(Transfer).where(Transfer.fee_invoice_id == invoice.id)
    )
    assert count == 1
    audits = await db_session.scalar(
        select(func.count()).select_from(AuditLog).where(AuditLog.action == "FEE_INVOICE_MARKED_PAID")
    )
    assert audits == 1

    # The filters split paid from unpaid.
    unpaid = (await client.get(LIST, params={"status": "UNPAID"}, headers=admin["headers"])).json()
    assert unpaid == []
    paid_list = (await client.get(LIST, params={"status": "PAID"}, headers=admin["headers"])).json()
    assert [r["id"] for r in paid_list] == [str(invoice.id)]


async def test_one_bank_transaction_pays_one_invoice(client, db_session, make_admin):
    admin = await make_admin()
    _, first = await _closed_round(db_session)
    _, second = await _closed_round(db_session)
    body = {"bank_txn_id": "TCB-SAME", "reason": "Statement"}
    assert (await client.post(_mark(first.id), json=body, headers=admin["headers"])).status_code == 200
    resp = await client.post(_mark(second.id), json=body, headers=admin["headers"])
    assert resp.status_code == 409
    assert resp.headers["X-Error-Code"] == "DUPLICATE_BANK_TXN"
    await db_session.refresh(second)
    assert second.status == "ISSUED"


@pytest.mark.parametrize(
    "body",
    [
        {"bank_txn_id": "", "reason": "Statement"},
        {"bank_txn_id": "TCB-1", "reason": "   "},
        {"bank_txn_id": "TCB-1\nX", "reason": "Statement"},
        {"bank_txn_id": "TCB-1"},
        {"bank_txn_id": "TCB-1", "reason": "Statement", "amount_vnd": 1},
    ],
)
async def test_marking_paid_needs_a_transaction_and_a_reason(client, db_session, make_admin, body):
    admin = await make_admin()
    _, invoice = await _closed_round(db_session)
    resp = await client.post(_mark(invoice.id), json=body, headers=admin["headers"])
    assert resp.status_code == 422


async def test_an_unknown_invoice_is_not_found(client, make_admin):
    admin = await make_admin()
    resp = await client.post(
        _mark(UUID(int=0)), json={"bank_txn_id": "TCB-1", "reason": "x"}, headers=admin["headers"]
    )
    assert resp.status_code == 404


async def test_only_staff_see_or_mark_fees(client, db_session, make_user):
    _, invoice = await _closed_round(db_session)
    for role in ("SME", "INVESTOR"):
        user = await make_user(role=role)
        assert (await client.get(LIST, headers=user["headers"])).status_code == 403
        resp = await client.post(
            _mark(invoice.id), json={"bank_txn_id": "T", "reason": "x"}, headers=user["headers"]
        )
        assert resp.status_code == 403
