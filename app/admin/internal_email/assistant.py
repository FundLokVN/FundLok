"""Admin > Email "Write with Claude": a draft subject, heading and body from
the admin's instruction (and what is already in the form).

See docs/specs/admin/email-assistant.md. Nothing is sent or stored: the
draft goes back to the composer, where the admin edits it and uses the
existing Preview and Send. Recipients never reach Claude, and an instruction
carrying an ID, phone or account number, or an outside email address, is
refused before it does. Claude's answer is checked against the composer's
limits and FundLok's copy rules (app/assistants/copy_rules.py); a fact it
did not have comes back as a [placeholder] the composer will not send.
"""
from __future__ import annotations

import html
import re
from typing import Literal

from fastapi import status
from pydantic import BaseModel, Field

from app.admin.internal_email.schemas import EmailAssistantDraft, EmailAssistantRequest
from app.admin.internal_email.service import FUNDLOK_EMAIL_DOMAINS
from app.assistants import claude, copy_rules
from app.core.config import settings
from app.users.models import User

# X-Error-Code prefix: EMAIL_ASSIST_BUSY, _NO_CREDIT, _FAILED, _UNAVAILABLE, _PERSONAL_DATA.
ERROR_PREFIX = "EMAIL_ASSIST"

# EmailContent's limits.
SUBJECT_MAX = 200
HEADING_MAX = 200
BODY_MAX = 10_000

# Nine or more digits in a row: a CCCD (12), a bank account, a phone number
# written without spaces. Amounts are written with separators (100.000.000).
_LONG_NUMBER = re.compile(r"\d{9,}")
_EMAIL_ADDRESS = re.compile(r"[\w.+-]+@([\w-]+(?:\.[\w-]+)+)", re.UNICODE)
# "[hạn chót]", not a markdown link "[text](https://…)".
_PLACEHOLDER = re.compile(r"\[[^\[\]\n]{1,60}\](?!\()")
_NUMBER = re.compile(r"\d+(?:[.,]\d+)*")

TEMPLATE_PURPOSE = {
    "general": "a general message",
    "announcement": "an announcement of news or a change",
    "action_required": "a request for the reader to do something; say clearly what and by when",
}

SYSTEM_PROMPT = f"""\
You draft emails that FundLok staff send from the admin console to \
businesses (SMEs) and investors who use FundLok. FundLok is a Vietnamese \
technology platform that connects small and medium businesses that need \
working capital with investors who fund them. A staff member reviews and \
edits every draft before it is sent.

Write a subject (one line, at most 200 characters, ideally under 70), an \
optional heading (one line, at most 200 characters; empty if the email does \
not need one) and the body as paragraphs and bullet lists. Plain text only: \
no HTML, no markdown, no emoji.

- Follow the staff member's instruction. If they gave you their current \
draft, rework it as they ask and keep what they did not ask to change.
- Write in the language you are asked for, in a warm, clear, professional \
tone. In Vietnamese address the reader as "Quý khách" or "bạn" and keep the \
register polite.
- Open with a greeting line. End with a short sign-off line ("Trân trọng," or \
"Best regards,") and no name: the sender's signature is added below it.
- The email's button and its link are set by staff. If there is a button, \
you may point to it ("the button below"); never write a URL yourself.
- Never invent a fact: no dates, deadlines, amounts, rates, names, \
percentages, phone numbers, addresses or links that the instruction or the \
current draft does not give. Where one is needed, write a short placeholder \
in square brackets in the email's language, e.g. [hạn chót] or [deadline], \
for staff to fill in.
- Do not include personal data about the reader.

{copy_rules.RULES}
If the instruction asks for something these rules forbid, write the email \
without it.\
"""


class Block(BaseModel):
    type: Literal["paragraph", "bullets"]
    text: str = Field(description="The paragraph's text. Empty for a bullet list.")
    items: list[str] = Field(description="The bullet list's items. Empty for a paragraph.")


class ClaudeDraft(BaseModel):
    subject: str = Field(description="One line, at most 200 characters.")
    heading: str = Field(description="One line, at most 200 characters, or empty.")
    body: list[Block] = Field(description="Greeting first, sign-off line last.")


def available_to(user: User) -> bool:
    return claude.available_to(user, settings.EMAIL_ASSISTANT_AUDIENCE)


def require_access(user: User) -> None:
    claude.require_access(user, settings.EMAIL_ASSISTANT_AUDIENCE, ERROR_PREFIX)


# --------------------------------------------------------------------------- #
# What may go to Claude
# --------------------------------------------------------------------------- #


def personal_data_found(text: str) -> bool:
    """A long number or an email address outside FundLok's own domains."""
    if _LONG_NUMBER.search(text):
        return True
    return any(domain.lower() not in FUNDLOK_EMAIL_DOMAINS for domain in _EMAIL_ADDRESS.findall(text))


def _source_text(body: EmailAssistantRequest) -> str:
    current = body.current
    return "\n".join([body.instruction, current.subject, current.heading, current.body, current.button_label])


# --------------------------------------------------------------------------- #
# Checking what came back
# --------------------------------------------------------------------------- #


def _line(value: str) -> str:
    return " ".join(value.split())


def _numbers(text: str) -> dict[str, str]:
    """Each figure keyed by its digits without separators or leading zeros."""
    found: dict[str, str] = {}
    for match in _NUMBER.findall(text):
        key = re.sub(r"\D", "", match).lstrip("0") or "0"
        found.setdefault(key, match)
    return found


def clean_draft(raw: ClaudeDraft, *, source: str) -> EmailAssistantDraft:
    subject = _line(raw.subject)
    heading = _line(raw.heading)
    if not subject or len(subject) > SUBJECT_MAX or len(heading) > HEADING_MAX:
        raise claude.unusable(ERROR_PREFIX, "subject or heading failed the checks")

    html_parts: list[str] = []
    text_parts: list[str] = []
    for block in raw.body:
        if block.type == "bullets":
            items = [_line(item) for item in block.items if _line(item)]
            if items:
                html_parts.append(
                    "<ul>" + "".join(f"<li>{html.escape(item, quote=False)}</li>" for item in items) + "</ul>"
                )
                text_parts.append("\n".join(f"• {item}" for item in items))
        elif _line(block.text):
            html_parts.append(f"<div>{html.escape(_line(block.text), quote=False)}</div>")
            text_parts.append(_line(block.text))
    # The composer's own blank line between paragraphs (email-presets.ts); a
    # list brings its own margin, so none next to one.
    body_html = ""
    for part in html_parts:
        if body_html and not body_html.endswith("</ul>") and not part.startswith("<ul>"):
            body_html += "<div><br></div>"
        body_html += part
    body_text = "\n\n".join(text_parts)
    if not body_text or len(body_html) > BODY_MAX:
        raise claude.unusable(ERROR_PREFIX, "body empty or too long")

    written = "\n".join([subject, heading, body_text])
    if copy_rules.breaks_rules(written):
        raise claude.unusable(ERROR_PREFIX, "draft breaks the copy rules")
    if personal_data_found(written):
        raise claude.unusable(ERROR_PREFIX, "draft carries a long number or an outside email address")

    placeholders = list(dict.fromkeys(_PLACEHOLDER.findall(written)))
    given = _numbers(source)
    # Step numbers and the like (single digits) are not worth a warning.
    unverified = [shown for key, shown in _numbers(written).items() if key not in given and len(key) > 1]

    return EmailAssistantDraft(
        subject=subject,
        heading=heading,
        body_html=body_html,
        body_text=body_text,
        placeholders=placeholders,
        unverified_numbers=unverified,
    )


# --------------------------------------------------------------------------- #
# The request
# --------------------------------------------------------------------------- #


def _user_message(body: EmailAssistantRequest) -> str:
    current = body.current
    parts = [
        f"Write the email in {copy_rules.LANGUAGE_NAMES[body.language]}.",
        f"It is {TEMPLATE_PURPOSE[body.template]}.",
        f"<instruction>\n{body.instruction}\n</instruction>",
    ]
    if any(value.strip() for value in (current.subject, current.heading, current.body)):
        parts.append(
            "<current_draft>\n"
            f"Subject: {current.subject}\nHeading: {current.heading}\n\n{current.body}\n"
            "</current_draft>"
        )
    if current.button_label.strip():
        parts.append(f'The email has a button labelled "{current.button_label.strip()}".')
    return "\n\n".join(parts)


async def draft(body: EmailAssistantRequest) -> EmailAssistantDraft:
    source = _source_text(body)
    if personal_data_found(source):
        raise claude.error(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            ERROR_PREFIX,
            "PERSONAL_DATA",
            "Take out ID, phone and account numbers and outside email addresses; use a [placeholder] instead.",
        )
    raw = await claude.ask(
        prefix=ERROR_PREFIX,
        system=SYSTEM_PROMPT,
        user_message=_user_message(body),
        output_format=ClaudeDraft,
    )
    return clean_draft(raw, source=source)
