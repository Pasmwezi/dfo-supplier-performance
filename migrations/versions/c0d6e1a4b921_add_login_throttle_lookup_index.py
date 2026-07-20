"""add login throttle lookup index

Revision ID: c0d6e1a4b921
Revises: 9140020dff13
Create Date: 2026-07-20 00:20:00
"""
from typing import Sequence, Union

from alembic import op


revision: str = "c0d6e1a4b921"
down_revision: Union[str, Sequence[str], None] = "9140020dff13"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_index(
        "ix_access_audit_login_throttle",
        "access_audit",
        ["action", "actor", "timestamp"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_access_audit_login_throttle", table_name="access_audit")
