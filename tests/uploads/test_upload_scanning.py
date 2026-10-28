"""SEC-04: uploads are checked by file signature and scanned for malware.

Storage and the job queue are stubbed (conftest.py). The scanner is either a
stub or a fake clamd on a local socket that speaks the INSTREAM protocol, so
no test needs ClamAV or Redis.
"""
import socket
import struct
import threading
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from arq import Retry
from botocore.exceptions import ClientError
from sqlalchemy import select, update

from app.core.config import settings
from app.lending.models import AuditLog, Document, LoanApplicationDocument
from app.uploads import scanning
from app.uploads import service as upload_service
from app.uploads.scanning import (
    MAX_TRIES,
    ScannerUnavailable,
    ScanResult,
    requeue_stalled_scans,
    scan_chunks,
    scan_upload,
)
from app.uploads.signatures import signature_matches
from conftest import auth_headers


# --- signatures ----------------------------------------------------------------


@pytest.mark.parametrize(
    "ext, head",
    [
        ("pdf", b"%PDF-1.7\n"),
        ("pdf", b"\r\n  junk before the header %PDF-1.4"),
        ("zip", b"PK\x03\x04\x14\x00"),
        ("xlsx", b"PK\x03\x04"),
        ("xml", b"\xef\xbb\xbf  <?xml version='1.0'?>"),
        ("csv", b"month,amount\n"),
    ],
)
def test_a_file_that_is_what_its_extension_says_passes(ext, head):
    assert signature_matches(ext, head)


@pytest.mark.parametrize(
    "ext, head",
    [
        ("pdf", b"MZ\x90\x00\x03"),  # a Windows executable named .pdf
        ("pdf", b""),
        ("zip", b"%PDF-1.7"),
        ("zip", b"PK\x05\x06"),  # an empty archive
        ("zip", b"MZ\x90\x00PK\x03\x04"),  # a self-extracting archive
        ("xml", b"\x00\x01binary"),
        ("csv", b"a,b\x00c"),
        ("exe", b"MZ\x90\x00"),  # no known signature: refused, not waved through
    ],
)
def test_a_file_that_is_not_what_its_extension_says_fails(ext, head):
    assert not signature_matches(ext, head)


# --- confirm ---------------------------------------------------------------------


@pytest.fixture
def storage(monkeypatch):
    state = {"deleted": [], "prefix": None, "size": 1024}
    monkeypatch.setattr("app.uploads.service.presign_put", lambda key, ct, **kw: f"https://r2.test/{key}")
    monkeypatch.setattr(
        "app.uploads.service.head_object",
        lambda key, **kw: {"size": state["size"], "content_type": "application/pdf"},
    )
    monkeypatch.setattr("app.uploads.service.delete_object", lambda key: state["deleted"].append(key))
    # A test sets state["prefix"] to store other bytes; otherwise conftest's
    # stub applies (bytes that match the key's extension).
    default_prefix = upload_service.read_prefix
    monkeypatch.setattr(
        upload_service,
        "read_prefix",
        lambda key, length, **kw: state["prefix"] if state["prefix"] is not None else default_prefix(key, length),
    )
    return state


@pytest.fixture
async def draft(make_user, make_project, make_loan_application):
    sme = await make_user(role="SME")
    project = await make_project(sme)
    application = await make_loan_application(sme, project["id"])
    return sme, application["id"]


@pytest.fixture
def queued(monkeypatch):
    calls = []

    async def record(kind, document_id):
        calls.append((kind, document_id))

    monkeypatch.setattr("app.uploads.router.queue_scan", record)
    return calls


async def _upload(client, sme, app_id):
    init = await client.post(
        "/uploads/init-upload",
        json={"loan_application_id": app_id, "document_type": "financial_report", "filename": "report.pdf",
              "content_type": "application/pdf", "size": 1024},
        headers=auth_headers(sme),
    )
    assert init.status_code == 201, init.text
    return init.json()["file_key"]


async def _confirm(client, sme, app_id, key):
    return await client.post(
        "/uploads/confirm", json={"loan_application_id": app_id, "file_keys": [key]}, headers=auth_headers(sme)
    )


async def _doc(db_session, key) -> LoanApplicationDocument:
    doc = (await db_session.execute(select(LoanApplicationDocument).where(LoanApplicationDocument.file_key == key))).scalar_one()
    await db_session.refresh(doc)
    return doc


async def test_a_file_whose_bytes_do_not_match_is_refused_and_deleted(client, db_session, draft, storage):
    sme, app_id = draft
    key = await _upload(client, sme, app_id)
    storage["prefix"] = b"MZ\x90\x00 an executable named report.pdf"

    resp = await _confirm(client, sme, app_id, key)

    assert resp.status_code == 400, resp.text
    assert resp.headers["X-Error-Code"] == "FILE_SIGNATURE_MISMATCH"
    assert storage["deleted"] == [key]
    assert (await _doc(db_session, key)).status == "PENDING"


async def test_an_empty_file_is_refused(client, db_session, draft, storage):
    sme, app_id = draft
    key = await _upload(client, sme, app_id)
    storage["size"] = 0

    resp = await _confirm(client, sme, app_id, key)

    assert resp.status_code == 400, resp.text
    assert storage["deleted"] == [key]


async def test_with_the_scan_off_a_matching_file_is_uploaded(client, db_session, draft, storage, queued):
    sme, app_id = draft
    key = await _upload(client, sme, app_id)

    resp = await _confirm(client, sme, app_id, key)

    assert resp.status_code == 200, resp.text
    assert (await _doc(db_session, key)).status == "UPLOADED"
    assert queued == []


async def test_with_the_scan_on_a_file_waits_and_is_queued(
    client, db_session, draft, storage, queued, make_admin, monkeypatch
):
    monkeypatch.setattr(settings, "UPLOAD_SCAN_ENABLED", True)
    sme, app_id = draft
    key = await _upload(client, sme, app_id)

    resp = await _confirm(client, sme, app_id, key)

    assert resp.status_code == 200, resp.text
    doc = await _doc(db_session, key)
    assert doc.status == "SCANNING"
    assert queued == [("application_document", doc.id)]

    # A reviewer cannot open it until the scan passes.
    admin = await make_admin()
    url = await client.get(f"/admin/documents/{doc.id}/url", headers=auth_headers(admin))
    assert url.status_code == 409, url.text

    # Confirming again does not queue a second scan.
    assert (await _confirm(client, sme, app_id, key)).status_code == 200
    assert len(queued) == 1


# --- the scan job ----------------------------------------------------------------


async def _scanning_document(db_session, *, status="SCANNING") -> Document:
    doc = Document(
        entity_type="PROJECT",
        entity_id=uuid.uuid4(),
        purpose="OTHER",
        filename="statement.pdf",
        mime_type="application/pdf",
        status=status,
        storage_key=f"fundlok/test/{uuid.uuid4()}/statement.pdf",
    )
    db_session.add(doc)
    await db_session.commit()
    return doc


@pytest.fixture
def scanner(monkeypatch):
    state = {"result": ScanResult(clean=True), "error": None, "deleted": []}

    def fake_scan(key):
        if state["error"]:
            raise state["error"]
        return state["result"]

    monkeypatch.setattr(scanning, "_scan_object", fake_scan)
    monkeypatch.setattr(scanning, "delete_object", lambda key: state["deleted"].append(key))
    return state


async def _scan_audit(db_session, doc_id):
    return (
        await db_session.execute(
            select(AuditLog).where(AuditLog.entity_id == doc_id, AuditLog.action == "UPLOAD_SCANNED")
        )
    ).scalar_one()


async def test_a_clean_file_becomes_uploaded(db_session, scanner):
    doc = await _scanning_document(db_session)

    assert await scan_upload({"job_try": 1}, "document", str(doc.id)) == "uploaded"

    await db_session.refresh(doc)
    assert doc.status == "UPLOADED"
    assert scanner["deleted"] == []
    assert (await _scan_audit(db_session, doc.id)).after_state == {"status": "UPLOADED"}


async def test_an_infected_file_is_deleted_and_recorded(db_session, scanner):
    doc = await _scanning_document(db_session)
    scanner["result"] = ScanResult(clean=False, signature="Win.Test.EICAR_HDB-1")

    assert await scan_upload({"job_try": 1}, "document", str(doc.id)) == "infected"

    await db_session.refresh(doc)
    assert doc.status == "INFECTED"
    assert scanner["deleted"] == [doc.storage_key]
    audit = await _scan_audit(db_session, doc.id)
    assert audit.after_state == {"status": "INFECTED", "signature": "Win.Test.EICAR_HDB-1"}


async def test_a_file_over_the_scanner_limits_is_not_called_clean(db_session, scanner):
    doc = await _scanning_document(db_session)
    scanner["result"] = ScanResult(clean=False, signature="Heuristics.Limits.Exceeded.MaxFileSize", limit_exceeded=True)

    assert await scan_upload({"job_try": 1}, "document", str(doc.id)) == "scan_failed"

    await db_session.refresh(doc)
    assert doc.status == "SCAN_FAILED"
    assert scanner["deleted"] == []  # unvetted, but not known to be malware


async def test_an_unreachable_scanner_is_retried_then_recorded(db_session, scanner):
    doc = await _scanning_document(db_session)
    scanner["error"] = ScannerUnavailable("ConnectionRefusedError")

    with pytest.raises(Retry):
        await scan_upload({"job_try": 1}, "document", str(doc.id))
    await db_session.refresh(doc)
    assert doc.status == "SCANNING"

    assert await scan_upload({"job_try": MAX_TRIES}, "document", str(doc.id)) == "scan_failed"
    await db_session.refresh(doc)
    assert doc.status == "SCAN_FAILED"


async def test_a_missing_object_fails_at_once(db_session, scanner):
    doc = await _scanning_document(db_session)
    scanner["error"] = ClientError({"Error": {"Code": "NoSuchKey"}}, "GetObject")

    assert await scan_upload({"job_try": 1}, "document", str(doc.id)) == "scan_failed"
    await db_session.refresh(doc)
    assert doc.status == "SCAN_FAILED"


async def test_a_document_no_longer_scanning_is_left_alone(db_session, scanner):
    doc = await _scanning_document(db_session, status="PENDING")

    assert await scan_upload({"job_try": 1}, "document", str(doc.id)) == "skipped"
    await db_session.refresh(doc)
    assert doc.status == "PENDING"


async def test_stalled_scans_are_queued_again(db_session):
    stalled = await _scanning_document(db_session)
    fresh = await _scanning_document(db_session)
    await db_session.execute(
        update(Document)
        .where(Document.id == stalled.id)
        .values(updated_at=datetime.now(timezone.utc) - timedelta(hours=1))
    )
    await db_session.commit()

    class _Redis:
        def __init__(self):
            self.jobs = []

        async def enqueue_job(self, name, *args):
            self.jobs.append((name, *args))

    redis = _Redis()
    assert await requeue_stalled_scans({"redis": redis}) == 1
    assert redis.jobs == [("scan_upload", "document", str(stalled.id))]
    assert str(fresh.id) not in {job[2] for job in redis.jobs}


# --- generic documents -----------------------------------------------------------


async def test_project_documents_follow_the_same_scan(client, db_session, make_user, make_project, queued, monkeypatch):
    sme = await make_user(role="SME")
    project = await make_project(sme)

    async def presign_and_commit():
        presign = await client.post(
            "/files/presign",
            json={"business_id": project["id"], "purpose": "OTHER", "filename": "a.pdf", "mime_type": "application/pdf"},
            headers=auth_headers(sme),
        )
        file_id = presign.json()["file_id"]
        commit = await client.post(
            f"/files/{file_id}/commit", json={"checksum": "0" * 64, "size": 1024}, headers=auth_headers(sme)
        )
        assert commit.status_code == 200, commit.text
        return file_id, commit.json()["status"]

    # Off: no longer claims SCANNING when nothing scans.
    assert (await presign_and_commit())[1] == "UPLOADED"
    assert queued == []

    monkeypatch.setattr(settings, "UPLOAD_SCAN_ENABLED", True)
    file_id, status = await presign_and_commit()
    assert status == "SCANNING"
    assert queued == [("document", uuid.UUID(file_id))]


@pytest.mark.parametrize("status", ["SCANNING", "INFECTED", "SCAN_FAILED"])
async def test_a_document_without_a_clean_scan_cannot_be_approved(client, db_session, make_admin, status):
    doc = await _scanning_document(db_session, status=status)
    admin = await make_admin()

    resp = await client.post(
        f"/lending/documents/{doc.id}/approve", json={"rationale": "looks fine"}, headers=auth_headers(admin)
    )

    assert resp.status_code == 409, resp.text
    assert resp.headers["X-Error-Code"] == "DOCUMENT_NOT_SCANNED"


# --- the clamd protocol ----------------------------------------------------------


class FakeClamd:
    """One-connection clamd: reads an INSTREAM, answers `reply`."""

    def __init__(self, reply: bytes, *, answer_early: bool = False):
        self.reply, self.answer_early = reply, answer_early
        self.command, self.received = b"", b""
        self._server = socket.socket()
        self._server.bind(("127.0.0.1", 0))
        self._server.listen(1)
        self.port = self._server.getsockname()[1]
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._thread.start()

    @staticmethod
    def _recv_exact(conn, n):
        data = b""
        while len(data) < n:
            chunk = conn.recv(n - len(data))
            if not chunk:
                break
            data += chunk
        return data

    def _serve(self):
        conn, _ = self._server.accept()
        with conn:
            self.command = self._recv_exact(conn, len(b"zINSTREAM\0"))
            if self.answer_early:
                conn.sendall(self.reply)
                return
            while True:
                size = struct.unpack("!I", self._recv_exact(conn, 4))[0]
                if size == 0:
                    break
                self.received += self._recv_exact(conn, size)
            conn.sendall(self.reply)
        self._server.close()

    def join(self):
        self._thread.join(timeout=5)


@pytest.fixture
def clamd_at(monkeypatch):
    def _point(fake: FakeClamd):
        monkeypatch.setattr(settings, "CLAMAV_HOST", "127.0.0.1")
        monkeypatch.setattr(settings, "CLAMAV_PORT", fake.port)
        monkeypatch.setattr(settings, "CLAMAV_TIMEOUT_SECONDS", 5)
        return fake

    return _point


def test_a_clean_stream_is_sent_whole_and_passes(clamd_at):
    fake = clamd_at(FakeClamd(b"stream: OK\0"))

    result = scan_chunks([b"%PDF-1.7 ", b"", b"rest of the file"])

    fake.join()
    assert result == ScanResult(clean=True)
    assert fake.command == b"zINSTREAM\0"
    assert fake.received == b"%PDF-1.7 rest of the file"


def test_malware_is_named(clamd_at):
    clamd_at(FakeClamd(b"stream: Win.Test.EICAR_HDB-1 FOUND\0"))
    assert scan_chunks([b"X5O!P%@AP"]) == ScanResult(clean=False, signature="Win.Test.EICAR_HDB-1")


def test_a_limits_hit_is_not_malware(clamd_at):
    clamd_at(FakeClamd(b"stream: Heuristics.Limits.Exceeded.MaxFileSize FOUND\0"))
    result = scan_chunks([b"big"])
    assert result.limit_exceeded and not result.clean


def test_clamd_cutting_off_a_stream_too_long_is_a_limits_hit(clamd_at):
    clamd_at(FakeClamd(b"INSTREAM size limit exceeded. ERROR\0", answer_early=True))
    result = scan_chunks([b"x" * 65536 for _ in range(64)])
    assert result.limit_exceeded and not result.clean


def test_an_error_reply_is_no_verdict(clamd_at):
    clamd_at(FakeClamd(b"Something broke. ERROR\0"))
    with pytest.raises(ScannerUnavailable):
        scan_chunks([b"data"])


def test_no_clamd_is_no_verdict(monkeypatch):
    closed = socket.socket()
    closed.bind(("127.0.0.1", 0))
    port = closed.getsockname()[1]
    closed.close()
    monkeypatch.setattr(settings, "CLAMAV_HOST", "127.0.0.1")
    monkeypatch.setattr(settings, "CLAMAV_PORT", port)

    with pytest.raises(ScannerUnavailable):
        scan_chunks([b"data"])
