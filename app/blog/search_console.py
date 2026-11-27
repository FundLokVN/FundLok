"""Google Search Console figures for blog posts.

Spec: docs/specs/blog/search-console-stats.md.

A daily Cloud Scheduler job (POST /internal/jobs/search-console-sync) asks the
Search Analytics API for clicks, impressions and average position per page per
day, keeps the /blog/<slug> pages, and stores one row per day per slug in
blog_search_daily. Google revises its most recent days, so every run re-reads
the last REFRESH_DAYS and replaces them; the first run reads BACKFILL_DAYS.
The admin posts list then shows each post's totals over the WINDOW_DAYS that
end on the newest day stored.

The same job then asks the URL Inspection API whether each published post is
indexed (blog_index_status), and the editor resubmits the sitemap after a
publish (submit_sitemap), so Google re-reads it sooner. Google offers no
supported way for a blog to request indexing directly: its Indexing API is
for job postings and livestreams only.

CREDENTIALS
The service's own Google identity (Application Default Credentials: the Cloud
Run service account in prod), scoped read-only. No key is stored: the account
is added as a user of the Search Console property instead: Restricted is
enough to read, submitting the sitemap needs Full. The property comes
from settings.SEARCH_CONSOLE_SITE_URL; unset, the sync skips.

All HTTP goes through `_client()` and the token through `_access_token()`,
the seams the tests replace (conftest makes the unpatched ones fail).
"""

import asyncio
import logging
import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from urllib.parse import quote, urlsplit

import httpx
from sqlalchemy import delete, func, insert, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.blog.models import BlogIndexStatus, BlogPost, BlogSearchDaily
from app.core.config import settings

logger = logging.getLogger(__name__)

READ_SCOPE = "https://www.googleapis.com/auth/webmasters.readonly"
# Submitting a sitemap is a write: it needs this scope and a Full (not
# Restricted) user on the property.
WRITE_SCOPE = "https://www.googleapis.com/auth/webmasters"
QUERY_URL = "https://searchconsole.googleapis.com/webmasters/v3/sites/{site}/searchAnalytics/query"
SITEMAP_URL = "https://searchconsole.googleapis.com/webmasters/v3/sites/{site}/sitemaps/{feed}"
INSPECT_URL = "https://searchconsole.googleapis.com/v1/urlInspection/index:inspect"
# URL Inspection allows 2,000 calls a day per property; the least recently
# checked posts go first, so every post is checked within a few days at most.
INSPECT_PER_RUN = 200
WINDOW_DAYS = 28
REFRESH_DAYS = 10
BACKFILL_DAYS = 90
ROW_LIMIT = 25_000
# A post's public address. The canonical one has no language prefix; an older
# /vi/ or /en/ address counts toward the same post.
_POST_PATH = re.compile(r"^/(?:(?:vi|en)/)?blog/([a-z0-9]+(?:-[a-z0-9]+)*)/?$")


class SearchConsoleError(Exception):
    """Google refused or failed the read. Nothing was written."""


@dataclass(frozen=True)
class PostSearchStats:
    clicks: int
    impressions: int
    position: float | None  # None when the post had no impressions


@dataclass(frozen=True)
class SyncStatus:
    configured: bool
    last_synced_at: datetime | None
    data_through: date | None


def site_url() -> str | None:
    return (settings.SEARCH_CONSOLE_SITE_URL or "").strip() or None


def _client() -> httpx.AsyncClient:
    return httpx.AsyncClient(timeout=30.0)


def _access_token(scope: str = READ_SCOPE) -> str:
    """A one-hour token for the service's own Google identity (sync: run in a thread)."""
    import google.auth
    from google.auth.transport.requests import Request

    credentials, _ = google.auth.default(scopes=[scope])
    credentials.refresh(Request())
    return credentials.token


def slug_of(page_url: str) -> str | None:
    """The post slug a reported page belongs to, or None for any other page."""
    return (m := _POST_PATH.match(urlsplit(page_url).path)) and m.group(1)


async def _token(scope: str = READ_SCOPE) -> str:
    try:
        return await asyncio.to_thread(_access_token, scope)
    except Exception as exc:  # noqa: BLE001 - no credentials, or Google refused them
        raise SearchConsoleError(f"no Google credentials: {type(exc).__name__}") from None


async def _read(site: str, start: date, end: date) -> list[dict]:
    token = await _token()
    url = QUERY_URL.format(site=quote(site, safe=""))
    rows: list[dict] = []
    async with _client() as client:
        while True:
            body = {
                "startDate": start.isoformat(),
                "endDate": end.isoformat(),
                "dimensions": ["date", "page"],
                "dimensionFilterGroups": [
                    {"filters": [{"dimension": "page", "operator": "contains", "expression": "/blog/"}]}
                ],
                "rowLimit": ROW_LIMIT,
                "startRow": len(rows),
            }
            try:
                resp = await client.post(url, json=body, headers={"Authorization": f"Bearer {token}"})
            except httpx.HTTPError as exc:
                raise SearchConsoleError(f"Search Console unreachable: {type(exc).__name__}") from None
            if resp.status_code != 200:
                # 403: the service account is not a user of the property.
                raise SearchConsoleError(f"Search Console answered {resp.status_code}")
            page = resp.json().get("rows") or []
            rows.extend(page)
            if len(page) < ROW_LIMIT:
                return rows


def _by_day_and_slug(rows: list[dict]) -> dict[tuple[date, str], list[float]]:
    """[clicks, impressions, position x impressions] per (day, slug): one post can
    be reported under several URLs (trailing slash, an old language prefix)."""
    totals: dict[tuple[date, str], list[float]] = {}
    for row in rows:
        day_text, page = row["keys"]
        slug = slug_of(page)
        if not slug:
            continue
        impressions = int(row.get("impressions", 0))
        t = totals.setdefault((date.fromisoformat(day_text), slug), [0, 0, 0.0])
        t[0] += int(row.get("clicks", 0))
        t[1] += impressions
        t[2] += float(row.get("position", 0.0)) * impressions
    return totals


async def sync(db: AsyncSession, *, today: date | None = None) -> dict:
    """Re-read the recent days from Google and replace them. Raises
    SearchConsoleError when Google fails, having written nothing."""
    site = site_url()
    if not site:
        return {"days": 0, "rows": 0, "skipped": "NOT_CONFIGURED"}
    # Search Console dates are Pacific time and today's are incomplete.
    end = (today or datetime.now(timezone.utc).date()) - timedelta(days=1)
    has_rows = (await db.execute(select(func.count()).select_from(BlogSearchDaily))).scalar_one() > 0
    start = end - timedelta(days=(REFRESH_DAYS if has_rows else BACKFILL_DAYS) - 1)

    totals = _by_day_and_slug(await _read(site, start, end))

    await db.execute(delete(BlogSearchDaily).where(BlogSearchDaily.date.between(start, end)))
    if totals:
        await db.execute(
            insert(BlogSearchDaily),
            [
                {"date": day, "slug": slug, "clicks": int(c), "impressions": int(i), "position_weighted": p}
                for (day, slug), (c, i, p) in totals.items()
            ],
        )
    else:
        # Keep a marker so the list can say when the last sync ran even when
        # no post was seen in search yet.
        await db.execute(
            insert(BlogSearchDaily),
            [{"date": end, "slug": "", "clicks": 0, "impressions": 0, "position_weighted": 0.0}],
        )
    await db.commit()
    return {"days": (end - start).days + 1, "rows": len(totals)}


async def status(db: AsyncSession) -> SyncStatus:
    last_synced_at, data_through = (
        await db.execute(select(func.max(BlogSearchDaily.synced_at), func.max(BlogSearchDaily.date)))
    ).one()
    return SyncStatus(configured=site_url() is not None, last_synced_at=last_synced_at, data_through=data_through)


async def stats_for(db: AsyncSession, slugs: list[str], *, through: date | None) -> dict[str, PostSearchStats]:
    """Each slug's totals over the WINDOW_DAYS ending on `through`. Slugs Google
    never reported are left out."""
    if not slugs or through is None:
        return {}
    since = through - timedelta(days=WINDOW_DAYS - 1)
    rows = await db.execute(
        select(
            BlogSearchDaily.slug,
            func.sum(BlogSearchDaily.clicks),
            func.sum(BlogSearchDaily.impressions),
            func.sum(BlogSearchDaily.position_weighted),
        )
        .where(BlogSearchDaily.slug.in_(slugs), BlogSearchDaily.date.between(since, through))
        .group_by(BlogSearchDaily.slug)
    )
    return {
        slug: PostSearchStats(
            clicks=int(clicks),
            impressions=int(impressions),
            position=round(weighted / impressions, 1) if impressions else None,
        )
        for slug, clicks, impressions, weighted in rows.all()
    }


# --------------------------------------------------------------------------- #
# Sitemap resubmission (on publish)
# --------------------------------------------------------------------------- #


def sitemap_url() -> str:
    return settings.FRONTEND_URL.strip().rstrip("/") + "/sitemap.xml"


def post_url(slug: str) -> str:
    """The canonical public address (fundlok-front-end lib/blog/json-ld.ts blogPostUrl)."""
    return f"{settings.FRONTEND_URL.strip().rstrip('/')}/blog/{slug}"


async def submit_sitemap() -> None:
    """Tell Google the sitemap changed, so it re-reads it sooner. Google still
    decides when to crawl. Raises SearchConsoleError when unset or refused."""
    site = site_url()
    if not site:
        raise SearchConsoleError("NOT_CONFIGURED")
    token = await _token(WRITE_SCOPE)
    url = SITEMAP_URL.format(site=quote(site, safe=""), feed=quote(sitemap_url(), safe=""))
    async with _client() as client:
        try:
            resp = await client.put(url, headers={"Authorization": f"Bearer {token}"})
        except httpx.HTTPError as exc:
            raise SearchConsoleError(f"Search Console unreachable: {type(exc).__name__}") from None
    if resp.status_code not in (200, 204):
        # 403: the service account is a Restricted user, not Full.
        raise SearchConsoleError(f"Search Console answered {resp.status_code}")


# --------------------------------------------------------------------------- #
# Index status (daily, URL Inspection API)
# --------------------------------------------------------------------------- #


def _crawl_time(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


async def inspect_posts(db: AsyncSession) -> dict:
    """Ask Google whether each published post is indexed, least recently
    checked first, and keep its answer. Stops at the first refusal (403/429:
    the rest would be refused too). Each answer is committed as it arrives."""
    site = site_url()
    if not site:
        return {"inspected": 0, "failed": 0}
    rows = await db.execute(
        select(BlogPost.slug)
        .outerjoin(BlogIndexStatus, BlogIndexStatus.slug == BlogPost.slug)
        .where(BlogPost.status == "published")
        .order_by(BlogIndexStatus.checked_at.asc().nulls_first(), BlogPost.published_at.desc())
        .limit(INSPECT_PER_RUN)
    )
    slugs = list(dict.fromkeys(rows.scalars()))
    if not slugs:
        return {"inspected": 0, "failed": 0}

    token = await _token()
    inspected = failed = 0
    async with _client() as client:
        for slug in slugs:
            try:
                resp = await client.post(
                    INSPECT_URL,
                    json={"inspectionUrl": post_url(slug), "siteUrl": site},
                    headers={"Authorization": f"Bearer {token}"},
                )
            except httpx.HTTPError:
                failed += 1
                continue
            if resp.status_code in (401, 403, 429):
                logger.warning("URL Inspection refused (%s); stopping this run", resp.status_code)
                failed += len(slugs) - inspected - failed
                break
            if resp.status_code != 200:
                failed += 1
                continue
            result = (resp.json().get("inspectionResult") or {}).get("indexStatusResult") or {}
            values = {
                "slug": slug,
                "verdict": str(result.get("verdict") or "VERDICT_UNSPECIFIED")[:30],
                "coverage_state": (result.get("coverageState") or None) and result["coverageState"][:200],
                "last_crawl_at": _crawl_time(result.get("lastCrawlTime")),
                "checked_at": func.now(),
            }
            stmt = pg_insert(BlogIndexStatus).values(**values)
            await db.execute(
                stmt.on_conflict_do_update(
                    index_elements=[BlogIndexStatus.slug],
                    set_={k: stmt.excluded[k] for k in ("verdict", "coverage_state", "last_crawl_at", "checked_at")},
                )
            )
            await db.commit()
            inspected += 1
    return {"inspected": inspected, "failed": failed}


async def index_status_for(db: AsyncSession, slugs: list[str]) -> dict[str, BlogIndexStatus]:
    if not slugs:
        return {}
    rows = await db.execute(select(BlogIndexStatus).where(BlogIndexStatus.slug.in_(slugs)))
    return {row.slug: row for row in rows.scalars()}
