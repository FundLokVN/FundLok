"""The editor's "Suggest SEO" button: Claude reads the draft on screen and
proposes search titles, a meta description and keywords.

See docs/specs/blog/seo-assistant.md. Nothing is stored: the suggestions go
back to the editor, and the admin applies, edits and saves them like any
other change. Claude's answer is constrained to a JSON schema
(app/assistants/claude.py), then checked here against the same limits as
BlogPostUpdate, the keyword rules in validation.py and the handbook's
never-say list (app/assistants/copy_rules.py), so the editor only ever shows
suggestions it could save.
"""
from __future__ import annotations

from pydantic import BaseModel, Field

from app.assistants import claude, copy_rules
from app.blog.models import KEYWORD_MAX_LENGTH, MAX_SECONDARY_KEYWORDS
from app.blog.schemas import BlogSeoSuggestions, Locale, SeoTitleSuggestion
from app.blog.validation import keyword_key, normalize_keyword
from app.core.config import settings
from app.users.models import User

# X-Error-Code prefix: BLOG_SEO_BUSY, BLOG_SEO_NO_CREDIT, BLOG_SEO_FAILED, BLOG_SEO_UNAVAILABLE.
ERROR_PREFIX = "BLOG_SEO"

# Same limits as BlogPostUpdate (seo_title, excerpt) and the keyword columns.
SEO_TITLE_MAX = 70
DESCRIPTION_MAX = 300
INTENT_MAX = 300
TITLE_OPTIONS = 3

SYSTEM_PROMPT = f"""\
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

{copy_rules.RULES}
If a claim in the article would break one of these rules, leave it out of \
your suggestions.\
"""


class TitleOption(BaseModel):
    title: str = Field(description="SEO title, about 60 characters, at most 70.")
    intent: str = Field(description="One short sentence: the search intent this title targets.")


class ClaudeSuggestions(BaseModel):
    titles: list[TitleOption] = Field(description="Exactly three options, each for a different search intent.")
    meta_description: str = Field(description="About 160 characters, at most 300.")
    focus_keyword: str = Field(description="Appears word for word in the article title or body.")
    related_terms: list[str] = Field(description="At most four, in the article's language.")


def available_to(user: User) -> bool:
    return claude.available_to(user, settings.BLOG_SEO_ASSISTANT_AUDIENCE)


def require_access(user: User) -> None:
    claude.require_access(user, settings.BLOG_SEO_ASSISTANT_AUDIENCE, ERROR_PREFIX)


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
            or copy_rules.breaks_rules(candidate)
        ):
            continue
        seen_titles.add(key)
        titles.append(SeoTitleSuggestion(title=candidate, intent=intent))
        if len(titles) == TITLE_OPTIONS:
            break
    if not titles:
        raise claude.unusable(ERROR_PREFIX, "no title option passed the checks")

    description = normalize_keyword(raw.meta_description)
    if not description or len(description) > DESCRIPTION_MAX or copy_rules.breaks_rules(description):
        raise claude.unusable(ERROR_PREFIX, "meta description failed the checks")

    focus = normalize_keyword(raw.focus_keyword)
    # Grounded: the editor's checklist looks for the focus keyword in the post,
    # so one the article never uses could not pass it.
    if (
        not focus
        or len(focus) > KEYWORD_MAX_LENGTH
        or copy_rules.breaks_rules(focus)
        or keyword_key(focus) not in copy_rules.fold(f"{title}\n{text}")
    ):
        raise claude.unusable(ERROR_PREFIX, "focus keyword failed the checks")

    related: list[str] = []
    seen_terms = {keyword_key(focus)}
    for term in raw.related_terms:
        candidate = normalize_keyword(term)
        if (
            not candidate
            or len(candidate) > KEYWORD_MAX_LENGTH
            or keyword_key(candidate) in seen_terms
            or copy_rules.breaks_rules(candidate)
        ):
            continue
        seen_terms.add(keyword_key(candidate))
        related.append(candidate)
        if len(related) == MAX_SECONDARY_KEYWORDS:
            break

    return BlogSeoSuggestions(
        titles=titles, meta_description=description, focus_keyword=focus, related_terms=related
    )


def _user_message(*, title: str, locale: Locale, text: str) -> str:
    return (
        f"The post is filed under {copy_rules.LANGUAGE_NAMES[locale]}.\n\n"
        f"<article_title>\n{title}\n</article_title>\n\n"
        f"<article_body>\n{text}\n</article_body>"
    )


async def suggest(*, title: str, locale: Locale, text: str) -> BlogSeoSuggestions:
    raw = await claude.ask(
        prefix=ERROR_PREFIX,
        system=SYSTEM_PROMPT,
        user_message=_user_message(title=title, locale=locale, text=text),
        output_format=ClaudeSuggestions,
    )
    return clean_suggestions(raw, title=title, text=text)
