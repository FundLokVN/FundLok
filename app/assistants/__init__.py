"""Claude-backed writing helpers for the admin console (blog SEO, email drafts).

`claude` holds the one client, the request defaults and how API failures map
to the console's error codes; `copy_rules` holds FundLok's rules for
financial claims, which every assistant's prompt carries and its answers are
checked against. See docs/specs/blog/seo-assistant.md and
docs/specs/admin/email-assistant.md.
"""
