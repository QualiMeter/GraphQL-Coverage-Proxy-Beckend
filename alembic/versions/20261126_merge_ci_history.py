"""Join the GitLab history and run admission migration branches."""

revision = "20261126_merge_ci_history"
down_revision = ("20261125_gitlab_history", "20261125_run_in_flight")
branch_labels = None
depends_on = None


def upgrade():
    pass


def downgrade():
    pass
