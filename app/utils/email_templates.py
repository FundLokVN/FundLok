"""Fill in the exported React Email templates.

The templates are written in React Email (`emails/src/templates/*.tsx`) and
exported to static HTML in `app/email_templates/` (`npm run export` in
`emails/`). The export contains Jinja placeholders where the values go, so the
API renders email without Node.

ESCAPING
Every value is HTML-escaped with `html.escape` semantics, exactly what the
hand-written f-string templates did before (`&quot;` for a double quote, where
Jinja's own escaper would write `&#34;`). It is done in `finalize`, which Jinja
applies to every `{{ … }}`, so a template cannot forget to escape. A value that
is deliberately markup must arrive as `Markup`; the only one is the output of
the `nl2br` filter, which escapes each line before joining them with `<br/>`.

StrictUndefined: a variable the caller forgot is an error, not an empty
string. A password reset with a blank link must fail in a test, not reach an
inbox.
"""

import html
from datetime import datetime
from functools import lru_cache
from pathlib import Path
from typing import Any

from jinja2 import Environment, FileSystemLoader, StrictUndefined
from markupsafe import Markup

TEMPLATES_DIR = Path(__file__).resolve().parents[1] / "email_templates"

# Referenced from the HTML as <img src="cid:fundlok-logo">; send_email attaches
# the image under this Content-ID.
LOGO_CID = "fundlok-logo"


def _escape(value: Any) -> Markup:
    if isinstance(value, Markup):
        return value
    return Markup(html.escape(str(value), quote=True))


def _nl2br(value: Any) -> Markup:
    """Plain text -> escaped HTML, one <br/> per newline.
    If value is already Markup, preserve safe markup without re-escaping."""
    text = str(value).replace("\r\n", "\n").replace("\r", "\n")
    if isinstance(value, Markup):
        return Markup("<br/>").join(Markup(line) for line in text.split("\n"))
    return Markup("<br/>").join(_escape(line) for line in text.split("\n"))


@lru_cache(maxsize=1)
def _environment() -> Environment:
    env = Environment(
        loader=FileSystemLoader(TEMPLATES_DIR),
        autoescape=True,
        finalize=_escape,
        undefined=StrictUndefined,
        auto_reload=False,
    )
    env.filters["nl2br"] = _nl2br
    return env


def render_email(template: str, /, **context: Any) -> str:
    """The HTML for `template` (a file in app/email_templates/, without
    `.html`), filled in with `context`. Positional-only, so a template can
    have a variable called `name`.

    `logo_src` defaults to the attached CID logo; the admin console's preview
    passes a data: URI instead, because a browser has no message to resolve a
    CID against. `year` defaults to the current year for the © line.
    """
    context.setdefault("logo_src", f"cid:{LOGO_CID}")
    context.setdefault("year", datetime.now().year)
    return _environment().get_template(f"{template}.html").render(**context)


def template_names() -> list[str]:
    """Every exported template, for the tests that render them all."""
    return sorted(path.stem for path in TEMPLATES_DIR.glob("*.html"))
