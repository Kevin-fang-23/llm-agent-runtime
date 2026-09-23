"""H11：events (task_id, seq) 从普通复合索引升级为唯一索引。

背景：seq 曾是引擎进程级计数器，崩溃恢复换引擎后从 1 重新编号 —— 同一任务
出现撞号事件，after_seq 增量订阅在恢复后永久漏事件。引擎已改为每任务续号，
本迁移把约束落成事实：撞号写入直接失败暴露 bug，而不是静默产出错乱时间线。

升级前必须先清理历史撞号副本（旧缺陷的既成事实）：同一 (task_id, seq) 保留
id 最小的一行 —— 撞号事件的时间线本就已不可信，删多余副本是让唯一索引
建起来的先决条件，不是可选优化。

Revision ID: 0002
Revises: 0001
Create Date: 2026-09-22

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '0002'
down_revision: Union[str, None] = '0001'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


_DEDUPE_SQL = (
    "DELETE FROM events WHERE id IN ("
    "SELECT e.id FROM events e JOIN ("
    "SELECT task_id, seq, MIN(id) AS keep_id "
    "FROM events GROUP BY task_id, seq HAVING COUNT(*) > 1) d "
    "ON e.task_id = d.task_id AND e.seq = d.seq AND e.id > d.keep_id)"
)


def upgrade() -> None:
    bind = op.get_bind()
    insp = sa.inspect(bind)
    if "events" in insp.get_table_names():
        op.execute(_DEDUPE_SQL)
        existing = {ix["name"] for ix in insp.get_indexes("events")}
        if "ix_events_task_seq" in existing:
            with op.batch_alter_table('events') as batch_op:
                batch_op.drop_index('ix_events_task_seq')
        if "uq_events_task_seq" not in existing:
            with op.batch_alter_table('events') as batch_op:
                batch_op.create_index('uq_events_task_seq', ['task_id', 'seq'], unique=True)


def downgrade() -> None:
    bind = op.get_bind()
    insp = sa.inspect(bind)
    if "events" in insp.get_table_names():
        existing = {ix["name"] for ix in insp.get_indexes("events")}
        if "uq_events_task_seq" in existing:
            with op.batch_alter_table('events') as batch_op:
                batch_op.drop_index('uq_events_task_seq')
        if "ix_events_task_seq" not in existing:
            with op.batch_alter_table('events') as batch_op:
                batch_op.create_index('ix_events_task_seq', ['task_id', 'seq'], unique=False)
