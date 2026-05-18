"""Add unique constraint and not-null safeguard to picks table

Revision ID: a1b2c3d4e5f6
Revises: c16973330891
Create Date: 2026-05-18 00:00:00.000000

"""
from alembic import op
import sqlalchemy as sa

revision = 'a1b2c3d4e5f6'
down_revision = 'c16973330891'
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table('picks', schema=None) as batch_op:
        # Prevent a player from having more than one pick per round
        batch_op.create_unique_constraint(
            'uq_picks_player_round',
            ['player_id', 'round_id']
        )


def downgrade():
    with op.batch_alter_table('picks', schema=None) as batch_op:
        batch_op.drop_constraint('uq_picks_player_round', type_='unique')
