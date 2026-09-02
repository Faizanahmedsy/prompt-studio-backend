"""public read-only link for a project

Every existing row comes out of this private: `public_token` is NULL, and NULL
is what the read path treats as "no public link". Nothing becomes visible
because of the migration itself — a project only goes public when an owner
asks for a token.

Revision ID: b1c7d2e94f30
Revises: 07f9f4041389
Create Date: 2026-08-27 11:05:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "b1c7d2e94f30"
down_revision: str | None = "07f9f4041389"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("projects", sa.Column("public_token", sa.String(length=64), nullable=True))
    op.add_column(
        "projects", sa.Column("public_enabled_at", sa.DateTime(timezone=True), nullable=True)
    )
    op.add_column("projects", sa.Column("public_enabled_by_id", sa.Uuid(), nullable=True))
    # Unique so a mint collision fails loudly rather than handing two projects
    # the same link; indexed because the anonymous read path looks up by it.
    op.create_index(op.f("ix_projects_public_token"), "projects", ["public_token"], unique=True)
    op.create_foreign_key(
        "fk_projects_public_enabled_by_id_users",
        "projects",
        "users",
        ["public_enabled_by_id"],
        ["id"],
        ondelete="SET NULL",
    )


def downgrade() -> None:
    op.drop_constraint("fk_projects_public_enabled_by_id_users", "projects", type_="foreignkey")
    op.drop_index(op.f("ix_projects_public_token"), table_name="projects")
    op.drop_column("projects", "public_enabled_by_id")
    op.drop_column("projects", "public_enabled_at")
    op.drop_column("projects", "public_token")
