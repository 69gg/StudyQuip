"""初始版本化数据与任务表。"""

import sqlalchemy as sa
from alembic import op

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "records",
        sa.Column("kind", sa.String(), primary_key=True),
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("data", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.Float(), nullable=False),
        sa.Column("updated_at", sa.Float(), nullable=False),
    )
    op.create_table(
        "record_history",
        sa.Column("kind", sa.String(), primary_key=True),
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("revision", sa.Integer(), primary_key=True),
        sa.Column("snapshot", sa.JSON(), nullable=False),
    )
    op.create_table(
        "jobs",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("kind", sa.String(), nullable=False),
        sa.Column("resource_id", sa.String(), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("checkpoint", sa.JSON(), nullable=False),
        sa.Column("status", sa.String(), nullable=False),
        sa.Column("not_before", sa.Float(), nullable=False),
        sa.Column("bypass_window", sa.Integer(), nullable=False),
        sa.Column("owner", sa.String()),
        sa.Column("lease_token", sa.String()),
        sa.Column("lease_until", sa.Float()),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("error", sa.String()),
        sa.Column("result", sa.JSON()),
        sa.Column("created_at", sa.Float(), nullable=False),
        sa.Column("updated_at", sa.Float(), nullable=False),
    )
    op.create_index("ix_jobs_status", "jobs", ["status"])
    from studyquip.retrieval import initialize_indexes

    initialize_indexes(op.get_bind())


def downgrade() -> None:
    raise RuntimeError("不支持破坏性自动降级；请恢复升级前的完整备份")
