"""SEC-02: every admin read of personal data leaves a VIEW audit row.

See app/admin/access_log.py. The first test is the guard that keeps this
true: a new /admin GET route without `audit_admin_read` fails it.
"""
import contextlib
import uuid

from fastapi.routing import APIRoute
from sqlalchemy import select

from app.admin.access_log import VIEW_ACTION
from app.lending.models import AuditLog
from app.main import app
from conftest import iter_endpoint_routes

# Admin GET routes that return no personal data, so they leave no VIEW row.
NOT_PERSONAL_DATA = {
    "/admin/email/settings": "the signed-in operator's own mailbox configuration",
    "/admin/email/policy": "sending limits and rules, no user data",
    "/admin/funding/analytics": "platform-wide totals and counts, no names or accounts",
    "/admin/email/oauth/authorize": "Google's consent URL for the operator's own mailbox, no user data",
    "/admin/email/oauth/callback": "Google's redirect back; stores the operator's own token, audited as EMAIL_GMAIL_CONNECTED",
}
# Admin-only GET routes mounted outside /admin.
ADMIN_READS_OUTSIDE_ADMIN_PREFIX = {
    "/api/v1/rates/inquiries",
    "/api/v1/rates/inquiries/{inquiry_id}",
}


def _audits_reads(route: APIRoute) -> bool:
    return any(getattr(dep.call, "audits_admin_read", False) for dep in route.dependant.dependencies)


async def _views(db_session) -> list[AuditLog]:
    result = await db_session.execute(
        select(AuditLog).where(AuditLog.action == VIEW_ACTION).order_by(AuditLog.created_at)
    )
    return list(result.scalars())


def test_every_admin_read_route_is_audited():
    routes = [
        route
        for route in iter_endpoint_routes(app.routes)
        if isinstance(route, APIRoute)
        and "GET" in route.methods
        and (route.path.startswith("/admin/") or route.path in ADMIN_READS_OUTSIDE_ADMIN_PREFIX)
    ]
    assert len(routes) > len(NOT_PERSONAL_DATA)

    unaudited = [r.path for r in routes if r.path not in NOT_PERSONAL_DATA and not _audits_reads(r)]
    assert unaudited == [], "add audit_admin_read (app/admin/access_log.py) or exempt with a reason"


async def test_viewing_a_user_record_records_who_what_and_where(
    client, db_session, make_admin, make_user
):
    member = await make_user(role="INVESTOR")
    admin = await make_admin()

    resp = await client.get(f"/admin/users/{member['id']}", headers=admin["headers"])

    assert resp.status_code == 200, resp.text
    [view] = await _views(db_session)
    assert view.entity_type == "USER"
    assert str(view.entity_id) == member["id"]
    assert str(view.actor_id) == admin["id"]
    assert view.after_state == {"path": f"/admin/users/{member['id']}"}
    assert view.ip_address


async def test_a_search_is_recorded_with_its_query(client, db_session, make_admin):
    admin = await make_admin()

    resp = await client.get(
        "/admin/overview", params={"mode": "users", "search": "nguyen"}, headers=admin["headers"]
    )

    assert resp.status_code == 200, resp.text
    [view] = await _views(db_session)
    assert view.entity_type == "ADMIN_OVERVIEW"
    assert view.entity_id is None
    assert view.after_state["query"] == {"mode": "users", "search": "nguyen"}


async def test_an_attempt_on_a_missing_document_is_still_recorded(client, db_session, make_admin):
    """The row records the access, not its outcome."""
    admin = await make_admin()
    document_id = uuid.uuid4()

    resp = await client.get(f"/admin/documents/{document_id}/url", headers=admin["headers"])

    assert resp.status_code == 404, resp.text
    [view] = await _views(db_session)
    assert view.entity_type == "LOAN_APPLICATION_DOCUMENT"
    assert view.entity_id == document_id


async def test_rate_inquiry_reads_are_recorded_on_both_mount_points(client, db_session, make_admin):
    admin = await make_admin()

    for path in ("/admin/rates/inquiries", "/api/v1/rates/inquiries"):
        resp = await client.get(path, headers=admin["headers"])
        assert resp.status_code == 200, resp.text

    views = await _views(db_session)
    assert [(v.entity_type, v.after_state["path"]) for v in views] == [
        ("RATE_INQUIRY", "/admin/rates/inquiries"),
        ("RATE_INQUIRY", "/api/v1/rates/inquiries"),
    ]


async def test_a_non_admin_is_refused_and_leaves_no_row(client, db_session, make_user):
    member = await make_user(role="INVESTOR")

    resp = await client.get(f"/admin/users/{member['id']}", headers=member["headers"])

    assert resp.status_code == 403, resp.text
    assert await _views(db_session) == []


async def test_nothing_is_read_when_the_row_cannot_be_written(
    client, make_admin, make_user, monkeypatch
):
    member = await make_user(role="INVESTOR")
    admin = await make_admin()
    handler_calls = []

    def failing_append_audit(*args, **kwargs):
        raise RuntimeError("audit store unavailable")

    async def spy_get_user_detail(*args, **kwargs):
        handler_calls.append(args)

    monkeypatch.setattr("app.admin.access_log.append_audit", failing_append_audit)
    monkeypatch.setattr("app.admin.service.get_user_detail", spy_get_user_detail)

    # The app's 500 handler may answer, or the transport may re-raise; either
    # way the handler must not have run.
    with contextlib.suppress(RuntimeError):
        resp = await client.get(f"/admin/users/{member['id']}", headers=admin["headers"])
        assert resp.status_code == 500
    assert handler_calls == []
