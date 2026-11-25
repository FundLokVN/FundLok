"""Admin blog workflow: drafts, saves, publishing, images (docs/specs/blog/blog-posts.md §8)."""
from sqlalchemy import select

from app.core.config import settings
from app.lending.models import AuditLog
from tests.blog.helpers import public_media  # noqa: F401 (autouse fixture)
from tests.blog.helpers import IMAGE_URL, article, document, paragraph, text, update_body

POSTS = "/admin/blog/posts"


async def _create(client, admin, title="Lãi suất vay vốn doanh nghiệp 2026"):
    resp = await client.post(POSTS, json={"title": title}, headers=admin["headers"])
    assert resp.status_code == 201, resp.text
    return resp.json()


async def _save(client, admin, post, **overrides):
    return await client.put(f"{POSTS}/{post['id']}", json=update_body(post, **overrides), headers=admin["headers"])


async def _ready(client, admin, **overrides):
    """A draft with everything publish needs."""
    post = await _create(client, admin)
    resp = await _save(client, admin, post, excerpt="Lãi suất tham khảo cho SME.", content=article(), **overrides)
    assert resp.status_code == 200, resp.text
    return resp.json()


async def _publish(client, admin, post):
    return await client.post(f"{POSTS}/{post['id']}/publish", json={"version": post["version"]}, headers=admin["headers"])


async def test_admin_creates_draft_with_slug_from_title(client, make_admin):
    admin = await make_admin()
    post = await _create(client, admin)

    assert post["slug"] == "lai-suat-vay-von-doanh-nghiep-2026"
    assert post["status"] == "draft"
    assert post["locale"] == "vi"
    assert post["version"] == 1
    assert post["published_at"] is None
    assert post["author_id"] == admin["id"]
    assert post["content"] == {"type": "doc", "content": [{"type": "paragraph"}]}


async def test_duplicate_title_gets_suffixed_slug(client, make_admin):
    admin = await make_admin()
    first = await _create(client, admin, "Vay vốn")
    second = await _create(client, admin, "Vay vốn")
    third = await _create(client, admin, "!!!")

    assert (first["slug"], second["slug"]) == ("vay-von", "vay-von-2")
    assert third["slug"] == "bai-viet"


async def test_non_admin_is_refused(client, make_user, make_admin):
    admin = await make_admin()
    post = await _create(client, admin)
    for role in ("INVESTOR", "SME"):
        user = await make_user(role=role)
        headers = user["headers"]
        assert (await client.get(POSTS, headers=headers)).status_code == 403
        assert (await client.post(POSTS, json={"title": "x"}, headers=headers)).status_code == 403
        assert (await client.put(f"{POSTS}/{post['id']}", json=update_body(post), headers=headers)).status_code == 403
        assert (await client.post("/admin/blog/media/presign", json={"content_type": "image/png", "size_bytes": 1}, headers=headers)).status_code == 403
    client.cookies.clear()
    assert (await client.get(POSTS)).status_code == 401


async def test_save_bumps_version_and_rejects_stale_version(client, make_admin):
    admin = await make_admin()
    post = await _create(client, admin)

    saved = await _save(client, admin, post, title="Tiêu đề mới", content=article(), seo_title="  Lãi suất SME  ")
    assert saved.status_code == 200, saved.text
    body = saved.json()
    assert body["version"] == 2
    assert body["title"] == "Tiêu đề mới"
    assert body["seo_title"] == "Lãi suất SME"
    assert body["content"] == article()

    stale = await _save(client, admin, post, title="Ghi đè")
    assert stale.status_code == 409
    assert stale.headers["X-Error-Code"] == "BLOG_VERSION_CONFLICT"


async def test_save_rejects_taken_slug(client, make_admin):
    admin = await make_admin()
    await _create(client, admin, "Vay vốn")
    other = await _create(client, admin, "Khác")

    resp = await _save(client, admin, other, slug="vay-von")
    assert resp.status_code == 409
    assert resp.headers["X-Error-Code"] == "BLOG_SLUG_TAKEN"

    bad = await _save(client, admin, other, slug="has.dot")
    assert bad.status_code == 422


async def test_save_rejects_invalid_content(client, make_admin):
    admin = await make_admin()
    post = await _create(client, admin)
    evil = document(paragraph(text("click", [{"type": "link", "attrs": {"href": "javascript:alert(1)"}}])))

    resp = await _save(client, admin, post, content=evil)
    assert resp.status_code == 422
    assert resp.headers["X-Error-Code"] == "BLOG_CONTENT_INVALID"
    assert "doc.content[0].content[0].marks[0]" in resp.json()["detail"]

    cover = await _save(client, admin, post, cover_image_url="https://evil.example/blog/x.png", cover_alt="A")
    assert cover.status_code == 422
    assert cover.headers["X-Error-Code"] == "BLOG_CONTENT_INVALID"

    ok = await _save(client, admin, post, cover_image_url=IMAGE_URL, cover_alt="Ảnh bìa")
    assert ok.status_code == 200
    assert ok.json()["cover_image_url"] == IMAGE_URL


async def test_publish_requires_title_excerpt_and_body(client, make_admin):
    admin = await make_admin()
    post = await _create(client, admin)

    resp = await _publish(client, admin, post)
    assert resp.status_code == 422
    assert resp.headers["X-Error-Code"] == "BLOG_PUBLISH_INCOMPLETE"
    assert "excerpt" in resp.json()["detail"] and "body" in resp.json()["detail"]


async def test_publish_sets_published_at_and_locks_slug(client, make_admin):
    admin = await make_admin()
    post = await _ready(client, admin)

    resp = await _publish(client, admin, post)
    assert resp.status_code == 200, resp.text
    published = resp.json()
    assert published["status"] == "published"
    assert published["published_at"] is not None
    assert published["version"] == post["version"] + 1

    again = await _publish(client, admin, published)
    assert again.status_code == 200
    assert again.json()["version"] == published["version"]

    renamed = await _save(client, admin, published, slug="dia-chi-moi")
    assert renamed.status_code == 422
    assert renamed.headers["X-Error-Code"] == "BLOG_SLUG_LOCKED"

    edited = await _save(client, admin, published, title="Sửa tiêu đề")
    assert edited.status_code == 200
    assert edited.json()["status"] == "published"


async def test_unpublish_keeps_published_at(client, make_admin):
    admin = await make_admin()
    published = (await _publish(client, admin, await _ready(client, admin))).json()

    resp = await client.post(
        f"{POSTS}/{published['id']}/unpublish", json={"version": published["version"]}, headers=admin["headers"]
    )
    assert resp.status_code == 200, resp.text
    draft = resp.json()
    assert draft["status"] == "draft"
    assert draft["published_at"] == published["published_at"]

    republished = (await _publish(client, admin, draft)).json()
    assert republished["published_at"] == published["published_at"]

    still_locked = await _save(client, admin, draft | {"version": republished["version"]}, slug="khac")
    assert still_locked.headers["X-Error-Code"] == "BLOG_SLUG_LOCKED"


async def test_published_post_cannot_be_deleted(client, make_admin):
    admin = await make_admin()
    published = (await _publish(client, admin, await _ready(client, admin))).json()

    resp = await client.delete(f"{POSTS}/{published['id']}", headers=admin["headers"])
    assert resp.status_code == 409
    assert resp.headers["X-Error-Code"] == "BLOG_DELETE_PUBLISHED"

    draft = await _create(client, admin, "Nháp")
    gone = await client.delete(f"{POSTS}/{draft['id']}", headers=admin["headers"])
    assert gone.status_code == 204
    assert (await client.get(f"{POSTS}/{draft['id']}", headers=admin["headers"])).status_code == 404


async def test_writes_are_audited(client, db_session, make_admin):
    admin = await make_admin()
    post = await _ready(client, admin)
    published = (await _publish(client, admin, post)).json()
    await client.post(f"{POSTS}/{post['id']}/unpublish", json={"version": published["version"]}, headers=admin["headers"])
    await client.delete(f"{POSTS}/{post['id']}", headers=admin["headers"])

    rows = (
        await db_session.execute(
            select(AuditLog).where(AuditLog.entity_type == "BLOG_POST").order_by(AuditLog.created_at, AuditLog.action)
        )
    ).scalars().all()
    assert [r.action for r in rows] == [
        "BLOG_POST_CREATED",
        "BLOG_POST_UPDATED",
        "BLOG_POST_PUBLISHED",
        "BLOG_POST_UNPUBLISHED",
        "BLOG_POST_DELETED",
    ]
    assert all(str(r.actor_id) == admin["id"] for r in rows)
    assert all("content" not in (r.after_state or {}) for r in rows)
    assert rows[2].before_state["status"] == "draft" and rows[2].after_state["status"] == "published"


async def test_list_filters_by_status_and_search(client, make_admin):
    admin = await make_admin()
    await _publish(client, admin, await _ready(client, admin))
    await _create(client, admin, "Hướng dẫn đăng ký 100%")
    await _create(client, admin, "Câu hỏi thường gặp")

    everything = (await client.get(POSTS, headers=admin["headers"])).json()
    assert everything["total"] == 3

    published = (await client.get(POSTS, params={"status": "published"}, headers=admin["headers"])).json()
    assert [p["slug"] for p in published["items"]] == ["lai-suat-vay-von-doanh-nghiep-2026"]

    found = (await client.get(POSTS, params={"search": "đăng ký"}, headers=admin["headers"])).json()
    assert [p["title"] for p in found["items"]] == ["Hướng dẫn đăng ký 100%"]
    by_slug = (await client.get(POSTS, params={"search": "cau-hoi"}, headers=admin["headers"])).json()
    assert by_slug["total"] == 1
    # % is a literal, not a wildcard.
    assert (await client.get(POSTS, params={"search": "%"}, headers=admin["headers"])).json()["total"] == 1

    paged = (await client.get(POSTS, params={"page": 2, "page_size": 2}, headers=admin["headers"])).json()
    assert (paged["page"], paged["page_size"], len(paged["items"])) == (2, 2, 1)

    assert (await client.get(POSTS, params={"status": "archived"}, headers=admin["headers"])).status_code == 422


# --------------------------------------------------------------------------- #
# Images
# --------------------------------------------------------------------------- #


async def test_media_presign_validates_type_and_size(client, make_admin, monkeypatch):
    admin = await make_admin()
    signed = {}

    def fake_presign(key, content_type, *, content_length, bucket=None):
        signed.update(key=key, content_type=content_type, content_length=content_length, bucket=bucket)
        return "https://upload.example/put"

    monkeypatch.setattr("app.blog.service.presign_put", fake_presign)

    resp = await client.post(
        "/admin/blog/media/presign", json={"content_type": "image/webp", "size_bytes": 2048}, headers=admin["headers"]
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["upload_url"] == "https://upload.example/put"
    assert body["public_url"] == f"https://media.example.com/blog/{body['media_id']}.webp"
    assert signed == {
        "key": f"blog/{body['media_id']}.webp",
        "content_type": "image/webp",
        "content_length": 2048,
        "bucket": "fundlok-public-test",
    }

    svg = await client.post(
        "/admin/blog/media/presign", json={"content_type": "image/svg+xml", "size_bytes": 10}, headers=admin["headers"]
    )
    assert svg.status_code == 400
    assert svg.headers["X-Error-Code"] == "BLOG_MEDIA_INVALID"

    huge = await client.post(
        "/admin/blog/media/presign",
        json={"content_type": "image/png", "size_bytes": 5 * 1024 * 1024 + 1},
        headers=admin["headers"],
    )
    assert huge.status_code == 400

    monkeypatch.setattr(settings, "R2_PUBLIC_BUCKET", None)
    off = await client.post(
        "/admin/blog/media/presign", json={"content_type": "image/png", "size_bytes": 10}, headers=admin["headers"]
    )
    assert off.status_code == 503
    assert off.headers["X-Error-Code"] == "BLOG_MEDIA_NOT_CONFIGURED"


async def test_media_confirm_checks_storage(client, db_session, make_admin, monkeypatch):
    admin = await make_admin()
    monkeypatch.setattr("app.blog.service.presign_put", lambda *a, **kw: "https://upload.example/put")
    stored: dict = {}
    deleted: list = []
    monkeypatch.setattr("app.blog.service.head_object", lambda key, bucket=None: stored.get(key))
    monkeypatch.setattr("app.blog.service.delete_object", lambda key, bucket=None: deleted.append((key, bucket)))

    async def presign():
        resp = await client.post(
            "/admin/blog/media/presign", json={"content_type": "image/png", "size_bytes": 100}, headers=admin["headers"]
        )
        return resp.json()

    media = await presign()
    missing = await client.post(f"/admin/blog/media/{media['media_id']}/confirm", headers=admin["headers"])
    assert missing.status_code == 400

    stored[f"blog/{media['media_id']}.png"] = {"size": 100, "content_type": "image/png"}
    ok = await client.post(f"/admin/blog/media/{media['media_id']}/confirm", headers=admin["headers"])
    assert ok.status_code == 200, ok.text
    assert ok.json()["public_url"] == media["public_url"]

    big = await presign()
    key = f"blog/{big['media_id']}.png"
    stored[key] = {"size": 6 * 1024 * 1024, "content_type": "image/png"}
    refused = await client.post(f"/admin/blog/media/{big['media_id']}/confirm", headers=admin["headers"])
    assert refused.status_code == 400
    assert deleted == [(key, "fundlok-public-test")]

    audits = (
        await db_session.execute(select(AuditLog).where(AuditLog.action == "BLOG_MEDIA_UPLOADED"))
    ).scalars().all()
    assert len(audits) == 1
