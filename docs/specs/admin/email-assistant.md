# Admin email assistant ("Write with Claude")

Status: DRAFT (26 Nov 2026) · Owner: Phat · Builds on the admin email composer and on `docs/specs/blog/seo-assistant.md` (same key, client and copy rules)

## 1. Context & Goal

Admin → Email → Compose gets a **Write with Claude** box under the presets. The admin says what the email should cover (or how to change what is already in the form), and Claude returns a subject, a heading and a body in the chosen content language (VI or EN), shaped by the chosen template. **Use this draft** fills those three fields; the composer asks first when they already hold text. Recipients, the button and the signature are left alone, and the existing Preview and Send are unchanged: nothing is sent without the admin.

## 2. Out of Scope

- Sending, scheduling or choosing recipients. Recipients never reach Claude.
- Writing the button link, or any link: staff set those.
- Personalising per recipient (mail merge).
- Storing drafts or a history of them.

## 3. Data Model

None. Settings (`app/core/config.py`):

| Setting | Default | Meaning |
|---|---|---|
| `ANTHROPIC_API_KEY` | unset | Shared with the blog SEO assistant. Unset: the box is hidden, the endpoint answers 503. |
| `EMAIL_ASSISTANT_AUDIENCE` | `system_admin` | `system_admin` while drafts are reviewed; `all_admins` for every ADMIN and SYSTEM_ADMIN. |

## 4. API Contract

ADMIN or SYSTEM_ADMIN (403 otherwise). Errors carry `X-Error-Code`.

### `GET /admin/email/assistant`

`{ "enabled": true }`. No personal data, so no VIEW audit row.

### `POST /admin/email/assistant/draft`

```json
{
  "instruction": "string, 1-2000",
  "language": "vi | en",
  "template": "general | announcement | action_required",
  "current": { "subject": "≤ 200", "heading": "≤ 200", "body": "plain text ≤ 10000", "button_label": "≤ 60" }
}
```

`current` is optional (all fields default to ""). Extra fields (e.g. `to`) are refused (422).

Response 200:

```json
{
  "subject": "one line ≤ 200",
  "heading": "one line ≤ 200, may be empty",
  "body_html": "<div>…</div><div><br></div><ul><li>…</li></ul>…  (≤ 10000, every text escaped)",
  "body_text": "the same body as plain text, for display",
  "placeholders": ["[hạn chót]"],
  "unverified_numbers": ["15"]
}
```

## 6. Business Rules

1. **Model and request.** `app/assistants/claude.py`: `claude-opus-5-5`, effort `low`, structured output (`ClaudeDraft`: subject, heading, body as paragraph/bullet blocks), server-side refusal fallbacks, 25 s deadline (the `/api` rewrite gives up at 30 s). The DB connection is released before the call.
2. **Prompt.** Writes as FundLok staff to SMEs and investors; greeting first, sign-off line last with no name (the signature is appended); may refer to the button but never writes a URL; carries `app/assistants/copy_rules.RULES`.
3. **No invented facts.** Dates, deadlines, amounts, rates, names, phone numbers, addresses and links not in the instruction or current draft come back as a bracketed placeholder. The response lists them (`placeholders`); the composer refuses to send while any placeholder from the applied draft is still in the email. Figures of two or more digits in the draft that are not in the instruction or current draft are listed in `unverified_numbers` as a warning.
4. **Personal data stays out.** The instruction and current draft are refused (422 `EMAIL_ASSIST_PERSONAL_DATA`) when they contain nine or more digits in a row (CCCD, bank account, phone) or an email address outside `fundlok.com` / `fundlok.vn`, before anything reaches Claude. Amounts with separators (3.000.000) are fine.
5. **Checks on the draft.** Subject 1-200 and heading ≤ 200 after whitespace is collapsed to one line; at least one non-empty block; `body_html` ≤ 10 000; no phrase from `FORBIDDEN_PHRASES`; no long number or outside address. Any failure: 502 `EMAIL_ASSIST_FAILED`.
6. **Body HTML.** Built on the server from the blocks: each paragraph `<div>escaped text</div>`, each list `<ul><li>escaped</li></ul>`; a blank line (`<div><br></div>`, the presets' spacing) between two paragraphs, none next to a list, which has its own margin. The composer puts it into its editor as is.
7. **Editor.** The request runs in the background; the form stays editable. A failure changes nothing in the form.

## 7. Error Cases

| Status | `X-Error-Code` | When |
|---|---|---|
| 503 / 403 | `EMAIL_ASSIST_UNAVAILABLE` | key unset / admin outside the audience |
| 422 | `EMAIL_ASSIST_PERSONAL_DATA` | long number or outside address in the instruction or current draft |
| 503 | `EMAIL_ASSIST_BUSY` | no answer in 25 s, overloaded, 5xx, 429, network |
| 503 | `EMAIL_ASSIST_NO_CREDIT` | workspace spend limit, contract limit, 402, low credit balance |
| 502 | `EMAIL_ASSIST_FAILED` | key refused, refusal, cut-off or unusable draft (§6.5) |
| 422 | — | blank or over-long instruction, unknown language/template, extra fields |

## 8. Acceptance Criteria

`tests/admin/test_email_assistant.py` and `ComposeEmailCard.test.tsx`:

- off without an API key; starts with SYSTEM_ADMIN only; `all_admins` includes ADMIN; non-admins refused
- sends the instruction, language, template and current draft; recipients are not accepted
- personal data never reaches Claude (CCCD, bank account, phone, outside email; in the instruction or the current draft); FundLok addresses and formatted amounts are fine
- the body is escaped HTML in the composer's paragraph style
- placeholders and figures not given are flagged
- drafts breaking the copy rules or the limits ask to try again
- slow or out-of-credit Claude uses the email error codes
- Use this draft fills subject, heading and body only, asking first over existing text; Send is refused while a placeholder remains; a failure leaves the form unchanged

## 9. Open Questions

- Q1. Switch `EMAIL_ASSISTANT_AUDIENCE` to `all_admins` after a SYSTEM_ADMIN has reviewed drafts for the common cases (onboarding nudges, KYB/KYC reminders, tax-declaration reminders, announcements).
- Q2. Whether drafts should be logged (instruction and output) for review; today only token counts and timing are logged.
