"""Initial schema with all tables

Revision ID: 001
Revises:
Create Date: 2026-04-08 00:00:00.000000

Creates all core tables for CommClient-Server:
- users: User accounts and profiles
- user_sessions: Active JWT sessions per device
- contacts: Buddy list relationships
- channels: Direct messages (DM) and group channels
- channel_members: Channel membership with roles
- messages: Text, file, and system messages
- reactions: Message emoji reactions
- files: File metadata and storage tracking
- call_logs: Audio/video/screen share call history
- message_receipts: Per-recipient delivery and read tracking
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = '001'
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Create all initial tables."""

    # ────────────────────────────────────────────────────────────
    # users table
    # ────────────────────────────────────────────────────────────
    op.create_table(
        'users',
        sa.Column('id', sa.String(32), nullable=False),
        sa.Column('username', sa.String(64), nullable=False),
        sa.Column('display_name', sa.String(128), nullable=False),
        sa.Column('password_hash', sa.String(256), nullable=False),
        sa.Column('avatar_url', sa.Text(), nullable=True),
        sa.Column('bio', sa.String(500), nullable=True),
        sa.Column('status', sa.String(16), nullable=False, server_default='offline'),
        sa.Column('last_seen', sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column('is_active', sa.Boolean(), nullable=False, server_default='1'),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('username', name='uq_users_username'),
    )
    op.create_index(op.f('ix_users_username'), 'users', ['username'])

    # ────────────────────────────────────────────────────────────
    # user_sessions table
    # ────────────────────────────────────────────────────────────
    op.create_table(
        'user_sessions',
        sa.Column('id', sa.String(32), nullable=False),
        sa.Column('user_id', sa.String(32), nullable=False),
        sa.Column('token_hash', sa.String(256), nullable=False),
        sa.Column('device_name', sa.String(256), nullable=True),
        sa.Column('ip_address', sa.String(45), nullable=True),
        sa.Column('user_agent', sa.Text(), nullable=True),
        sa.Column('is_active', sa.Boolean(), nullable=False, server_default='1'),
        sa.Column('last_activity', sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column('expires_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.ForeignKeyConstraint(['user_id'], ['users.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('token_hash', name='uq_user_sessions_token_hash'),
    )
    op.create_index(op.f('ix_user_sessions_user_id'), 'user_sessions', ['user_id'])

    # ────────────────────────────────────────────────────────────
    # contacts table (buddy list)
    # ────────────────────────────────────────────────────────────
    op.create_table(
        'contacts',
        sa.Column('id', sa.String(32), nullable=False),
        sa.Column('user_id', sa.String(32), nullable=False),
        sa.Column('contact_id', sa.String(32), nullable=False),
        sa.Column('nickname', sa.String(128), nullable=True),
        sa.Column('is_blocked', sa.Boolean(), nullable=False, server_default='0'),
        sa.Column('is_favorite', sa.Boolean(), nullable=False, server_default='0'),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.ForeignKeyConstraint(['contact_id'], ['users.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['user_id'], ['users.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('user_id', 'contact_id', name='uq_user_contact'),
    )
    op.create_index(op.f('ix_contacts_contact_id'), 'contacts', ['contact_id'])
    op.create_index(op.f('ix_contacts_user_id'), 'contacts', ['user_id'])

    # ────────────────────────────────────────────────────────────
    # channels table (DM and group)
    # ────────────────────────────────────────────────────────────
    op.create_table(
        'channels',
        sa.Column('id', sa.String(32), nullable=False),
        sa.Column('type', sa.String(16), nullable=False),
        sa.Column('name', sa.String(128), nullable=True),
        sa.Column('description', sa.Text(), nullable=True),
        sa.Column('avatar_url', sa.Text(), nullable=True),
        sa.Column('created_by', sa.String(32), nullable=True),
        sa.Column('is_active', sa.Boolean(), nullable=False, server_default='1'),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.ForeignKeyConstraint(['created_by'], ['users.id'], ondelete='SET NULL'),
        sa.PrimaryKeyConstraint('id'),
    )

    # ────────────────────────────────────────────────────────────
    # channel_members table (membership tracking)
    # ────────────────────────────────────────────────────────────
    op.create_table(
        'channel_members',
        sa.Column('channel_id', sa.String(32), nullable=False),
        sa.Column('user_id', sa.String(32), nullable=False),
        sa.Column('role', sa.String(16), nullable=False, server_default='member'),
        sa.Column('last_read_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('is_muted', sa.Boolean(), nullable=False, server_default='0'),
        sa.Column('joined_at', sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.ForeignKeyConstraint(['channel_id'], ['channels.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['user_id'], ['users.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('channel_id', 'user_id'),
        sa.UniqueConstraint('channel_id', 'user_id', name='uq_channel_member'),
    )
    op.create_index(op.f('ix_channel_members_user_id'), 'channel_members', ['user_id'])

    # ────────────────────────────────────────────────────────────
    # files table (file metadata and storage)
    # ────────────────────────────────────────────────────────────
    op.create_table(
        'files',
        sa.Column('id', sa.String(32), nullable=False),
        sa.Column('uploader_id', sa.String(32), nullable=False),
        sa.Column('channel_id', sa.String(32), nullable=True),
        sa.Column('original_name', sa.String(512), nullable=False),
        sa.Column('stored_name', sa.String(256), nullable=False),
        sa.Column('mime_type', sa.String(128), nullable=False),
        sa.Column('size_bytes', sa.BigInteger(), nullable=False),
        sa.Column('storage_path', sa.Text(), nullable=False),
        sa.Column('thumbnail_path', sa.Text(), nullable=True),
        sa.Column('checksum_sha256', sa.String(64), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.ForeignKeyConstraint(['channel_id'], ['channels.id'], ondelete='SET NULL'),
        sa.ForeignKeyConstraint(['uploader_id'], ['users.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('stored_name', name='uq_files_stored_name'),
    )
    op.create_index(op.f('ix_files_uploader_id'), 'files', ['uploader_id'])

    # ────────────────────────────────────────────────────────────
    # messages table (text, file, system, replies)
    # ────────────────────────────────────────────────────────────
    op.create_table(
        'messages',
        sa.Column('id', sa.String(32), nullable=False),
        sa.Column('channel_id', sa.String(32), nullable=False),
        sa.Column('sender_id', sa.String(32), nullable=False),
        sa.Column('content', sa.Text(), nullable=False),
        sa.Column('type', sa.String(16), nullable=False, server_default='text'),
        sa.Column('reply_to', sa.String(32), nullable=True),
        sa.Column('file_id', sa.String(32), nullable=True),
        sa.Column('status', sa.String(16), nullable=False, server_default='sent'),
        sa.Column('edited_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('deleted_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.ForeignKeyConstraint(['channel_id'], ['channels.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['file_id'], ['files.id'], ondelete='SET NULL'),
        sa.ForeignKeyConstraint(['reply_to'], ['messages.id'], ondelete='SET NULL'),
        sa.ForeignKeyConstraint(['sender_id'], ['users.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index(op.f('ix_messages_channel_id'), 'messages', ['channel_id'])
    op.create_index(op.f('ix_messages_sender_id'), 'messages', ['sender_id'])

    # ────────────────────────────────────────────────────────────
    # reactions table (emoji reactions on messages)
    # ────────────────────────────────────────────────────────────
    op.create_table(
        'reactions',
        sa.Column('message_id', sa.String(32), nullable=False),
        sa.Column('user_id', sa.String(32), nullable=False),
        sa.Column('emoji', sa.String(32), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.ForeignKeyConstraint(['message_id'], ['messages.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['user_id'], ['users.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('message_id', 'user_id', 'emoji'),
        sa.UniqueConstraint('message_id', 'user_id', 'emoji', name='uq_reaction'),
    )

    # ────────────────────────────────────────────────────────────
    # call_logs table (audio/video call history)
    # ────────────────────────────────────────────────────────────
    op.create_table(
        'call_logs',
        sa.Column('id', sa.String(32), nullable=False),
        sa.Column('channel_id', sa.String(32), nullable=True),
        sa.Column('initiator_id', sa.String(32), nullable=False),
        sa.Column('call_type', sa.String(16), nullable=False),
        sa.Column('routing', sa.String(8), nullable=False),
        sa.Column('status', sa.String(16), nullable=False, server_default='ringing'),
        sa.Column('started_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('ended_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('duration_seconds', sa.Integer(), nullable=True),
        sa.Column('end_reason', sa.String(32), nullable=True),
        sa.Column('participant_count', sa.Integer(), nullable=False, server_default='2'),
        sa.Column('metadata_json', sa.Text(), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.ForeignKeyConstraint(['channel_id'], ['channels.id'], ondelete='SET NULL'),
        sa.ForeignKeyConstraint(['initiator_id'], ['users.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index(op.f('ix_call_logs_channel_id'), 'call_logs', ['channel_id'])

    # ────────────────────────────────────────────────────────────
    # message_receipts table (delivery/read tracking per recipient)
    # ────────────────────────────────────────────────────────────
    op.create_table(
        'message_receipts',
        sa.Column('id', sa.String(32), nullable=False),
        sa.Column('message_id', sa.String(32), nullable=False),
        sa.Column('recipient_id', sa.String(32), nullable=False),
        sa.Column('delivered_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('read_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.ForeignKeyConstraint(['message_id'], ['messages.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['recipient_id'], ['users.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('message_id', 'recipient_id', name='uq_message_receipt'),
    )
    op.create_index(op.f('ix_message_receipts_message_id'), 'message_receipts', ['message_id'])
    op.create_index(op.f('ix_message_receipts_recipient_id'), 'message_receipts', ['recipient_id'])
    op.create_index('ix_receipt_recipient_delivered', 'message_receipts', ['recipient_id', 'delivered_at'])
    op.create_index('ix_receipt_message_read', 'message_receipts', ['message_id', 'read_at'])


def downgrade() -> None:
    """Drop all tables in reverse order."""
    op.drop_index('ix_receipt_message_read', table_name='message_receipts')
    op.drop_index('ix_receipt_recipient_delivered', table_name='message_receipts')
    op.drop_index(op.f('ix_message_receipts_recipient_id'), table_name='message_receipts')
    op.drop_index(op.f('ix_message_receipts_message_id'), table_name='message_receipts')
    op.drop_table('message_receipts')

    op.drop_index(op.f('ix_call_logs_channel_id'), table_name='call_logs')
    op.drop_table('call_logs')

    op.drop_table('reactions')

    op.drop_index(op.f('ix_messages_sender_id'), table_name='messages')
    op.drop_index(op.f('ix_messages_channel_id'), table_name='messages')
    op.drop_table('messages')

    op.drop_index(op.f('ix_files_uploader_id'), table_name='files')
    op.drop_table('files')

    op.drop_index(op.f('ix_channel_members_user_id'), table_name='channel_members')
    op.drop_table('channel_members')

    op.drop_table('channels')

    op.drop_index(op.f('ix_contacts_user_id'), table_name='contacts')
    op.drop_index(op.f('ix_contacts_contact_id'), table_name='contacts')
    op.drop_table('contacts')

    op.drop_index(op.f('ix_user_sessions_user_id'), table_name='user_sessions')
    op.drop_table('user_sessions')

    op.drop_index(op.f('ix_users_username'), table_name='users')
    op.drop_table('users')
