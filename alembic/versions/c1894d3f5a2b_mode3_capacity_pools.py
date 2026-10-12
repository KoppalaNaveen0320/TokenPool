"""mode3_capacity_pools

Revision ID: c1894d3f5a2b
Revises: 15ab10972bb8
Create Date: 2026-10-04 16:42:00.000000

"""
from typing import Sequence, Union
from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect


# revision identifiers, used by Alembic.
revision: str = 'c1894d3f5a2b'
down_revision: Union[str, Sequence[str], None] = '15ab10972bb8'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    bind = op.get_bind()
    insp = inspect(bind)

    # 1. Add new columns to ai_models
    ai_models_cols = [c['name'] for c in insp.get_columns('ai_models')]
    if 'capacity_unit' not in ai_models_cols:
        op.add_column('ai_models', sa.Column('capacity_unit', sa.String(length=50), server_default='TOKEN', nullable=False))
    if 'supported_periods' not in ai_models_cols:
        op.add_column('ai_models', sa.Column('supported_periods', sa.String(length=100), server_default='DAILY,MONTHLY,YEARLY', nullable=False))
    if 'execution_adapter' not in ai_models_cols:
        op.add_column('ai_models', sa.Column('execution_adapter', sa.String(length=50), server_default='mock', nullable=False))

    tables = insp.get_table_names()

    # 2. Create ai_capacity_pools table if not exists
    if 'ai_capacity_pools' not in tables:
        op.create_table(
            'ai_capacity_pools',
            sa.Column('id', sa.Integer(), nullable=False),
            sa.Column('model_id', sa.Integer(), nullable=False),
            sa.Column('model_slug', sa.String(length=100), nullable=False),
            sa.Column('provider_brand', sa.String(length=100), nullable=False),
            sa.Column('execution_adapter', sa.String(length=50), nullable=False),
            sa.Column('capacity_unit', sa.String(length=50), server_default='TOKEN', nullable=False),
            sa.Column('period_type', sa.String(length=50), server_default='MONTHLY', nullable=False),
            sa.Column('total_capacity', sa.Integer(), server_default='0', nullable=False),
            sa.Column('allocated_capacity', sa.Integer(), server_default='0', nullable=False),
            sa.Column('reserved_capacity', sa.Integer(), server_default='0', nullable=False),
            sa.Column('sold_capacity', sa.Integer(), server_default='0', nullable=False),
            sa.Column('consumed_capacity', sa.Integer(), server_default='0', nullable=False),
            sa.Column('remaining_capacity', sa.Integer(), server_default='0', nullable=False),
            sa.Column('period_start', sa.DateTime(), nullable=False),
            sa.Column('period_end', sa.DateTime(), nullable=False),
            sa.Column('status', sa.String(length=50), server_default='UNVERIFIED', nullable=False),
            sa.Column('created_at', sa.DateTime(), nullable=True),
            sa.Column('updated_at', sa.DateTime(), nullable=True),
            sa.ForeignKeyConstraint(['model_id'], ['ai_models.id'], ondelete='CASCADE'),
            sa.PrimaryKeyConstraint('id')
        )
        op.create_index(op.f('ix_ai_capacity_pools_id'), 'ai_capacity_pools', ['id'], unique=False)
        op.create_index(op.f('ix_ai_capacity_pools_model_slug'), 'ai_capacity_pools', ['model_slug'], unique=False)

    # 3. Create provider_capacity_allocations table if not exists
    if 'provider_capacity_allocations' not in tables:
        op.create_table(
            'provider_capacity_allocations',
            sa.Column('id', sa.Integer(), nullable=False),
            sa.Column('provider_id', sa.Integer(), nullable=False),
            sa.Column('capacity_pool_id', sa.Integer(), nullable=False),
            sa.Column('model_id', sa.Integer(), nullable=False),
            sa.Column('model_slug', sa.String(length=100), nullable=False),
            sa.Column('allocated_capacity', sa.Integer(), server_default='0', nullable=False),
            sa.Column('reserved_capacity', sa.Integer(), server_default='0', nullable=False),
            sa.Column('sold_capacity', sa.Integer(), server_default='0', nullable=False),
            sa.Column('consumed_capacity', sa.Integer(), server_default='0', nullable=False),
            sa.Column('remaining_capacity', sa.Integer(), server_default='0', nullable=False),
            sa.Column('capacity_unit', sa.String(length=50), server_default='TOKEN', nullable=False),
            sa.Column('period_type', sa.String(length=50), server_default='MONTHLY', nullable=False),
            sa.Column('period_start', sa.DateTime(), nullable=False),
            sa.Column('period_end', sa.DateTime(), nullable=False),
            sa.Column('status', sa.String(length=50), server_default='ACTIVE', nullable=False),
            sa.Column('created_at', sa.DateTime(), nullable=True),
            sa.Column('updated_at', sa.DateTime(), nullable=True),
            sa.ForeignKeyConstraint(['capacity_pool_id'], ['ai_capacity_pools.id'], ondelete='CASCADE'),
            sa.ForeignKeyConstraint(['model_id'], ['ai_models.id'], ondelete='CASCADE'),
            sa.ForeignKeyConstraint(['provider_id'], ['provider_accounts.id'], ondelete='CASCADE'),
            sa.PrimaryKeyConstraint('id')
        )
        op.create_index(op.f('ix_provider_capacity_allocations_id'), 'provider_capacity_allocations', ['id'], unique=False)
        op.create_index(op.f('ix_provider_capacity_allocations_model_slug'), 'provider_capacity_allocations', ['model_slug'], unique=False)
        op.create_index(op.f('ix_provider_capacity_allocations_provider_id'), 'provider_capacity_allocations', ['provider_id'], unique=False)
        op.create_index(op.f('ix_provider_capacity_allocations_capacity_pool_id'), 'provider_capacity_allocations', ['capacity_pool_id'], unique=False)

    # 4. Add new columns to provider_listings
    pl_cols = [c['name'] for c in insp.get_columns('provider_listings')]
    if 'provider_capacity_allocation_id' not in pl_cols:
        op.add_column('provider_listings', sa.Column('provider_capacity_allocation_id', sa.Integer(), nullable=True))
    if 'capacity_unit' not in pl_cols:
        op.add_column('provider_listings', sa.Column('capacity_unit', sa.String(length=50), server_default='TOKEN', nullable=True))
    if 'period_type' not in pl_cols:
        op.add_column('provider_listings', sa.Column('period_type', sa.String(length=50), server_default='MONTHLY', nullable=True))
    if 'period_start' not in pl_cols:
        op.add_column('provider_listings', sa.Column('period_start', sa.DateTime(), nullable=True))
    if 'period_end' not in pl_cols:
        op.add_column('provider_listings', sa.Column('period_end', sa.DateTime(), nullable=True))
    if 'total_capacity' not in pl_cols:
        op.add_column('provider_listings', sa.Column('total_capacity', sa.Integer(), server_default='18500', nullable=True))
    if 'sold_capacity' not in pl_cols:
        op.add_column('provider_listings', sa.Column('sold_capacity', sa.Integer(), server_default='0', nullable=True))
    if 'consumed_capacity' not in pl_cols:
        op.add_column('provider_listings', sa.Column('consumed_capacity', sa.Integer(), server_default='0', nullable=True))
    if 'remaining_capacity' not in pl_cols:
        op.add_column('provider_listings', sa.Column('remaining_capacity', sa.Integer(), server_default='18500', nullable=True))
    if 'updated_at' not in pl_cols:
        op.add_column('provider_listings', sa.Column('updated_at', sa.DateTime(), nullable=True))


def downgrade() -> None:
    pass
