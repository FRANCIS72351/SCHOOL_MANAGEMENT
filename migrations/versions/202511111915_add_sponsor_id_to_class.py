"""add sponsor_id to class table

Revision ID: 202511111915
Revises: 
Create Date: 2025-11-11 19:15:00.000000

"""

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = "202511111915"
down_revision = None
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("class", sa.Column("sponsor_id", sa.Integer(), nullable=True))
    op.create_foreign_key(
        "fk_class_sponsor_id_users",
        "class",
        "users",
        ["sponsor_id"],
        ["id"],
        ondelete="SET NULL"
    )


def downgrade():
    op.drop_constraint("fk_class_sponsor_id_users", "class", type_="foreignkey")
    op.drop_column("class", "sponsor_id")