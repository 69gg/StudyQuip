"""Question section projections and independent vector spaces."""

from alembic import op

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    from studyquip.question_index import initialize_question_indexes

    initialize_question_indexes(op.get_bind())


def downgrade() -> None:
    raise RuntimeError("不支持破坏性自动降级；请恢复升级前的完整备份")
