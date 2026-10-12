"""official_delegated_plan_execution

Revision ID: f4a17d5e8b3c
Revises: e3906f5a7c4b
Create Date: 2026-10-06 19:30:00.000000

"""
from typing import Sequence, Union
from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect


# revision identifiers, used by Alembic.
revision: str = 'f4a17d5e8b3c'
down_revision: Union[str, Sequence[str], None] = 'e3906f5a7c4b'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    bind = op.get_bind()
    insp = inspect(bind)

    # 1. Update provider_connections table
    conn_cols = [c['name'] for c in insp.get_columns('provider_connections')]
    
    if 'delegated_provider' not in conn_cols:
        op.add_column('provider_connections', sa.Column('delegated_provider', sa.String(length=50), nullable=True))

    if 'delegated_plan_name' not in conn_cols:
        op.add_column('provider_connections', sa.Column('delegated_plan_name', sa.String(length=100), nullable=True))

    if 'delegated_authorization_status' not in conn_cols:
        op.add_column('provider_connections', sa.Column('delegated_authorization_status', sa.String(length=50), server_default='NOT_CONNECTED', nullable=False))

    if 'delegated_execution_status' not in conn_cols:
        op.add_column('provider_connections', sa.Column('delegated_execution_status', sa.String(length=50), server_default='NOT_SUPPORTED', nullable=False))

    if 'marketplace_commitment' not in conn_cols:
        op.add_column('provider_connections', sa.Column('marketplace_commitment', sa.Float(), server_default='0.0', nullable=False))

    if 'committed_units' not in conn_cols:
        op.add_column('provider_connections', sa.Column('committed_units', sa.Integer(), server_default='0', nullable=False))

    if 'reserved_units' not in conn_cols:
        op.add_column('provider_connections', sa.Column('reserved_units', sa.Integer(), server_default='0', nullable=False))

    if 'consumed_units' not in conn_cols:
        op.add_column('provider_connections', sa.Column('consumed_units', sa.Integer(), server_default='0', nullable=False))

    if 'remaining_units' not in conn_cols:
        op.add_column('provider_connections', sa.Column('remaining_units', sa.Integer(), server_default='0', nullable=False))

    if 'upstream_usage_unit' not in conn_cols:
        op.add_column('provider_connections', sa.Column('upstream_usage_unit', sa.String(length=50), server_default='REQUEST', nullable=False))

    if 'upstream_limit' not in conn_cols:
        op.add_column('provider_connections', sa.Column('upstream_limit', sa.Integer(), nullable=True))

    if 'upstream_reset_at' not in conn_cols:
        op.add_column('provider_connections', sa.Column('upstream_reset_at', sa.DateTime(), nullable=True))

    # 2. Update ai_request_records table
    req_cols = [c['name'] for c in insp.get_columns('ai_request_records')]

    if 'usage_amount' not in req_cols:
        op.add_column('ai_request_records', sa.Column('usage_amount', sa.Float(), server_default='0.0', nullable=False))

    if 'usage_unit' not in req_cols:
        op.add_column('ai_request_records', sa.Column('usage_unit', sa.String(length=50), server_default='TOKEN', nullable=False))

    if 'metering_status' not in req_cols:
        op.add_column('ai_request_records', sa.Column('metering_status', sa.String(length=50), server_default='VERIFIED', nullable=False))

    if 'conversation_id' not in req_cols:
        op.add_column('ai_request_records', sa.Column('conversation_id', sa.Integer(), nullable=True))

    if 'provider_request_id' not in req_cols:
        op.add_column('ai_request_records', sa.Column('provider_request_id', sa.String(length=100), nullable=True))


def downgrade() -> None:
    pass
