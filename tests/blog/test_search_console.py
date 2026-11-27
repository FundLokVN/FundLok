"""Google Search Console figures on the admin posts list
(docs/specs/blog/search-console-stats.md).

Google is never contacted: `_access_token` and `_client` are replaced by a
fake Search Analytics API that answers from `FakeGoogle.rows`.
"""
import datetime as dt

import httpx
import pytest
from sqlalchemy import func, select

from app.blog import search_console
from app.blog.models import BlogIndexStatus, BlogSearchDaily
from app.core.config import settings
from tests.blog.helpers import public_media  # noqa: F401 (autouse fixture)
from tests.blog.test_admin_blog import _publish, _ready
from tests.scheduled_jobs.test_cloud_scheduler_jobs import scheduler  # noqa: F401 (fixture)

POSTS = "/admin/blog/posts"
SYNC = "/internal/jobs/search-console-sync"
SITE = "sc-domain:fundlok.com"
TODAY = dt.date(2026, 11, 27)
END = TODAY - dt.timedelta(days=1)


class FakeGoogle:
    def __init__(self):
        self.rows: list[dict] = []
        self.status = 200
        self.requests: list[dict] = []
        # URL Inspection: indexStatusResult per inspected URL; status per call.
        self.inspections: dict[str, dict] = {}
        self.inspect_status = 200
        self.sitemap_status = 200
        self.sitemaps: list[str] = []
        self.inspected: list[str] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        import json

        url = str(request.url)
        auth = request.headers.get("authorization")
        if "/sitemaps/" in url:
            self.sitemaps.append(url + " " + auth)
            return httpx.Response(self.sitemap_status)
        body = json.loads(request.content)
        if "urlInspection" in url:
            self.inspected.append(body["inspectionUrl"])
            if self.inspect_status != 200:
                return httpx.Response(self.inspect_status, json={"error": {}})
            result = self.inspections.get(body["inspectionUrl"], {"verdict": "NEUTRAL", "coverageState": "URL is unknown to Google"})
            return httpx.Response(200, json={"inspectionResult": {"indexStatusResult": result}})
        self.requests.append({"url": url, "body": body, "auth": auth})
        if self.status != 200:
            return httpx.Response(self.status, json={"error": {"code": self.status}})
        start = body["startRow"]
        return httpx.Response(200, json={"rows": self.rows[start : start + body["rowLimit"]]})


@pytest.fixture
def google(monkeypatch):
    fake = FakeGoogle()
    monkeypatch.setattr(settings, "SEARCH_CONSOLE_SITE_URL", SITE)
    monkeypatch.setattr(settings, "FRONTEND_URL", "https://www.fundlok.com")
    monkeypatch.setattr(
        search_console,
        "_access_token",
        lambda scope=search_console.READ_SCOPE: "sa-token" if scope == search_console.READ_SCOPE else "sa-write-token",
    )
    monkeypatch.setattr(
        search_console, "_client", lambda: httpx.AsyncClient(transport=httpx.MockTransport(fake.handler))
    )
    return fake


def row(day: dt.date, page: str, clicks: int, impressions: int, position: float) -> dict:
    return {"keys": [day.isoformat(), page], "clicks": clicks, "impressions": impressions, "position": position}


async def _post(client, admin, title: str) -> dict:
    resp = await client.post(POSTS, json={"title": title}, headers=admin["headers"])
    assert resp.status_code == 201, resp.text
    return resp.json()


async def _list(client, admin) -> dict:
    resp = await client.get(POSTS, headers=admin["headers"])
    assert resp.status_code == 200, resp.text
    return resp.json()


async def test_slug_of_keeps_only_blog_post_addresses():
    assert search_console.slug_of("https://www.fundlok.com/blog/vay-von") == "vay-von"
    assert search_console.slug_of("https://www.fundlok.com/blog/vay-von/") == "vay-von"
    assert search_console.slug_of("https://fundlok.com/en/blog/vay-von?utm_source=x") == "vay-von"
    assert search_console.slug_of("https://www.fundlok.com/blog") is None
    assert search_console.slug_of("https://www.fundlok.com/blog/a/b") is None
    assert search_console.slug_of("https://www.fundlok.com/investors") is None


async def test_sync_skips_until_a_property_is_configured(db_session, monkeypatch):
    monkeypatch.setattr(settings, "SEARCH_CONSOLE_SITE_URL", None)
    assert await search_console.sync(db_session, today=TODAY) == {"days": 0, "rows": 0, "skipped": "NOT_CONFIGURED"}


async def test_first_sync_backfills_and_merges_a_post_s_addresses(db_session, google):
    google.rows = [
        row(END, "https://www.fundlok.com/blog/vay-von", 3, 100, 4.0),
        row(END, "https://www.fundlok.com/blog/vay-von/", 1, 300, 8.0),
        row(END, "https://www.fundlok.com/blog", 9, 900, 2.0),
    ]
    result = await search_console.sync(db_session, today=TODAY)

    assert result == {"days": search_console.BACKFILL_DAYS, "rows": 1}
    sent = google.requests[0]
    assert sent["url"].endswith("/sites/sc-domain%3Afundlok.com/searchAnalytics/query")
    assert sent["auth"] == "Bearer sa-token"
    assert sent["body"]["endDate"] == END.isoformat()
    assert sent["body"]["dimensions"] == ["date", "page"]
    stored = (await db_session.execute(select(BlogSearchDaily))).scalars().all()
    assert [(r.slug, r.clicks, r.impressions, r.position_weighted) for r in stored] == [
        ("vay-von", 4, 400, 100 * 4.0 + 300 * 8.0)
    ]


async def test_later_syncs_replace_only_the_recent_days(db_session, google):
    old_day = END - dt.timedelta(days=40)
    google.rows = [row(old_day, "https://www.fundlok.com/blog/a", 1, 10, 5.0), row(END, "https://www.fundlok.com/blog/a", 1, 10, 5.0)]
    await search_console.sync(db_session, today=TODAY)

    google.rows = [row(END, "https://www.fundlok.com/blog/a", 7, 70, 3.0)]  # Google revised the last day
    result = await search_console.sync(db_session, today=TODAY)

    assert result["days"] == search_console.REFRESH_DAYS
    assert google.requests[-1]["body"]["startDate"] == (END - dt.timedelta(days=search_console.REFRESH_DAYS - 1)).isoformat()
    stored = {(r.date, r.clicks) for r in (await db_session.execute(select(BlogSearchDaily))).scalars()}
    assert stored == {(old_day, 1), (END, 7)}


async def test_sync_reads_every_page_of_results(db_session, google, monkeypatch):
    monkeypatch.setattr(search_console, "ROW_LIMIT", 2)
    google.rows = [row(END - dt.timedelta(days=i), "https://www.fundlok.com/blog/a", 1, 10, 5.0) for i in range(5)]
    assert (await search_console.sync(db_session, today=TODAY))["rows"] == 5
    assert [r["body"]["startRow"] for r in google.requests] == [0, 2, 4]


async def test_job_answers_502_and_keeps_old_figures_when_google_fails(client, db_session, google, scheduler):  # noqa: F811
    google.rows = [row(END, "https://www.fundlok.com/blog/a", 1, 10, 5.0)]
    await search_console.sync(db_session, today=TODAY)
    google.status = 403  # the service account is not a user of the property

    client.cookies.clear()
    resp = await client.post(SYNC, headers={"Authorization": "Bearer valid"})

    assert resp.status_code == 502
    assert resp.json() == {
        "job": "search-console-sync",
        "days": 0,
        "rows": 0,
        "inspected": 0,
        "inspect_failed": 0,
        "failed": 1,
        "skipped": None,
    }
    assert (await db_session.execute(select(func.count()).select_from(BlogSearchDaily))).scalar_one() == 1


async def test_job_is_only_for_the_scheduler(client, google, scheduler):  # noqa: F811
    client.cookies.clear()
    assert (await client.post(SYNC)).status_code == 401
    resp = await client.post(SYNC, headers={"Authorization": "Bearer valid"})
    assert resp.status_code == 200, resp.text
    assert resp.json()["job"] == "search-console-sync"


async def test_list_shows_each_post_s_last_28_days(client, db_session, make_admin, google):
    admin = await make_admin()
    ranked = await _post(client, admin, "Vay von")
    unseen = await _post(client, admin, "Chua len Google")
    page = f"https://www.fundlok.com/blog/{ranked['slug']}"
    google.rows = [
        row(END, page, 2, 100, 4.0),
        row(END - dt.timedelta(days=27), page, 3, 300, 8.0),
        row(END - dt.timedelta(days=28), page, 50, 5000, 1.0),  # outside the window
    ]
    await search_console.sync(db_session, today=TODAY)

    body = await _list(client, admin)

    assert body["search_console"]["configured"] is True
    assert body["search_console"]["data_through"] == END.isoformat()
    assert body["search_console"]["window_days"] == 28
    assert body["search_console"]["last_synced_at"] is not None
    by_id = {item["id"]: item for item in body["items"]}
    assert by_id[ranked["id"]]["search"] == {"clicks": 5, "impressions": 400, "position": 7.0}
    assert by_id[unseen["id"]]["search"] is None


async def test_list_says_when_search_console_is_not_set_up(client, make_admin, monkeypatch):
    monkeypatch.setattr(settings, "SEARCH_CONSOLE_SITE_URL", None)
    admin = await make_admin()
    await _post(client, admin, "Bai viet")

    body = await _list(client, admin)

    assert body["search_console"] == {
        "configured": False,
        "last_synced_at": None,
        "data_through": None,
        "window_days": 28,
    }
    assert body["items"][0]["search"] is None


# --------------------------------------------------------------------------- #
# Sitemap resubmission and index status
# --------------------------------------------------------------------------- #


async def _published(client, admin, title: str) -> dict:
    post = await _ready(client, admin, title=title)
    resp = await _publish(client, admin, post)
    assert resp.status_code == 200, resp.text
    return resp.json()


async def test_submitting_the_sitemap_tells_google_with_write_access(client, make_admin, google):
    admin = await make_admin()
    resp = await client.post("/admin/blog/sitemap/submit", headers=admin["headers"])

    assert resp.status_code == 200, resp.text
    assert resp.json() == {"submitted": True, "reason": None}
    assert google.sitemaps == [
        "https://searchconsole.googleapis.com/webmasters/v3/sites/sc-domain%3Afundlok.com/sitemaps/"
        "https%3A%2F%2Fwww.fundlok.com%2Fsitemap.xml Bearer sa-write-token"
    ]


async def test_a_refused_sitemap_submit_is_reported_not_raised(client, make_admin, google):
    admin = await make_admin()
    google.sitemap_status = 403  # the service account is Restricted, not Full

    resp = await client.post("/admin/blog/sitemap/submit", headers=admin["headers"])

    assert resp.status_code == 200
    assert resp.json() == {"submitted": False, "reason": "REFUSED"}


async def test_sitemap_submit_says_when_search_console_is_not_set_up(client, make_admin, monkeypatch):
    monkeypatch.setattr(settings, "SEARCH_CONSOLE_SITE_URL", None)
    admin = await make_admin()
    resp = await client.post("/admin/blog/sitemap/submit", headers=admin["headers"])
    assert resp.json() == {"submitted": False, "reason": "NOT_CONFIGURED"}


async def test_only_admins_submit_the_sitemap(client, make_user, google):
    user = await make_user(role="INVESTOR")
    resp = await client.post("/admin/blog/sitemap/submit", headers=user["headers"])
    assert resp.status_code == 403
    assert google.sitemaps == []


async def test_the_daily_check_records_which_published_posts_google_indexed(client, db_session, make_admin, google):
    admin = await make_admin()
    live = await _published(client, admin, "Lai suat vay")
    draft = await _post(client, admin, "Ban nhap")
    url = f"https://www.fundlok.com/blog/{live['slug']}"
    google.inspections[url] = {
        "verdict": "PASS",
        "coverageState": "Submitted and indexed",
        "lastCrawlTime": "2026-11-25T10:00:00Z",
    }

    result = await search_console.inspect_posts(db_session)

    assert result == {"inspected": 1, "failed": 0}
    assert google.inspected == [url]  # drafts are never inspected
    body = await _list(client, admin)
    by_id = {item["id"]: item for item in body["items"]}
    status = by_id[live["id"]]["index_status"]
    assert status["verdict"] == "PASS"
    assert status["coverage_state"] == "Submitted and indexed"
    assert status["last_crawl_at"].startswith("2026-11-25T10:00:00")
    assert by_id[draft["id"]]["index_status"] is None


async def test_a_later_check_replaces_the_answer(client, db_session, make_admin, google):
    admin = await make_admin()
    live = await _published(client, admin, "Lai suat vay")
    await search_console.inspect_posts(db_session)
    google.inspections[f"https://www.fundlok.com/blog/{live['slug']}"] = {"verdict": "PASS", "coverageState": "Submitted and indexed"}

    await search_console.inspect_posts(db_session)

    rows = (await db_session.execute(select(BlogIndexStatus))).scalars().all()
    assert [(r.slug, r.verdict) for r in rows] == [(live["slug"], "PASS")]


async def test_a_refusal_stops_the_check_and_keeps_earlier_answers(client, db_session, make_admin, google):
    admin = await make_admin()
    await _published(client, admin, "Bai mot")
    await _published(client, admin, "Bai hai")
    await search_console.inspect_posts(db_session)
    google.inspect_status = 429
    google.inspected.clear()

    result = await search_console.inspect_posts(db_session)

    assert result == {"inspected": 0, "failed": 2}
    assert len(google.inspected) == 1  # stopped at the first refusal
    assert (await db_session.execute(select(func.count()).select_from(BlogIndexStatus))).scalar_one() == 2


async def test_the_job_checks_index_status_after_the_figures(client, make_admin, google, scheduler):  # noqa: F811
    admin = await make_admin()
    await _published(client, admin, "Lai suat vay")
    client.cookies.clear()

    resp = await client.post(SYNC, headers={"Authorization": "Bearer valid"})

    assert resp.status_code == 200, resp.text
    assert resp.json()["inspected"] == 1
    assert resp.json()["inspect_failed"] == 0
