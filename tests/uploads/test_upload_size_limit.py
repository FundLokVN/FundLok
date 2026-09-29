"""Upload URLs carry a size limit that storage enforces.

The presigned PUT used to sign only Content-Type, so the per-document size
limit was checked against the size the client DECLARED at presign and nothing
more: the URL itself accepted anything up to R2's 5 GB single-PUT maximum.
The real size was only looked at on confirm, which refused an oversized file
without deleting it, so the file stayed in the bucket.

These tests pin the fix:
  * the URL signs Content-Length, so storage rejects any other size;
  * the size signed is the one validated against the document type's limit;
  * an oversized object found at confirm is deleted as well as refused.
Same for the avatar upload, which shared the helper and the gap.
"""
from urllib.parse import parse_qs, urlparse

import pytest
from sqlalchemy import select

from app.core.config import settings
from app.lending.models import LoanApplicationDocument
from app.uploads.schemas import DOCUMENT_TYPE_RULES
from app.utils import r2
from conftest import auth_headers


# --- the helper itself -----------------------------------------------------


@pytest.fixture
def real_signer(monkeypatch):
    """A real boto3 client with dummy credentials: signing is local, so the
    generated URL can be inspected without any network call."""
    monkeypatch.setattr(settings, "R2_ENDPOINT_URL", "https://example.r2.cloudflarestorage.com")
    monkeypatch.setattr(settings, "R2_ACCESS_KEY_ID", "test-key")
    monkeypatch.setattr(settings, "R2_SECRET_ACCESS_KEY", "test-secret")
    monkeypatch.setattr(settings, "R2_BUCKET", "test-bucket")
    r2._client.cache_clear()
    yield
    r2._client.cache_clear()


def _signed_headers(url: str) -> set[str]:
    return set(parse_qs(urlparse(url).query)["X-Amz-SignedHeaders"][0].split(";"))


def test_presigned_put_signs_the_content_length(real_signer):
    url = r2.presign_put("k.pdf", "application/pdf", content_length=1234)

    # A signed header must be sent with exactly the signed value, or storage
    # rejects the request's signature. That is what enforces the size.
    assert {"content-length", "content-type", "host"} <= _signed_headers(url)


def test_presigned_put_for_different_sizes_differs(real_signer):
    small = r2.presign_put("k.pdf", "application/pdf", content_length=1)
    large = r2.presign_put("k.pdf", "application/pdf", content_length=2)
    sig = lambda u: parse_qs(urlparse(u).query)["X-Amz-Signature"][0]  # noqa: E731
    assert sig(small) != sig(large), "the size must be part of what is signed"


def test_presigned_put_requires_a_size(real_signer):
    with pytest.raises(TypeError):
        r2.presign_put("k.pdf", "application/pdf")  # type: ignore[call-arg]
    for bad in (0, -1):
        with pytest.raises(ValueError):
            r2.presign_put("k.pdf", "application/pdf", content_length=bad)


# --- loan-application documents -------------------------------------------


@pytest.fixture
def storage(monkeypatch):
    state = {"presigned": [], "deleted": [], "head_size": 1024}

    def fake_presign(key, content_type, *, content_length):
        state["presigned"].append((key, content_type, content_length))
        return f"https://r2.test/{key}"

    monkeypatch.setattr("app.uploads.service.presign_put", fake_presign)
    monkeypatch.setattr(
        "app.uploads.service.head_object",
        lambda key, **kw: {"size": state["head_size"], "content_type": "application/pdf"},
    )
    monkeypatch.setattr("app.uploads.service.delete_object", lambda key: state["deleted"].append(key))
    return state


@pytest.fixture
async def draft(make_user, make_project, make_loan_application):
    sme = await make_user(role="SME")
    project = await make_project(sme)
    application = await make_loan_application(sme, project["id"])
    return sme, application["id"]


async def _init(client, sme, app_id, size):
    return await client.post(
        "/uploads/init-upload",
        json={
            "loan_application_id": app_id,
            "document_type": "legal_charter",
            "filename": "charter.pdf",
            "content_type": "application/pdf",
            "size": size,
        },
        headers=auth_headers(sme),
    )


async def test_the_upload_url_is_signed_to_the_declared_size(client, draft, storage):
    sme, app_id = draft

    resp = await _init(client, sme, app_id, 123_456)

    assert resp.status_code == 201, resp.text
    [(key, content_type, length)] = storage["presigned"]
    assert key == resp.json()["file_key"]
    assert content_type == "application/pdf"
    assert length == 123_456


async def test_a_declared_size_over_the_limit_gets_no_url(client, draft, storage):
    sme, app_id = draft

    resp = await _init(client, sme, app_id, DOCUMENT_TYPE_RULES["legal_charter"]["max_size"] + 1)

    assert resp.status_code == 400
    assert storage["presigned"] == []


async def test_the_limit_itself_is_allowed(client, draft, storage):
    sme, app_id = draft
    limit = DOCUMENT_TYPE_RULES["legal_charter"]["max_size"]

    resp = await _init(client, sme, app_id, limit)

    assert resp.status_code == 201, resp.text
    assert storage["presigned"][0][2] == limit


async def test_an_oversized_object_is_deleted_at_confirm(client, draft, storage, db_session):
    sme, app_id = draft
    file_key = (await _init(client, sme, app_id, 1024)).json()["file_key"]
    # Put some other way (or before the URL signed the size): 5 GB in storage.
    storage["head_size"] = 5 * 1024**3

    resp = await client.post(
        "/uploads/confirm",
        json={"loan_application_id": app_id, "file_keys": [file_key]},
        headers=auth_headers(sme),
    )

    assert resp.status_code == 400, resp.text
    assert storage["deleted"] == [file_key], "a refused object must not stay in the bucket"
    doc = (
        await db_session.execute(
            select(LoanApplicationDocument).where(LoanApplicationDocument.file_key == file_key)
        )
    ).scalar_one()
    assert doc.status == "PENDING", "the SME can upload again"


async def test_an_object_within_the_limit_is_confirmed_and_kept(client, draft, storage):
    sme, app_id = draft
    file_key = (await _init(client, sme, app_id, 1024)).json()["file_key"]

    resp = await client.post(
        "/uploads/confirm",
        json={"loan_application_id": app_id, "file_keys": [file_key]},
        headers=auth_headers(sme),
    )

    assert resp.status_code == 200, resp.text
    assert storage["deleted"] == []


# --- avatar ------------------------------------------------------------------


@pytest.fixture
def avatar_storage(monkeypatch):
    state = {"presigned": [], "deleted": [], "head_size": 2048}

    def fake_presign(key, content_type, *, content_length):
        state["presigned"].append((key, content_type, content_length))
        return f"https://r2.test/{key}"

    monkeypatch.setattr("app.users.service.presign_put", fake_presign)
    monkeypatch.setattr(
        "app.users.service.head_object",
        lambda key, **kw: {"size": state["head_size"], "content_type": "image/png"},
    )
    monkeypatch.setattr("app.users.service.delete_object", lambda key: state["deleted"].append(key))
    return state


async def test_the_avatar_url_is_signed_to_the_declared_size(client, make_user, avatar_storage):
    user = await make_user()

    resp = await client.post(
        "/users/me/avatar/presign",
        json={"content_type": "image/png", "size": 2048},
        headers=auth_headers(user),
    )

    assert resp.status_code == 201, resp.text
    assert avatar_storage["presigned"] == [(resp.json()["file_key"], "image/png", 2048)]


async def test_an_oversized_avatar_is_deleted_at_confirm(client, make_user, avatar_storage):
    from app.users.schemas import AVATAR_MAX_SIZE as limit

    user = await make_user()
    key = (
        await client.post(
            "/users/me/avatar/presign",
            json={"content_type": "image/png", "size": 2048},
            headers=auth_headers(user),
        )
    ).json()["file_key"]
    avatar_storage["head_size"] = limit + 1

    resp = await client.post(
        "/users/me/avatar/confirm", json={"file_key": key}, headers=auth_headers(user)
    )

    assert resp.status_code == 400, resp.text
    assert avatar_storage["deleted"] == [key]
