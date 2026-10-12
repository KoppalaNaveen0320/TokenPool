"""gmail_ai_account_connections

Revision ID: d2895e4f6b3a
Revises: c1894d3f5a2b
Create Date: 2026-10-06 09:30:00.000000

"""
from typing import Sequence, Union
from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect


# revision identifiers, used by Alembic.
revision: str = 'd2895e4f6b3a'
down_revision: Union[str, Sequence[str], None] = 'c1894d3f5a2b'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    bind = op.get_bind()
    insp = inspect(bind)

    # 1. Update users table with google_sub_id and email_verified
    user_cols = [c['name'] for c in insp.get_columns('users')]
    if 'google_sub_id' not in user_cols:
        op.add_column('users', sa.Column('google_sub_id', sa.String(length=255), nullable=True))
        op.create_index('ix_users_google_sub_id', 'users', ['google_sub_id'], unique=True)
    if 'email_verified' not in user_cols:
        op.add_column('users', sa.Column('email_verified', sa.Boolean(), server_default='true', nullable=False))

    # 2. Update provider_connections table with Gmail-based AI account fields
    conn_cols = [c['name'] for c in insp.get_columns('provider_connections')]
    
    if 'account_email' not in conn_cols:
        op.add_column('provider_connections', sa.Column('account_email', sa.String(length=255), nullable=True))
        op.create_index('ix_provider_connections_account_email', 'provider_connections', ['account_email'])
    
    if 'account_identifier' not in conn_cols:
        op.add_column('provider_connections', sa.Column('account_identifier', sa.String(length=255), nullable=True))
        
    if 'connection_method' not in conn_cols:
        op.add_column('provider_connections', sa.Column('connection_method', sa.String(length=50), server_default='google_identity', nullable=False))
        
    if 'identity_status' not in conn_cols:
        op.add_column('provider_connections', sa.Column('identity_status', sa.String(length=50), server_default='IDENTITY_MATCHED', nullable=False))
        
    if 'authorization_status' not in conn_cols:
        op.add_column('provider_connections', sa.Column('authorization_status', sa.String(length=50), server_default='AUTHORIZED', nullable=False))
        
    if 'connection_status' not in conn_cols:
        op.add_column('provider_connections', sa.Column('connection_status', sa.String(length=50), server_default='CONNECTED', nullable=False))
        
    if 'plan_type' not in conn_cols:
        op.add_column('provider_connections', sa.Column('plan_type', sa.String(length=50), server_default='UNVERIFIED', nullable=False))
        
    if 'plan_name' not in conn_cols:
        op.add_column('provider_connections', sa.Column('plan_name', sa.String(length=100), server_default='Unable to verify', nullable=False))
        
    if 'capacity_status' not in conn_cols:
        op.add_column('provider_connections', sa.Column('capacity_status', sa.String(length=50), server_default='DECLARED', nullable=False))
        
    if 'marketplace_status' not in conn_cols:
        op.add_column('provider_connections', sa.Column('marketplace_status', sa.String(length=50), server_default='NOT_ELIGIBLE', nullable=False))
        
    if 'marketplace_reason' not in conn_cols:
        op.add_column('provider_connections', sa.Column('marketplace_reason', sa.Text(), nullable=True))
        
    if 'encrypted_authorization_data' not in conn_cols:
        op.add_column('provider_connections', sa.Column('encrypted_authorization_data', sa.Text(), nullable=True))
        
    if 'token_expires_at' not in conn_cols:
        op.add_column('provider_connections', sa.Column('token_expires_at', sa.DateTime(), nullable=True))
        
    if 'last_verified_at' not in conn_cols:
        op.add_column('provider_connections', sa.Column('last_verified_at', sa.DateTime(), nullable=True))
        
    if 'last_health_check_at' not in conn_cols:
        op.add_column('provider_connections', sa.Column('last_health_check_at', sa.DateTime(), nullable=True))
        
    if 'revoked_at' not in conn_cols:
        op.add_column('provider_connections', sa.Column('revoked_at', sa.DateTime(), nullable=True))

    # 3. Backfill existing connections with account_email from provider's user email
    op.execute("""
        UPDATE provider_connections pc
        SET account_email = u.email,
            connection_status = pc.status,
            last_verified_at = pc.validated_at
        FROM provider_accounts pa
        JOIN users u ON u.id = pa.user_id
        WHERE pc.provider_id = pa.id
          AND pc.account_email IS NULL;
    """)

    # Backfill marketplace status for validated connections with declared capacity
    op.execute("""
        UPDATE provider_connections
        SET marketplace_status = 'ELIGIBLE',
            marketplace_reason = 'AI account is authorized and eligible for marketplace execution.'
        WHERE validation_status = 'VALIDATED'
          AND status = 'CONNECTED'
          AND capacity_declared > 0
          AND (marketplace_status IS NULL OR marketplace_status = 'NOT_ELIGIBLE');
    """)


def downgrade() -> None:
    op.drop_index('ix_users_google_sub_id', table_name='users')
    op.drop_column('users', 'google_sub_id')
    op.drop_column('users', 'email_verified')

    op.drop_index('ix_provider_connections_account_email', table_name='provider_connections')
    op.drop_column('provider_connections', 'account_email')
    op.drop_column('provider_connections', 'account_identifier')
    op.drop_column('provider_connections', 'connection_method')
    op.drop_column('provider_connections', 'identity_status')
    op.drop_column('provider_connections', 'authorization_status')
    op.drop_column('provider_connections', 'connection_status')
    op.drop_column('provider_connections', 'plan_type')
    op.drop_column('provider_connections', 'plan_name')
    op.drop_column('provider_connections', 'capacity_status')
    op.drop_column('provider_connections', 'marketplace_status')
    op.drop_column('provider_connections', 'marketplace_reason')
    op.drop_column('provider_connections', 'encrypted_authorization_data')
    op.drop_column('provider_connections', 'token_expires_at')
    op.drop_column('provider_connections', 'last_verified_at')
    op.drop_column('provider_connections', 'last_health_check_at')
    op.drop_column('provider_connections', 'revoked_at')
