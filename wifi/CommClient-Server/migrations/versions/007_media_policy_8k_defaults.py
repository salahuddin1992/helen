"""Raise default media-policy role caps so all roles can reach 8K

Revision ID: 007
Revises: 006
Create Date: 2026-04-20

Rationale
---------
Migration 006 seeded the global policy with conservative role caps
(admin = 4K, moderator = 1440p, user = 1080p). The product goal is to
accept every camera at every resolution *by default* — the admin can
still lower caps per-role from the UI if they need to. This revision
rewrites the 'global' row's `role_caps_json` so fresh installs come up
with 8K/60/80Mbps available to every role out of the box.

Existing deployments where an admin has already customized caps are
left untouched: we only overwrite the row whose `role_caps_json`
matches the known 006-era default.
"""
from __future__ import annotations

from alembic import op


revision = "007"
down_revision = "006"
branch_labels = None
depends_on = None


_OLD_DEFAULT = (
    '{"admin":{"max_w":3840,"max_h":2160,"max_fps":60,"max_kbps":40000},'
    '"moderator":{"max_w":2560,"max_h":1440,"max_fps":30,"max_kbps":20000},'
    '"user":{"max_w":1920,"max_h":1080,"max_fps":30,"max_kbps":10000}}'
)

_NEW_DEFAULT = (
    '{"admin":{"max_w":7680,"max_h":4320,"max_fps":60,"max_kbps":80000},'
    '"moderator":{"max_w":3840,"max_h":2160,"max_fps":60,"max_kbps":40000},'
    '"user":{"max_w":7680,"max_h":4320,"max_fps":60,"max_kbps":80000}}'
)


def upgrade() -> None:
    bind = op.get_bind()
    # Only overwrite if the operator hasn't customized caps yet. Use
    # exec_driver_sql so SQLAlchemy doesn't try to parse JSON colons as
    # bind-parameter markers.
    bind.exec_driver_sql(
        f"""
        UPDATE media_policies
           SET role_caps_json        = '{_NEW_DEFAULT}',
               global_max_width      = 7680,
               global_max_height     = 4320,
               global_max_framerate  = 60,
               global_max_bitrate_kbps = 80000,
               allow_8k              = 1
         WHERE id = 'global'
           AND role_caps_json = '{_OLD_DEFAULT}'
        """
    )


def downgrade() -> None:
    bind = op.get_bind()
    bind.exec_driver_sql(
        f"""
        UPDATE media_policies
           SET role_caps_json        = '{_OLD_DEFAULT}',
               global_max_width      = 1920,
               global_max_height     = 1080,
               global_max_framerate  = 30,
               global_max_bitrate_kbps = 10000,
               allow_8k              = 0
         WHERE id = 'global'
           AND role_caps_json = '{_NEW_DEFAULT}'
        """
    )
