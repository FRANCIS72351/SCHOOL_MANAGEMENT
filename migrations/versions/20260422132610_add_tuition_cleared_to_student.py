"""add tuition_cleared to student table

Revision ID: 20260422132610
Revises: 202511111915
Create Date: 2026-04-22 13:26:10.000000

"""

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = "20260422132610"
down_revision = "202511111915"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("student", sa.Column("tuition_cleared", sa.Boolean(), nullable=False, default=False))


def downgrade():
    op.drop_column("student", "tuition_cleared")