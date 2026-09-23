"""A13（迁移溢出项）：为六张表的常量默认列补 server_default。

背景：0001 基线由 autogenerate 生成时，所有默认值都只落在 ORM 侧
（ColumnDefault），DDL 里没有任何 DEFAULT 子句 —— raw SQL 插入 / 外部工具写库
只要省略其中一个 NOT NULL 列就直接失败；`rate_windows.hits` 尤甚（UPSERT 的
写法脆弱依赖列存在与显式赋值）。models 已同步补齐 server_default，本迁移把
既成版本化的库推齐，两侧不再有两套真相。

实现要点：
  - 全部走 batch_alter_table：PostgreSQL 下 batch 直通为
    ``ALTER COLUMN ... SET DEFAULT``（廉价、不重写表）；SQLite 不支持
    SET DEFAULT，batch 会做"建新表-拷数据-换名"的整表重建 —— 本项目规模可接受，
    且反射会保留主键、唯一约束（uq_events_task_seq）与全部索引。
  - 存在性防御照 0002 的风格：表不存在（理论上不该发生）就跳过该表。
  - **旧库引导路径（stamp head）不会经过本迁移**：那是 create_all +
    migrate_schema 的存量通路，其 ALTER ADD COLUMN 本来就自带 DEFAULT，
    语义一致（历史列缺默认与旧行为相同，属有意保留）。

时间列（created_at/updated_at 等）刻意排除：默认值是 Python 墙钟可调用，
SQL 层没有跨方言统一的字面量，仍由 repository 层显式传值 —— 不变量守卫见
tests/test_migrations.py。

Revision ID: 0003
Revises: 0002
Create Date: 2026-09-23

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '0003'
down_revision: Union[str, None] = '0002'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


# 与 app/storage/models.py 的 server_default 一一对应（值改了必须两边同改，
# test_migrations 的 raw SQL 行为用例会红在缺任何一侧的地方）。
_DEFAULTS = {
    "tenants": [
        ("key_prefix", sa.String(16), sa.text("''")),
        ("enabled", sa.Boolean(), sa.true()),
        ("daily_token_quota", sa.Integer(), sa.text("0")),
    ],
    "tasks": [
        ("mode", sa.String(20), sa.text("'react'")),
        ("status", sa.String(20), sa.text("'queued'")),
        ("tenant_id", sa.String(40), sa.text("''")),
        ("require_approval", sa.Boolean(), sa.false()),
        ("max_tokens", sa.Integer(), sa.text("60000")),
        ("max_steps", sa.Integer(), sa.text("24")),
        ("tokens_used", sa.Integer(), sa.text("0")),
        ("steps_used", sa.Integer(), sa.text("0")),
        ("downgraded", sa.Boolean(), sa.false()),
        ("selfheal_count", sa.Integer(), sa.text("0")),
        ("result", sa.Text(), sa.text("''")),
        ("error", sa.Text(), sa.text("''")),
        ("duration_s", sa.Float(), sa.text("0")),
    ],
    "events": [
        ("trace_id", sa.String(32), sa.text("''")),
        ("payload", sa.Text(), sa.text("'{}'")),
    ],
    "tool_executions": [
        ("arguments", sa.Text(), sa.text("'{}'")),
        ("ok", sa.Boolean(), sa.true()),
        ("result", sa.Text(), sa.text("''")),
        ("error", sa.Text(), sa.text("''")),
        ("error_type", sa.String(20), sa.text("''")),
    ],
    "spans": [
        ("parent_span_id", sa.String(16), sa.text("''")),
        ("task_id", sa.String(40), sa.text("''")),
        ("start_ts", sa.Float(), sa.text("0")),
        ("end_ts", sa.Float(), sa.text("0")),
        ("duration_ms", sa.Float(), sa.text("0")),
        ("status", sa.String(16), sa.text("'ok'")),
        ("attributes", sa.Text(), sa.text("'{}'")),
    ],
    "rate_windows": [
        ("hits", sa.Integer(), sa.text("0")),
    ],
}


def upgrade() -> None:
    bind = op.get_bind()
    insp = sa.inspect(bind)
    tables = set(insp.get_table_names())
    for table, cols in _DEFAULTS.items():
        if table not in tables:
            continue
        with op.batch_alter_table(table) as batch_op:
            for name, typ, default in cols:
                batch_op.alter_column(name, existing_type=typ, server_default=default)


def downgrade() -> None:
    bind = op.get_bind()
    insp = sa.inspect(bind)
    tables = set(insp.get_table_names())
    for table, cols in _DEFAULTS.items():
        if table not in tables:
            continue
        with op.batch_alter_table(table) as batch_op:
            for name, typ, _default in cols:
                # server_default=None 在 alembic 语义里就是 DROP DEFAULT
                batch_op.alter_column(name, existing_type=typ, server_default=None)
