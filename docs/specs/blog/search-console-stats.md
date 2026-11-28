# Blog — Google Search Console figures on the admin posts list

Status: IMPLEMENTED (2026-11-27; v2 2026-11-28: index status, sitemap resubmit) · Owner: Phat · Module: `app/blog/search_console.py`

## 1. Context & Goal

The admin blog list shows only on-site view counts. Marketing cannot see which
posts Google shows, how often people click them, or how high they rank, so it
cannot tell which posts to rewrite (ranking but not clicked) or push (near
page 1). This brings Google Search Console's figures next to each post.

v2 adds two things around publishing. Google offers no supported way for a
blog to request indexing (its Indexing API is for job postings and livestreams
only, and sitemap pings were retired in 2023), so instead:

- after a publish the editor **resubmits the sitemap** through the Search
  Console API, so Google re-reads it sooner (Google still decides when);
- the daily job **inspects each published post** (URL Inspection API) and the
  list shows whether Google has indexed it and when it last crawled it, so
  marketing can request indexing by hand for the posts that need it.

## 2. Out of Scope

- Per-query figures (which searches a post ranks for). Possible follow-up: the
  same API with the `query` dimension, shown in the editor.
- Pages outside `/blog/<slug>`.
- Charts or trends over time; the list shows one window.
- Changing the public blog.

## 3. Data Model

`blog_search_daily` (migration `803b4ff6b4ad`):

| column | type | notes |
|---|---|---|
| id | UUID PK | |
| date | DATE | Search Console's day (Pacific time) |
| slug | VARCHAR(80) | the post address; `""` is a marker row written when Google reported no post that run |
| clicks | INTEGER ≥ 0 | |
| impressions | INTEGER ≥ 0 | |
| position_weighted | FLOAT | Σ position × impressions, so averages weight by impressions |
| synced_at | TIMESTAMPTZ | when the row was written |

Unique `(date, slug)`; index `(slug, date)`. Not a foreign key: Google reports
URLs, and history outlives a deleted post.

`blog_index_status` (migration `8cd040e93e0e`): one row per slug, replaced on
each check.

| column | type | notes |
|---|---|---|
| id | UUID PK | |
| slug | VARCHAR(80) UNIQUE | |
| verdict | VARCHAR(30) | Google's verdict: PASS (indexed), NEUTRAL (not indexed), PARTIAL, FAIL, VERDICT_UNSPECIFIED |
| coverage_state | VARCHAR(200) NULL | Google's wording, e.g. "Submitted and indexed" |
| last_crawl_at | TIMESTAMPTZ NULL | |
| checked_at | TIMESTAMPTZ | |

## 4. API Contract

### `POST /admin/blog/sitemap/submit` (admin)

Tells Search Console that `{FRONTEND_URL}/sitemap.xml` changed. Always 200:

```json
{ "submitted": true, "reason": null }
```

`reason` is `NOT_CONFIGURED` (no property) or `REFUSED` (Google said no; the
service account needs **Full** permission). The editor calls it after a
publish, once its cache refresh has made the new post part of the sitemap, and
never blocks the publish on it.

### `GET /admin/blog/posts` (existing; fields added)

Each item gains `search`:

```json
{ "clicks": 5, "impressions": 400, "position": 7.0 }
```

or `null` when Google has not reported the post in the window. `position` is
the impression-weighted average position, one decimal, `null` with no
impressions. CTR is `clicks / impressions`, computed by the client.

The response gains `search_console`:

```json
{ "configured": true, "last_synced_at": "2026-11-27T23:00:04Z", "data_through": "2026-11-26", "window_days": 28 }
```

The window is the `window_days` days ending on `data_through`.

Each item also gains `index_status` (published posts that have been checked;
otherwise `null`):

```json
{ "verdict": "PASS", "coverage_state": "Submitted and indexed", "last_crawl_at": "2026-11-25T10:00:00Z", "checked_at": "2026-11-28T23:00:05Z" }
```

### `POST /internal/jobs/search-console-sync` (Cloud Scheduler only)

Auth as every scheduled job (`app/scheduled_jobs/auth.py`). Response:

```json
{ "job": "search-console-sync", "days": 10, "rows": 14, "inspected": 7, "inspect_failed": 0, "failed": 0, "skipped": null }
```

`skipped: "NOT_CONFIGURED"` while `SEARCH_CONSOLE_SITE_URL` is unset. 502 with
`failed: 1` when Google fails, having written nothing, so the scheduler retries.

## 5. Business Rules

1. The sync reads the Search Analytics API with dimensions `date, page`, pages
   containing `/blog/`, from the day before today back `REFRESH_DAYS` (10), or
   `BACKFILL_DAYS` (90) when the table is empty. It pages through results
   25,000 rows at a time.
2. A page counts toward a post when its path is `/blog/<slug>`, `/vi/blog/<slug>`
   or `/en/blog/<slug>`, with or without a trailing slash; query strings are
   ignored. Figures for the same post and day are summed.
3. Each run deletes the days it re-read and writes them again, in one
   transaction (Google revises recent days).
4. Credentials are the service's own (Application Default Credentials) with the
   `webmasters.readonly` scope. No key is stored.
5. Admin reads stay unaudited (marketing figures, no personal data).
6. After the figures, the job inspects up to 200 published posts, least
   recently checked first, at their canonical `{FRONTEND_URL}/blog/<slug>`.
   Google allows 2,000 inspections a day per property. A 401/403/429 stops
   the run (the rest would be refused too); answers already stored are kept.
   Inspection failures never turn the job into a 502.

## 6. Setup (by hand, once)

1. Search Console → the property → Settings → Users and permissions → add the
   prod-api runtime service account (`1086381007504-compute@developer.gserviceaccount.com`)
   with **Full** permission (Restricted reads figures and index status, but
   cannot submit the sitemap).
1a. Enable `searchconsole.googleapis.com` in the `fundlok` project (done
   2026-11-28; without it every call answers 403).
2. prod-api env: `SEARCH_CONSOLE_SITE_URL` = the property exactly as Google
   names it (`sc-domain:fundlok.com` for a domain property, or
   `https://www.fundlok.com/` for a URL-prefix property).
3. Cloud Scheduler job `search-console-sync`, `0 6 * * *`, Asia/Ho_Chi_Minh,
   `POST https://<prod-api>/internal/jobs/search-console-sync`, OIDC as the
   other jobs (`docs/specs/scheduled_jobs/cloud-scheduler-jobs.md`).

## 7. Error Cases

| case | result |
|---|---|
| property unset | job 200 `skipped: NOT_CONFIGURED`; list `configured: false` |
| no credentials / 403 (account not a user) / 5xx / network | job 502 `failed: 1`, nothing written, earlier figures kept |
| post never in search | its `search` is `null` |

## 8. Acceptance Criteria (tests/blog/test_search_console.py)

- `test_slug_of_keeps_only_blog_post_addresses`
- `test_sync_skips_until_a_property_is_configured`
- `test_first_sync_backfills_and_merges_a_post_s_addresses`
- `test_later_syncs_replace_only_the_recent_days`
- `test_sync_reads_every_page_of_results`
- `test_job_answers_502_and_keeps_old_figures_when_google_fails`
- `test_job_is_only_for_the_scheduler`
- `test_list_shows_each_post_s_last_28_days`
- `test_list_says_when_search_console_is_not_set_up`
- `test_submitting_the_sitemap_tells_google_with_write_access`
- `test_a_refused_sitemap_submit_is_reported_not_raised`
- `test_sitemap_submit_says_when_search_console_is_not_set_up`
- `test_only_admins_submit_the_sitemap`
- `test_the_daily_check_records_which_published_posts_google_indexed`
- `test_a_later_check_replaces_the_answer`
- `test_a_refusal_stops_the_check_and_keeps_earlier_answers`
- `test_the_job_checks_index_status_after_the_figures`

## 9. Open Questions

- The migrations are in `alembic/`, Edward's area: he reviews them.
