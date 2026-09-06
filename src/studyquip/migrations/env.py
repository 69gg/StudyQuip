"""迁移仅由持有 schema 独占锁的 CLI 执行。"""

from alembic import context

from studyquip.db import metadata

connection = context.config.attributes["connection"]
context.configure(connection=connection, target_metadata=metadata)
with context.begin_transaction():
    context.run_migrations()
