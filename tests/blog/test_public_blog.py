"""Public read API used by the Next.js /blog pages (docs/specs/blog/blog-posts.md §4)."""
from tests.blog.helpers import public_media  # noqa: F401 (autouse fixture)
from tests.blog.helpers import article, update_body

POSTS = "/admin/blog/posts"
PUBLIC = "/api/v1/blog/posts"


async def _post(client, admin, title, *, publish):
    post = (await client.post(POSTS, json={"title": title}, headers=admin["headers"])).json()
    post = (
        await client.put(
            f"{POSTS}/{post['id']}",
            json=update_body(post, excerpt="Tóm tắt.", content=article()),
            headers=admin["headers"],
        )
    ).json()
    if publish:
        post = (
            await client.post(f"{POSTS}/{post['id']}/publish", json={"version": post["version"]}, headers=admin["headers"])
        ).json()
    return post


async def test_public_api_hides_drafts(client, make_admin):
    admin = await make_admin()
    live = await _post(client, admin, "Bài đã đăng", publish=True)
    draft = await _post(client, admin, "Bài nháp", publish=False)

    listing = await client.get(PUBLIC)
    assert listing.status_code == 200
    items = listing.json()["items"]
    assert [p["slug"] for p in items] == [live["slug"]]
    assert set(items[0]) == {
        "slug", "title", "excerpt", "cover_image_url", "cover_alt", "published_at", "updated_at", "reading_minutes",
    }

    page = await client.get(f"{PUBLIC}/vi/{live['slug']}")
    assert page.status_code == 200
    assert page.json()["content"] == article()
    assert "id" not in page.json() and "author_id" not in page.json()

    assert (await client.get(f"{PUBLIC}/vi/{draft['slug']}")).status_code == 404
    assert (await client.get(f"{PUBLIC}/en/{live['slug']}")).status_code == 404
    assert (await client.get(f"{PUBLIC}/fr/{live['slug']}")).status_code == 422
    assert (await client.get(f"{PUBLIC}/vi/not.a.slug")).status_code == 422


async def test_unpublished_post_leaves_the_public_api(client, make_admin):
    admin = await make_admin()
    live = await _post(client, admin, "Sắp gỡ", publish=True)
    await client.post(f"{POSTS}/{live['id']}/unpublish", json={"version": live["version"]}, headers=admin["headers"])

    assert (await client.get(PUBLIC)).json()["items"] == []
    assert (await client.get(f"{PUBLIC}/vi/{live['slug']}")).status_code == 404


async def test_public_list_is_newest_first_and_limited(client, make_admin):
    admin = await make_admin()
    first = await _post(client, admin, "Một", publish=True)
    second = await _post(client, admin, "Hai", publish=True)

    items = (await client.get(PUBLIC)).json()["items"]
    assert [p["slug"] for p in items] == [second["slug"], first["slug"]]
    assert len((await client.get(PUBLIC, params={"limit": 1})).json()["items"]) == 1
    older = await client.get(PUBLIC, params={"limit": 1, "offset": 1})
    assert [post["slug"] for post in older.json()["items"]] == [first["slug"]]


async def test_public_views_only_count_for_published_posts_and_preserve_metadata(client, make_admin):
    admin = await make_admin()
    live = await _post(client, admin, "Bài có lượt xem", publish=True)
    draft = await _post(client, admin, "Bài chưa đăng", publish=False)

    client.cookies.clear()
    live_url = f"{PUBLIC}/vi/{live['slug']}"
    assert (await client.get(live_url)).status_code == 200
    assert (await client.post(f"{live_url}/views", headers={"Sec-Fetch-Site": "cross-site"})).status_code == 204
    for _ in range(2):
        assert (await client.post(f"{live_url}/views")).status_code == 204
    assert (await client.post(f"{PUBLIC}/vi/{draft['slug']}/views")).status_code == 204
    assert "view_count" not in (await client.get(live_url)).json()

    items = (await client.get(POSTS, headers=admin["headers"])).json()["items"]
    by_id = {post["id"]: post for post in items}
    assert by_id[live["id"]]["view_count"] == 2
    assert by_id[draft["id"]]["view_count"] == 0
    detail = (await client.get(f"{POSTS}/{live['id']}", headers=admin["headers"])).json()
    assert detail["updated_at"] == live["updated_at"]
    assert detail["version"] == live["version"]
