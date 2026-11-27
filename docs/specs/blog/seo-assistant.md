# Blog editor SEO assistant

Status: DRAFT (26 Nov 2026) · Owner: Phat · Builds on `blog-posts.md` v1.1 (SEO checklist, focus and secondary keywords)

## 1. Context & Goal

Admin → Blog → Edit post gets a **Suggest SEO** button beside the SEO checklist and the search preview. Claude reads the draft currently in the editor, unsaved changes included, and suggests:

- three SEO titles, each with one sentence on the search intent it targets;
- one meta description that summarises the article;
- one focus keyword and up to four related terms, in the post's language.

Each suggestion has a **Use this** button that writes that one field into the draft (SEO title, meta description, focus keyword, or one related term added as a secondary keyword). The checklist and the preview recompute at once. Nothing is saved until the admin uses Save or Publish, as for any other edit.

## 2. Out of Scope

- Rewriting the article body, the post title (H1) or the slug.
- Generating on save, on publish or in the background: only on click.
- Storing suggestions or a history of them (no table, no migration).
- Choosing a model per post or exposing the prompt to admins.

## 3. Data Model

None. Suggestions live in the editor's local state until applied.

Settings (`app/core/config.py`):

| Setting | Default | Meaning |
|---|---|---|
| `ANTHROPIC_API_KEY` | unset | Key of a dedicated Claude Console workspace with a monthly spend limit. Cloud Run reads it from Secret Manager. Unset: the button is hidden and the endpoint answers 503. |
| `BLOG_SEO_ASSISTANT_AUDIENCE` | `system_admin` | `system_admin`: only SYSTEM_ADMIN sees the button (review period). `all_admins`: every ADMIN and SYSTEM_ADMIN. |

## 4. API Contract

Both routes need ADMIN or SYSTEM_ADMIN (403 otherwise). Errors carry `X-Error-Code`.

### `GET /admin/blog/seo-assistant`

```json
{ "enabled": true }
```

`true` when the key is set and the admin is in the audience. No personal data, so no VIEW audit row (exempt in `tests/admin/test_admin_read_audit.py`).

### `POST /admin/blog/seo-assistant/suggestions`

Request: the draft as it is on screen.

```json
{ "title": "string, 1-200", "locale": "vi | en", "text": "plain text of the body, 1-100000 chars" }
```

Response 200:

```json
{
  "titles": [{ "title": "≤ 70 chars", "intent": "one sentence" }],
  "meta_description": "≤ 300 chars",
  "focus_keyword": "≤ 100 chars, appears in the title or body",
  "related_terms": ["at most 4, each ≤ 100 chars"]
}
```

`titles` holds 1 to 3 options (an option that fails a check is dropped, §6). Nothing is written and no audit row is added: the post is not read or changed.

## 6. Business Rules

1. **Model and request.** `claude-opus-5-5`, effort `low`, structured output (`output_config.format`, JSON schema from `ClaudeSuggestions` in `app/blog/seo_assistant.py`), server-side refusal fallbacks (`fallbacks: "default"`). The system prompt carries FundLok's copy rules for financial claims (Handbook v3 §4-§5, the `fundlok-domain` skill).
2. **Language.** The prompt names the post's locale; Claude writes in the language the body is written in and falls back to the locale when the body mixes languages.
3. **Grounding.** The prompt forbids facts, figures, rates, dates, names or claims the article does not contain. The focus keyword must appear in the title or body (compared like the checklist: NFC, single spaces, case-insensitive); otherwise the answer is refused.
4. **Checks before display** (same limits as `BlogPostUpdate` and the keyword rules in `validation.py`):
   - title option: whitespace collapsed, 1-70 characters, intent 1-300 characters, no case-insensitive repeat; failing options are dropped, keep at most 3;
   - meta description: 1-300 characters;
   - focus keyword: `normalize_keyword`, 1-100 characters, grounded (rule 3);
   - related terms: `normalize_keyword`, 1-100 characters, no repeat of each other or of the focus keyword (`keyword_key`), keep at most 4;
   - any field containing a phrase from `FORBIDDEN_PHRASES` (promised returns, risk-free, capital protection, rating language, in English and Vietnamese) is dropped.
   No usable title, meta description or focus keyword: 502 `BLOG_SEO_FAILED`.
5. **Targets.** The prompt aims for about 60 characters per title and 160 for the description; the editor's counters already warn past 60 and 160 when a suggestion is applied.
6. **Time.** The call has 25 s (the Next.js `/api` rewrite gives up at 30 s), one SDK retry inside that window. The DB connection used to load the admin is released before the call.
7. **Editor.** The request runs in the background; the admin keeps typing, saving and publishing. A failure changes nothing in the draft. Suggestions stay on screen until applied, dismissed or the page is left; a new click replaces them.

## 7. Error Cases

| Status | `X-Error-Code` | When | Editor shows |
|---|---|---|---|
| 503 | `BLOG_SEO_UNAVAILABLE` | key unset | "not switched on" (button normally hidden) |
| 403 | `BLOG_SEO_UNAVAILABLE` | admin outside the audience | same |
| 503 | `BLOG_SEO_BUSY` | no answer within 25 s, overloaded (529), 5xx, rate limited (429), network error | retry message |
| 503 | `BLOG_SEO_NO_CREDIT` | workspace spend limit (`enforced_spend_limit_reached`), contract limit, 402, prepaid credit balance too low | paused-until-credit message, retry |
| 502 | `BLOG_SEO_FAILED` | key refused (401/403), refusal, cut-off or unparseable answer, nothing passed §6.4 | try-again message |
| 422 | — | blank title or body, body over 100 000 characters, unknown locale | generic error |

## 8. Acceptance Criteria

Covered by `tests/blog/test_seo_assistant.py` (backend) and `BlogPostEditor.test.tsx` / `seo-suggestions.test.ts` (frontend):

- off without an API key; starts with SYSTEM_ADMIN only; `all_admins` includes ADMIN; non-admins refused
- sends the draft title, language and text; an English draft is sent as English
- uses the draft sent and stores nothing
- blank or oversized drafts refused before Claude is called
- over-long, repeated or promising title options dropped; related terms tidied, deduplicated, capped at four
- focus keyword must appear in the article
- unusable meta description, refusal or cut-off answer → try again
- slow, overloaded, rate-limited or unreachable Claude → retry; out of credit → paused
- **Use this** writes only its own field and the checklist and preview update; the editor stays editable while a request runs; a failure leaves the draft unchanged

## 9. Open Questions

- Q1. Rollout: switch `BLOG_SEO_ASSISTANT_AUDIENCE` to `all_admins` after a SYSTEM_ADMIN has reviewed suggestions on a few real posts (VI and EN).
- Q2. Spend limit for the Console workspace (estimate: about USD 0.05 per click at current Opus 5.5 prices for a 1 500-word post).
