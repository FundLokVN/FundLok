# Blog — Google Search Console figures on the admin posts list

Status: IMPLEMENTED (2026-11-27) · Owner: Phat · Module: `app/blog/search_console.py`

## 1. Context & Goal

The admin blog list shows only on-site view counts. Marketing cannot see which
posts Google shows, how often people click them, or how high they rank, so it
cannot tell which posts to rewrite (ranking but not clicked) or push (near
page 1). This brings Google Search Console's figures next to each post.

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

## 4. API Contract

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

### `POST /internal/jobs/search-console-sync` (Cloud Scheduler only)

Auth as every scheduled job (`app/scheduled_jobs/auth.py`). Response:

```json
{ "job": "search-console-sync", "days": 10, "rows": 14, "failed": 0, "skipped": null }
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

## 6. Setup (by hand, once)

1. Search Console → the property → Settings → Users and permissions → add the
   prod-api runtime service account with **Restricted** permission.
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

## 9. Open Questions

- Which Search Console property exists today (domain or URL-prefix)? It decides
  the value of `SEARCH_CONSOLE_SITE_URL`.
- The migration is in `alembic/`, Edward's area: he reviews it.
