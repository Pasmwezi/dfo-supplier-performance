"""serialize evaluation workflow versions

Revision ID: e7a4b9c2d101
Revises: c0d6e1a4b921
Create Date: 2026-07-30 01:22:00
"""
from typing import Sequence, Union

from alembic import op


revision: str = "e7a4b9c2d101"
down_revision: Union[str, Sequence[str], None] = "c0d6e1a4b921"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_unique_constraint(
        "uq_evaluation_versions_evaluation_version",
        "evaluation_versions",
        ["evaluation_id", "version"],
    )
    op.create_index(
        "ix_access_audit_account_throttle",
        "access_audit",
        ["action", "username", "timestamp"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_access_audit_account_throttle", table_name="access_audit")
    op.drop_constraint(
        "uq_evaluation_versions_evaluation_version",
        "evaluation_versions",
        type_="unique",
    )
