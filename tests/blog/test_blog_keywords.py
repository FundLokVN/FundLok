"""Focus and secondary keywords for the admin SEO checklist (docs/specs/blog/blog-posts.md v1.1)."""
from tests.blog.helpers import public_media  # noqa: F401 (autouse fixture)
from tests.blog.helpers import article, update_body

POSTS = "/admin/blog/posts"
USAGE = "/admin/blog/keyword-usage"


async def _create(client, admin, title):
    resp = await client.post(POSTS, json={"title": title}, headers=admin["headers"])
    assert resp.status_code == 201, resp.text
    return resp.json()


async def _save(client, admin, post, **overrides):
    return await client.put(f"{POSTS}/{post['id']}", json=update_body(post, **overrides), headers=admin["headers"])


async def test_new_post_has_no_keywords(client, make_admin):
    admin = await make_admin()
    post = await _create(client, admin, "Bài mới")
    assert post["focus_keyword"] is None
    assert post["secondary_keywords"] == []


async def test_keywords_are_saved_trimmed_and_deduplicated(client, make_admin):
    admin = await make_admin()
    post = await _create(client, admin, "Trả góp hằng ngày")

    resp = await _save(
        client,
        admin,
        post,
        focus_keyword="  Vay vốn   trả góp hằng ngày ",
        secondary_keywords=["lãi suất", " Lãi  Suất ", "", "kỳ hạn", "VAY VỐN TRẢ GÓP HẰNG NGÀY", "doanh nghiệp nhỏ"],
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["focus_keyword"] == "Vay vốn trả góp hằng ngày"
    # Repeats (any case) and the focus keyword itself are dropped; order is kept.
    assert body["secondary_keywords"] == ["lãi suất", "kỳ hạn", "doanh nghiệp nhỏ"]

    again = (await client.get(f"{POSTS}/{post['id']}", headers=admin["headers"])).json()
    assert again["focus_keyword"] == "Vay vốn trả góp hằng ngày"
    assert again["secondary_keywords"] == ["lãi suất", "kỳ hạn", "doanh nghiệp nhỏ"]

    cleared = await _save(client, admin, body, focus_keyword="   ", secondary_keywords=[])
    assert cleared.json()["focus_keyword"] is None
    assert cleared.json()["secondary_keywords"] == []


async def test_rejects_more_than_four_secondary_keywords_or_long_ones(client, make_admin):
    admin = await make_admin()
    post = await _create(client, admin, "Nhiều từ khóa")

    too_many = await _save(client, admin, post, secondary_keywords=["a", "b", "c", "d", "e"])
    assert too_many.status_code == 422
    assert "at most 4" in too_many.text

    too_long = await _save(client, admin, post, focus_keyword="x" * 101)
    assert too_long.status_code == 422
    long_secondary = await _save(client, admin, post, secondary_keywords=["y" * 101])
    assert long_secondary.status_code == 422


async def test_keyword_usage_finds_other_posts_case_insensitively(client, make_admin):
    admin = await make_admin()
    first = await _create(client, admin, "Bài một")
    second = await _create(client, admin, "Bài hai")
    third = await _create(client, admin, "Bài ba")
    await _save(client, admin, first, focus_keyword="Vay vốn trả góp hằng ngày")
    await _save(client, admin, second, focus_keyword="vay vốn TRẢ GÓP hằng ngày")
    await _save(client, admin, third, focus_keyword="lãi suất vay vốn")

    resp = await client.get(
        USAGE, params={"keyword": " VAY VỐN trả góp  hằng ngày", "exclude_id": first["id"]}, headers=admin["headers"]
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["keyword"] == "VAY VỐN trả góp hằng ngày"
    assert [p["id"] for p in body["posts"]] == [second["id"]]
    assert set(body["posts"][0]) == {"id", "slug", "title", "status"}

    everyone = (await client.get(USAGE, params={"keyword": "vay vốn trả góp hằng ngày"}, headers=admin["headers"])).json()
    assert {p["id"] for p in everyone["posts"]} == {first["id"], second["id"]}

    nobody = (await client.get(USAGE, params={"keyword": "hồ sơ vay vốn"}, headers=admin["headers"])).json()
    assert nobody["posts"] == []


async def test_keyword_usage_is_admin_only(client, make_user):
    investor = await make_user(role="INVESTOR")
    resp = await client.get(USAGE, params={"keyword": "x"}, headers=investor["headers"])
    assert resp.status_code == 403


async def test_keywords_never_reach_the_public_api(client, make_admin):
    admin = await make_admin()
    post = await _create(client, admin, "Bài công khai")
    saved = (
        await _save(
            client,
            admin,
            post,
            excerpt="Tóm tắt.",
            content=article(),
            focus_keyword="vay vốn",
            secondary_keywords=["lãi suất"],
        )
    ).json()
    await client.post(f"{POSTS}/{post['id']}/publish", json={"version": saved["version"]}, headers=admin["headers"])

    detail = (await client.get(f"/api/v1/blog/posts/vi/{post['slug']}")).json()
    listing = (await client.get("/api/v1/blog/posts")).json()["items"][0]
    for payload in (detail, listing):
        assert "focus_keyword" not in payload and "secondary_keywords" not in payload
