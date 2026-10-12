"""consumer_chat_history_and_capacity

Revision ID: e3906f5a7c4b
Revises: d2895e4f6b3a
Create Date: 2026-10-06 18:00:00.000000

"""
from typing import Sequence, Union
from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect


# revision identifiers, used by Alembic.
revision: str = 'e3906f5a7c4b'
down_revision: Union[str, Sequence[str], None] = 'd2895e4f6b3a'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    bind = op.get_bind()
    insp = inspect(bind)

    # 1. Update conversations table
    conv_cols = [c['name'] for c in insp.get_columns('conversations')]
    if 'listing_id' not in conv_cols:
        op.add_column('conversations', sa.Column('listing_id', sa.Integer(), nullable=True))
        try:
            op.create_foreign_key(
                'fk_conversations_listing_id',
                'conversations', 'provider_listings',
                ['listing_id'], ['id'],
                ondelete='SET NULL'
            )
        except Exception:
            pass

    if 'provider_id' not in conv_cols:
        op.add_column('conversations', sa.Column('provider_id', sa.Integer(), nullable=True))
        try:
            op.create_foreign_key(
                'fk_conversations_provider_id',
                'conversations', 'provider_accounts',
                ['provider_id'], ['id'],
                ondelete='SET NULL'
            )
        except Exception:
            pass

    if 'model' not in conv_cols:
        op.add_column('conversations', sa.Column('model', sa.String(length=100), nullable=True))

    if 'status' not in conv_cols:
        op.add_column('conversations', sa.Column('status', sa.String(length=50), server_default='ACTIVE', nullable=False))

    # 2. Update chat_messages table
    msg_cols = [c['name'] for c in insp.get_columns('chat_messages')]
    if 'model' not in msg_cols:
        op.add_column('chat_messages', sa.Column('model', sa.String(length=100), nullable=True))

    if 'provider_request_id' not in msg_cols:
        op.add_column('chat_messages', sa.Column('provider_request_id', sa.String(length=100), nullable=True))

    # 3. Update provider_connections table
    conn_cols = [c['name'] for c in insp.get_columns('provider_connections')]
    if 'execution_status' not in conn_cols:
        op.add_column('provider_connections', sa.Column('execution_status', sa.String(length=50), server_default='EXECUTION_NOT_SUPPORTED', nullable=False))

    if 'failure_reason' not in conn_cols:
        op.add_column('provider_connections', sa.Column('failure_reason', sa.Text(), nullable=True))

    if 'capacity_source' not in conn_cols:
        op.add_column('provider_connections', sa.Column('capacity_source', sa.String(length=100), server_default='Provider API Capacity', nullable=False))


def downgrade() -> None:
    pass
