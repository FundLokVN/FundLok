"""blog_posts: focus and secondary keywords for the admin SEO checklist

Revision ID: ebeed4fd366b
Revises: 749861cba438
Create Date: 2026-11-23 10:00:00

Two editor-only columns. They drive the SEO checklist in the admin and are
never rendered into the public page. Existing posts get no focus keyword and
an empty list.

See docs/specs/blog/blog-posts.md (v1.1).
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = "ebeed4fd366b"
down_revision: Union[str, None] = "749861cba438"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("blog_posts", sa.Column("focus_keyword", sa.String(length=100), nullable=True))
    op.add_column(
        "blog_posts",
        sa.Column(
            "secondary_keywords",
            postgresql.ARRAY(sa.String(length=100)),
            nullable=False,
            server_default="{}",
        ),
    )
    op.create_check_constraint(
        "chk_blog_posts_secondary_keywords",
        "blog_posts",
        "cardinality(secondary_keywords) <= 4",
    )


def downgrade() -> None:
    op.drop_constraint("chk_blog_posts_secondary_keywords", "blog_posts", type_="check")
    op.drop_column("blog_posts", "secondary_keywords")
    op.drop_column("blog_posts", "focus_keyword")
