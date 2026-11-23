# Spec: Blog posts (admin editor + public read API)

| Field | Value |
|---|---|
| **Status** | DRAFT |
| **Owner** | Phat |
| **Implementer(s)** | Phat (backend + frontend) |
| **Module** | `app/blog/` |
| **Version** | 1.1 |
| **Date** | 2026-11-23 |
| **Related ADR** | — |
| **Depends on** | — |

---

## 1. Context & Goal

FundLok needs a blog that ranks in Google for the questions SMEs actually search
("lãi suất vay vốn doanh nghiệp", ...). Admins write posts in the admin area with
a rich-text editor (TipTap); the public Next.js site renders published posts on
the server with full SEO metadata (`/blog`, `/blog/<slug>`).

Content is stored as TipTap JSON only. The backend checks every node against an
allowlist, so the public site renders it from a fixed schema and never inserts
raw HTML.

**Goal:** CRUD + publish workflow for blog posts, image upload to a public
bucket, and an unauthenticated read API for published posts.

---

## 2. Out of Scope

- English posts (the `locale` column exists, the UI and public site are Vietnamese only).
- Tables, code blocks, embeds/video in post content.
- Changing a slug after first publish (would need a redirect table).
- Scheduled publishing, autosave, revision history.
- Categories/tags, comments, search.
- Cleaning up images no longer referenced by any post.

---

## 3. Data Model

### New Table: `blog_posts`

```
blog_posts
├── id               UUID          PK
├── locale           VARCHAR(5)    NOT NULL DEFAULT 'vi', CHECK IN ('vi', 'en')
├── slug             VARCHAR(80)   NOT NULL, CHECK slug ~ '^[a-z0-9]+(-[a-z0-9]+)*$'
├── title            VARCHAR(200)  NOT NULL
├── seo_title        VARCHAR(70)   nullable          -- <title> override; falls back to title
├── excerpt          VARCHAR(300)  NOT NULL DEFAULT '' -- meta description
├── content          JSONB         NOT NULL          -- TipTap document, validated (§6)
├── reading_minutes  INTEGER       NOT NULL DEFAULT 1, CHECK > 0
├── cover_image_url  VARCHAR(500)  nullable
├── cover_alt        VARCHAR(200)  nullable
├── focus_keyword    VARCHAR(100)  nullable          -- v1.1, editor-only SEO target
├── secondary_keywords VARCHAR(100)[] NOT NULL DEFAULT '{}', CHECK cardinality <= 4  -- v1.1
├── status           VARCHAR(20)   NOT NULL DEFAULT 'draft', CHECK IN ('draft', 'published')
├── published_at     TIMESTAMPTZ   nullable          -- set on FIRST publish, kept after unpublish
├── author_id        UUID          nullable → users(id) ON DELETE SET NULL
├── updated_by       UUID          nullable → users(id) ON DELETE SET NULL
├── version          INTEGER       NOT NULL DEFAULT 1 -- optimistic concurrency
├── created_at       TIMESTAMPTZ   NOT NULL DEFAULT NOW()
└── updated_at       TIMESTAMPTZ   NOT NULL DEFAULT NOW()

UNIQUE (locale, slug)
CHECK (status <> 'published' OR published_at IS NOT NULL)
INDEX (status, published_at DESC)
```

### New Table: `blog_media`

```
blog_media
├── id            UUID          PK
├── key           VARCHAR(255)  NOT NULL UNIQUE   -- blog/<uuid>.<ext> in R2_PUBLIC_BUCKET
├── content_type  VARCHAR(50)   NOT NULL
├── size_bytes    INTEGER       NOT NULL, CHECK > 0
├── uploaded_by   UUID          nullable → users(id) ON DELETE SET NULL
├── created_at    TIMESTAMPTZ   NOT NULL DEFAULT NOW()
└── confirmed_at  TIMESTAMPTZ   nullable          -- set once the object is verified in storage
```

### Configuration

- `R2_PUBLIC_BUCKET`: bucket served publicly (e.g. `fundlok-public`).
- `R2_PUBLIC_BASE_URL`: public origin of that bucket (e.g. `https://media.fundlok.com`).
  Image `src` values must start with `<R2_PUBLIC_BASE_URL>/blog/`.

### Alembic Migration

`alembic/versions/749861cba438_blog_posts.py`; v1.1 keywords: `alembic/versions/ebeed4fd366b_blog_focus_keywords.py`

---

## 4. API Contract

Errors that the UI must translate carry an `X-Error-Code` header.

### Shapes

```
BlogPostSummary {
  id: uuid, slug: string, title: string, excerpt: string,
  status: "draft" | "published",
  cover_image_url: string|null, cover_alt: string|null,
  published_at: datetime|null, updated_at: datetime, created_at: datetime,
  reading_minutes: int
}
BlogPostDetail = BlogPostSummary + {
  locale: "vi"|"en", seo_title: string|null, content: TipTapDoc,
  version: int, author_id: uuid|null, updated_by: uuid|null,
  focus_keyword: string|null, secondary_keywords: string[]   // v1.1, admin only
}
PublicBlogPostSummary {
  slug, title, excerpt, cover_image_url, cover_alt, published_at, updated_at, reading_minutes
}
PublicBlogPost = PublicBlogPostSummary + { locale, seo_title, content }
```

### Admin (roles `ADMIN` | `SYSTEM_ADMIN`)

| Method + path | Body | Response |
|---|---|---|
| `GET /admin/blog/posts?status=&search=&page=&page_size=` | — | `{ total, page, page_size, items: BlogPostSummary[] }` |
| `POST /admin/blog/posts` | `{ title }` | 201 `BlogPostDetail` (draft, slug from title, `-2`, `-3`… if taken) |
| `GET /admin/blog/posts/{post_id}` | — | `BlogPostDetail` |
| `PUT /admin/blog/posts/{post_id}` | `{ title, slug, seo_title, excerpt, content, cover_image_url, cover_alt, focus_keyword?, secondary_keywords?, version }` | `BlogPostDetail` |
| `POST /admin/blog/posts/{post_id}/publish` | `{ version }` | `BlogPostDetail` |
| `POST /admin/blog/posts/{post_id}/unpublish` | `{ version }` | `BlogPostDetail` |
| `DELETE /admin/blog/posts/{post_id}` | — | 204 |
| `POST /admin/blog/media/presign` | `{ content_type, size_bytes }` | 201 `{ media_id, upload_url, public_url }` |
| `POST /admin/blog/media/{media_id}/confirm` | — | `{ media_id, public_url }` |
| `GET /admin/blog/keyword-usage?keyword=&exclude_id=` | — | `{ keyword, posts: [{ id, slug, title, status }] }` (v1.1) |

Admin GETs return no personal data (marketing content), so they are listed in
`NOT_PERSONAL_DATA` instead of writing VIEW audit rows.

### Public (no auth)

| Method + path | Response |
|---|---|
| `GET /api/v1/blog/posts?locale=vi&limit=50` | `{ items: PublicBlogPostSummary[] }`, published only, newest first |
| `GET /api/v1/blog/posts/{locale}/{slug}` | `PublicBlogPost`, or 404 if missing or not published |

### Error Responses

| Status | Code | Condition |
|---|---|---|
| 400 | `BLOG_MEDIA_INVALID` | unsupported image type, too large, or object missing/too large at confirm |
| 403 | — | caller is not ADMIN / SYSTEM_ADMIN |
| 404 | — | post or media not found |
| 409 | `BLOG_VERSION_CONFLICT` | `version` in the body is not the post's current version |
| 409 | `BLOG_SLUG_TAKEN` | another post in the same locale has the slug |
| 409 | `BLOG_DELETE_PUBLISHED` | delete requested while the post is published |
| 422 | `BLOG_SLUG_LOCKED` | slug change on a post that has been published |
| 422 | `BLOG_CONTENT_INVALID` | content or cover image fails §6 validation |
| 422 | `BLOG_PUBLISH_INCOMPLETE` | publish without title, excerpt or body text |
| 503 | `BLOG_MEDIA_NOT_CONFIGURED` | `R2_PUBLIC_BUCKET` / `R2_PUBLIC_BASE_URL` unset |

---

## 5. State Machine

```
draft  ──[publish, admin]────►  published
published ──[unpublish, admin]──►  draft
draft  ──[delete, admin]──────►  (row removed)
```

- Publish on a published post and unpublish on a draft are no-ops (no version bump, no audit row).
- Every state change and every save increments `version`.

---

## 6. Business Rules

1. Slug: `^[a-z0-9]+(-[a-z0-9]+)*$`, at most 80 characters, no dots (the front-end proxy skips paths containing `.`). Generated from the title by removing Vietnamese diacritics (`đ` → `d`).
2. Once `published_at` is set, the slug cannot change (live links must not break), even after unpublishing.
3. `published_at` is set on the first publish only; republishing keeps the original date.
4. Content allowlist:
   - nodes `doc`, `paragraph`, `heading` (level 2 or 3), `text`, `bulletList`, `orderedList`, `listItem`, `blockquote`, `horizontalRule`, `hardBreak`, `image`.
   - marks `bold`, `italic`, `underline`, `strike`, `link`.
   - Any other node or mark type is rejected. Unknown attributes on allowed types are dropped.
5. Link `href`: `http`, `https`, `mailto` or `tel` scheme, or a path starting with `/` (not `//`) or `#`. Any whitespace or control character in the href is rejected.
6. Image `src` and `cover_image_url` must start with `<R2_PUBLIC_BASE_URL>/blog/`; `alt` (and `cover_alt` when a cover is set) is required, at most 200 characters.
7. Content limits: nesting depth ≤ 20, serialized size ≤ 512 KB.
8. Publish requires a non-empty title, excerpt and body text.
9. A published post cannot be deleted; unpublish it first.
10. Images: `image/jpeg`, `image/png`, `image/webp`, at most 5 MB; the presigned PUT is bound to the declared type and exact size; confirm deletes an object larger than the limit.
11. Every write is audited (`entity_type` `BLOG_POST` / `BLOG_MEDIA`) with metadata only, not the post body.
12. Keywords (v1.1) are editor-only: they drive the admin SEO checklist and never appear in the public API or the page (search engines ignore `<meta name="keywords">`).
13. Keywords are stored NFC, trimmed, with single spaces, keeping the admin's casing. Secondary keywords drop blanks, case-insensitive repeats and the focus keyword; at most 4 remain, each at most 100 characters (422 otherwise).
14. `keyword-usage` lists other posts whose focus keyword matches case-insensitively (Python `casefold`, so Vietnamese folds correctly). It only warns: two posts may share a focus keyword.

---

## 7. Error Cases

| Scenario | System behaviour | Response to caller |
|---|---|---|
| Two admins edit the same post; the second saves with an old version | Row locked (`FOR UPDATE`), version compared | 409 `BLOG_VERSION_CONFLICT` |
| Two creates race for the same slug | Unique constraint catches the loser | 409 `BLOG_SLUG_TAKEN` |
| Pasted content contains a `javascript:` link | Validation rejects | 422 `BLOG_CONTENT_INVALID` naming the node |
| Image hotlinked from another site | Validation rejects | 422 `BLOG_CONTENT_INVALID` |
| Upload larger than declared | Storage refuses the PUT (signed length) | browser upload fails |
| Public request for a draft slug | Not found | 404 |

---

## 8. Acceptance Criteria

- [ ] `test_valid_document_passes_and_yields_plain_text`
- [ ] `test_rejects_unknown_node_and_mark_types`
- [ ] `test_rejects_h1_and_h4`
- [ ] `test_rejects_unsafe_link_hrefs`
- [ ] `test_rejects_foreign_or_data_image_src_and_missing_alt`
- [ ] `test_drops_unknown_attributes`
- [ ] `test_slugify_strips_vietnamese_diacritics`
- [ ] `test_admin_creates_draft_with_slug_from_title`
- [ ] `test_duplicate_title_gets_suffixed_slug`
- [ ] `test_non_admin_is_refused`
- [ ] `test_save_bumps_version_and_rejects_stale_version`
- [ ] `test_save_rejects_taken_slug`
- [ ] `test_save_rejects_invalid_content`
- [ ] `test_publish_requires_title_excerpt_and_body`
- [ ] `test_publish_sets_published_at_and_locks_slug`
- [ ] `test_unpublish_keeps_published_at`
- [ ] `test_published_post_cannot_be_deleted`
- [ ] `test_writes_are_audited`
- [ ] `test_list_filters_by_status_and_search`
- [ ] `test_public_api_hides_drafts`
- [ ] `test_media_presign_validates_type_and_size`
- [ ] `test_media_confirm_checks_storage`
- [ ] `test_keywords_are_saved_trimmed_and_deduplicated` (v1.1)
- [ ] `test_rejects_more_than_four_secondary_keywords_or_long_ones` (v1.1)
- [ ] `test_keyword_usage_finds_other_posts_case_insensitively` (v1.1)
- [ ] `test_keywords_never_reach_the_public_api` (v1.1)

---

## 9. Open Questions

| # | Question | Owner | Resolution |
|---|---|---|---|
| 1 | Who creates the `fundlok-public` R2 bucket, the `media.fundlok.com` domain and its CORS rule? | Phat / Edward | — |

---

## Changelog

| Version | Date | Author | Changes |
|---|---|---|---|
| 1.0 | 2026-11-21 | Phat | Initial draft |
| 1.1 | 2026-11-23 | Phat | Focus and secondary keywords for the admin SEO checklist; `keyword-usage` endpoint |
