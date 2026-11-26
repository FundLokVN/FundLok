"""The editor's "Suggest SEO" button: Claude reads the draft on screen and
proposes search titles, a meta description and keywords.

See docs/specs/blog/seo-assistant.md. Nothing is stored: the suggestions go
back to the editor, and the admin applies, edits and saves them like any
other change. Claude's answer is constrained to a JSON schema, then checked
here against the same limits as BlogPostUpdate, the keyword rules in
validation.py and the handbook's never-say list for financial claims, so the
editor only ever shows suggestions it could save.
"""
from __future__ import annotations

import asyncio
import logging
import time
from functools import lru_cache

import anthropic
from fastapi import HTTPException, status
from pydantic import BaseModel, Field

from app.blog.models import KEYWORD_MAX_LENGTH, MAX_SECONDARY_KEYWORDS
from app.blog.schemas import BlogSeoSuggestions, Locale, SeoTitleSuggestion
from app.blog.service import blog_error
from app.blog.validation import keyword_key, normalize_keyword
from app.core.config import settings
from app.users.models import Role, User

logger = logging.getLogger(__name__)

MODEL = "claude-opus-5-5"
# Short copy from a supplied article: low effort keeps the wait to seconds.
EFFORT = "low"
MAX_TOKENS = 8_000
# The browser reaches the API through the Next.js /api rewrite, which gives
# up after 30 s; answer (or fail) before it does.
DEADLINE_SECONDS = 25.0
# On a refusal the API re-runs the request on a model its policy routes to.
FALLBACK_BETA = "server-side-fallback-2026-07-01"

# Same limits as BlogPostUpdate (seo_title, excerpt) and the keyword columns.
SEO_TITLE_MAX = 70
DESCRIPTION_MAX = 300
INTENT_MAX = 300
TITLE_OPTIONS = 3

LANGUAGE_NAMES: dict[str, str] = {"vi": "Vietnamese", "en": "English"}

# Handbook v3 §4-§5 (fundlok-domain skill): a suggestion containing any of
# these is dropped, whatever the article says. Kept to phrases that are a
# claim on their own; "đảm bảo" alone is not listed because "tài sản đảm bảo"
# (collateral) is ordinary lending vocabulary.
FORBIDDEN_PHRASES: tuple[str, ...] = (
    "guaranteed",
    "risk-free",
    "risk free",
    "no risk",
    "zero risk",
    "protected capital",
    "capital protection",
    "principal protection",
    "credit scoring",
    "credit rating",
    "đảm bảo lợi nhuận",
    "lợi nhuận đảm bảo",
    "cam kết lợi nhuận",
    "lợi nhuận cam kết",
    "không rủi ro",
    "không có rủi ro",
    "phi rủi ro",
    "bảo toàn vốn",
    "chấm điểm",
    "xếp hạng tín dụng",
)

SYSTEM_PROMPT = """\
You write search-engine copy for articles on the FundLok blog. FundLok is a \
Vietnamese technology platform that connects small and medium businesses that \
need working capital with investors who fund them. An editor reviews every \
suggestion before anything is published.

From the article you are given, write:
- Three SEO titles, each aimed at a different search intent (for example: \
someone learning how something works, someone comparing options, someone \
ready to act). Aim for about 60 characters; never more than 70. For each, \
explain in one short sentence which intent it targets.
- One meta description that accurately summarises the article. Aim for about \
160 characters; never more than 300.
- One focus keyword: the phrase a searcher would type that this article \
answers best. It must appear word for word in the article's title or body.
- Up to four related terms: other phrases searchers use for the same topic.

Write every field in the language the article body is written in, using the \
article's own vocabulary. If the article mixes languages, use the language \
the post is filed under.

Stay grounded in the article. Say only what the article says: add no facts, \
figures, rates, dates, names, promises or claims it does not contain. If the \
draft is short or unfinished, work from what is there.

FundLok's rules for financial claims apply to every field:
- Never promise or imply a return. No "guaranteed", "risk-free", "no risk", \
"safe returns", "fixed profit"; in Vietnamese no "lợi nhuận đảm bảo", "cam \
kết lợi nhuận", "không rủi ro". Returns are targets or ranges, never certain, \
and the investor bears the loss if a business underperforms.
- Never imply that capital is protected, that there is a reserve or \
insurance-like safety net, or that repayment is certain ("bảo toàn vốn").
- FundLok is not a bank, not a lender and not a credit rating agency. Never \
describe FundLok as scoring or rating credit: no "credit scoring", "credit \
rating", "chấm điểm", "xếp hạng tín dụng". Its 0-100 business score is a \
reference for the investor's own decision ("điểm doanh nghiệp và lãi suất \
tham khảo").
- FundLok never holds customer money: investors transfer straight to the \
business's own bank account, and the business repays each investor directly. \
Never mention deposits, withdrawals, wallets, balances, escrow or custodial \
accounts at FundLok.
- Never say approval is certain or instant, that no documents are needed, or \
that everyone qualifies.
- Early repayment means no prepayment penalty; never call it a discount or \
say that paying early costs less.
- Do not mention FundLok's licences or the regulations it operates under.
- Be specific, modest and factual. No hype such as "get rich", "instant \
money", "best" or "number one".
If a claim in the article would break one of these rules, leave it out of \
your suggestions.\
"""


# --------------------------------------------------------------------------- #
# What Claude must answer with (structured output)
# --------------------------------------------------------------------------- #


class TitleOption(BaseModel):
    title: str = Field(description="SEO title, about 60 characters, at most 70.")
    intent: str = Field(description="One short sentence: the search intent this title targets.")


class ClaudeSuggestions(BaseModel):
    titles: list[TitleOption] = Field(description="Exactly three options, each for a different search intent.")
    meta_description: str = Field(description="About 160 characters, at most 300.")
    focus_keyword: str = Field(description="Appears word for word in the article title or body.")
    related_terms: list[str] = Field(description="At most four, in the article's language.")


# --------------------------------------------------------------------------- #
# Who sees the button
# --------------------------------------------------------------------------- #


def available_to(user: User) -> bool:
    if not settings.ANTHROPIC_API_KEY:
        return False
    if settings.BLOG_SEO_ASSISTANT_AUDIENCE == "all_admins":
        return user.role in (Role.ADMIN.value, Role.SYSTEM_ADMIN.value)
    return user.role == Role.SYSTEM_ADMIN.value


def require_access(user: User) -> None:
    if not settings.ANTHROPIC_API_KEY:
        raise blog_error(
            status.HTTP_503_SERVICE_UNAVAILABLE, "BLOG_SEO_UNAVAILABLE", "SEO suggestions are not switched on."
        )
    if not available_to(user):
        raise blog_error(
            status.HTTP_403_FORBIDDEN, "BLOG_SEO_UNAVAILABLE", "SEO suggestions are not switched on for you yet."
        )


# --------------------------------------------------------------------------- #
# Checking what came back
# --------------------------------------------------------------------------- #


def _fold(value: str) -> str:
    return normalize_keyword(value).casefold()


def breaks_copy_rules(value: str) -> bool:
    folded = _fold(value)
    return any(_fold(phrase) in folded for phrase in FORBIDDEN_PHRASES)


def _unusable(reason: str) -> HTTPException:
    logger.warning("blog SEO suggestions unusable: %s", reason)
    return blog_error(
        status.HTTP_502_BAD_GATEWAY, "BLOG_SEO_FAILED", "The suggestions could not be used. Try again."
    )


def clean_suggestions(raw: ClaudeSuggestions, *, title: str, text: str) -> BlogSeoSuggestions:
    """Keep only what the editor could save and the handbook allows.

    A bad title option or related term is dropped on its own; without a
    usable meta description, focus keyword or at least one title there is
    nothing worth showing, and the admin is asked to try again.
    """
    titles: list[SeoTitleSuggestion] = []
    seen_titles: set[str] = set()
    for option in raw.titles:
        candidate = normalize_keyword(option.title)
        intent = normalize_keyword(option.intent)
        key = candidate.casefold()
        if (
            not candidate
            or len(candidate) > SEO_TITLE_MAX
            or not intent
            or len(intent) > INTENT_MAX
            or key in seen_titles
            or breaks_copy_rules(candidate)
        ):
            continue
        seen_titles.add(key)
        titles.append(SeoTitleSuggestion(title=candidate, intent=intent))
        if len(titles) == TITLE_OPTIONS:
            break
    if not titles:
        raise _unusable("no title option passed the checks")

    description = normalize_keyword(raw.meta_description)
    if not description or len(description) > DESCRIPTION_MAX or breaks_copy_rules(description):
        raise _unusable("meta description failed the checks")

    focus = normalize_keyword(raw.focus_keyword)
    # Grounded: the editor's checklist looks for the focus keyword in the post,
    # so one the article never uses could not pass it.
    article = _fold(f"{title}\n{text}")
    if (
        not focus
        or len(focus) > KEYWORD_MAX_LENGTH
        or breaks_copy_rules(focus)
        or keyword_key(focus) not in article
    ):
        raise _unusable("focus keyword failed the checks")

    related: list[str] = []
    seen_terms = {keyword_key(focus)}
    for term in raw.related_terms:
        candidate = normalize_keyword(term)
        if (
            not candidate
            or len(candidate) > KEYWORD_MAX_LENGTH
            or keyword_key(candidate) in seen_terms
            or breaks_copy_rules(candidate)
        ):
            continue
        seen_terms.add(keyword_key(candidate))
        related.append(candidate)
        if len(related) == MAX_SECONDARY_KEYWORDS:
            break

    return BlogSeoSuggestions(
        titles=titles, meta_description=description, focus_keyword=focus, related_terms=related
    )


# --------------------------------------------------------------------------- #
# Calling Claude
# --------------------------------------------------------------------------- #


@lru_cache(maxsize=1)
def _client_for(api_key: str) -> anthropic.AsyncAnthropic:
    """One client per key, so connections are reused between clicks."""
    return anthropic.AsyncAnthropic(
        api_key=api_key,
        timeout=anthropic.Timeout(DEADLINE_SECONDS, connect=5.0),
        max_retries=1,
    )


def _user_message(*, title: str, locale: Locale, text: str) -> str:
    return (
        f"The post is filed under {LANGUAGE_NAMES[locale]}.\n\n"
        f"<article_title>\n{title}\n</article_title>\n\n"
        f"<article_body>\n{text}\n</article_body>"
    )


def _busy() -> HTTPException:
    return blog_error(
        status.HTTP_503_SERVICE_UNAVAILABLE,
        "BLOG_SEO_BUSY",
        "Claude is slow or unavailable right now. Try again in a moment.",
    )


def _no_credit() -> HTTPException:
    return blog_error(
        status.HTTP_503_SERVICE_UNAVAILABLE,
        "BLOG_SEO_NO_CREDIT",
        "SEO suggestions are paused: the Claude workspace is out of credit for this month.",
    )


def _failed() -> HTTPException:
    return blog_error(
        status.HTTP_502_BAD_GATEWAY, "BLOG_SEO_FAILED", "SEO suggestions failed. Try again."
    )


async def _ask_claude(*, title: str, locale: Locale, text: str) -> ClaudeSuggestions:
    client = _client_for(settings.ANTHROPIC_API_KEY or "")
    started = time.monotonic()
    try:
        async with asyncio.timeout(DEADLINE_SECONDS):
            response = await client.beta.messages.parse(
                model=MODEL,
                max_tokens=MAX_TOKENS,
                betas=[FALLBACK_BETA],
                fallbacks="default",
                output_config={"effort": EFFORT},
                system=SYSTEM_PROMPT,
                messages=[{"role": "user", "content": _user_message(title=title, locale=locale, text=text)}],
                output_format=ClaudeSuggestions,
            )
    except TimeoutError:
        logger.warning("blog SEO suggestions: no answer within %.0f s", DEADLINE_SECONDS)
        raise _busy() from None
    except (
        anthropic.EnforcedSpendLimitReachedError,
        anthropic.ContractSpendLimitReachedError,
        anthropic.PaymentRequiredError,
    ) as exc:
        logger.error("blog SEO suggestions: spend limit reached (%s)", exc.status_code)
        raise _no_credit() from None
    except anthropic.RateLimitError:
        raise _busy() from None
    except anthropic.BadRequestError as exc:
        # An exhausted prepaid balance is a plain 400 with no error code of its own.
        if "credit balance" in str(exc.message).lower():
            logger.error("blog SEO suggestions: credit balance too low")
            raise _no_credit() from None
        logger.error("blog SEO suggestions: request refused: %s", exc.message)
        raise _failed() from None
    except (anthropic.AuthenticationError, anthropic.PermissionDeniedError) as exc:
        logger.error("blog SEO suggestions: the API key was refused (%s)", exc.status_code)
        raise _failed() from None
    except anthropic.InternalServerError:  # 5xx, overloaded included
        raise _busy() from None
    except anthropic.APIConnectionError:  # timeouts included
        raise _busy() from None
    except anthropic.APIStatusError as exc:
        logger.error("blog SEO suggestions: unexpected API error %s", exc.status_code)
        raise _failed() from None
    except ValueError as exc:  # the JSON did not match the schema (e.g. cut off)
        raise _unusable(f"unparseable answer: {type(exc).__name__}") from None

    logger.info(
        "blog SEO suggestions: model=%s stop=%s input_tokens=%s output_tokens=%s seconds=%.1f request_id=%s",
        response.model,
        response.stop_reason,
        response.usage.input_tokens,
        response.usage.output_tokens,
        time.monotonic() - started,
        response._request_id,
    )
    if response.stop_reason == "refusal":
        category = response.stop_details.category if response.stop_details else None
        raise _unusable(f"refused (category {category})")
    if response.stop_reason == "max_tokens" or response.parsed_output is None:
        raise _unusable(f"no complete answer (stop {response.stop_reason})")
    return response.parsed_output


async def suggest(*, title: str, locale: Locale, text: str) -> BlogSeoSuggestions:
    raw = await _ask_claude(title=title, locale=locale, text=text)
    return clean_suggestions(raw, title=title, text=text)
