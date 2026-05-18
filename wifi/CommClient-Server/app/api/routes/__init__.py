"""
Route aggregator — registers all API routers on the FastAPI app.
"""

from __future__ import annotations

from fastapi import APIRouter

from app.api.routes.auth import router as auth_router
from app.api.routes.users import router as users_router
from app.api.routes.profile_photos import router as profile_photos_router
from app.api.routes.channels import router as channels_router
from app.api.routes.messages import router as messages_router
from app.api.routes.messages import search_router, msg_router, channel_router as msg_channel_router
from app.api.routes.files import router as files_router
from app.api.routes.sessions import router as sessions_router
from app.api.routes.calls import router as calls_router, channel_call_router
from app.api.routes.notifications import router as notifications_router
from app.api.routes.health import router as health_router
from app.api.routes.connection_diagnostics import router as connection_diagnostics_router
from app.api.routes.admin import router as admin_router
from app.api.routes.turn import router as turn_router
from app.api.routes.voice_messages import router as voice_messages_router
from app.api.routes.e2ee import router as e2ee_router
from app.api.routes.whiteboard import router as whiteboard_router
from app.api.routes.media_gallery import router as media_gallery_router
from app.api.routes.file_drop import router as file_drop_router
from app.api.routes.transport import router as transport_router
from app.api.routes.transports import router as transports_health_router
from app.api.routes.scheduled_messages import router as scheduled_messages_router
from app.api.routes.device_tokens import router as device_tokens_router
from app.api.routes.saved_messages import router as saved_messages_router
from app.api.routes.polls import router as polls_router, channel_polls_router
from app.api.routes.webhooks import router as webhooks_router
from app.api.routes.drafts import router as drafts_router
from app.api.routes.channel_categories import router as channel_categories_router
from app.api.routes.schedule import router as schedule_router
from app.api.routes.templates import router as templates_router
from app.api.routes.permissions import router as permissions_router
from app.api.routes.peers import router as peers_router
from app.api.routes.federation import router as federation_router
from app.api.routes.files_resumable import router as files_resumable_router
from app.api.routes.file_acceptance import (
    router as file_acceptance_router,
    inbox_router as file_acceptance_inbox_router,
)
from app.api.routes.sfu_events import router as sfu_events_router
from app.api.routes.dlq import router as dlq_router
from app.api.routes.chaos import router as chaos_router
from app.api.routes.metrics import router as metrics_router
from app.api.routes.admin_peers import router as admin_peers_router
from app.api.routes.group_file_offers import router as group_file_offers_router
from app.api.routes.media_policy import (
    user_router as media_policy_user_router,
    admin_router as media_policy_admin_router,
)
from app.api.routes.ingest import (
    user_router as ingest_user_router,
    admin_router as ingest_admin_router,
)
from app.api.routes.pair import router as pair_router, public_router as pair_public_router
from app.api.routes.calendar import router as calendar_router
from app.api.routes.transcription import router as transcription_router
from app.api.routes.operability import router as operability_router
from app.api.routes.online_mode import (
    public_router as online_mode_public_router,
    admin_router as online_mode_admin_router,
)
from app.api.routes.channel_join import router as channel_join_router
from app.api.routes.channel_slow_mode import router as channel_slow_mode_router
from app.api.routes.channel_message_ttl import router as channel_ttl_router
from app.api.routes.custom_emoji import router as custom_emoji_router

api_router = APIRouter(prefix="/api")

# Public
api_router.include_router(health_router)
api_router.include_router(connection_diagnostics_router)
api_router.include_router(auth_router)

# Authenticated
# Profile photos router uses /users/... paths too — mount before the generic
# users_router so its more-specific routes win FastAPI's match order.
api_router.include_router(profile_photos_router)
api_router.include_router(users_router)
api_router.include_router(channels_router)
api_router.include_router(messages_router)
api_router.include_router(search_router)
api_router.include_router(msg_router)
api_router.include_router(files_router)
api_router.include_router(files_resumable_router)
# Per-recipient acceptance tracking — inbox router mounted first so its
# more-specific prefix beats /files/{file_id} path matching.
api_router.include_router(file_acceptance_inbox_router)
api_router.include_router(file_acceptance_router)
api_router.include_router(sessions_router)
api_router.include_router(calls_router)
# /api/channels/{id}/active-call — discovery for "Join Existing Call" UX.
# Mounted as a sibling of channels_router so the FastAPI route table
# resolves /channels/{id}/active-call before the generic channel detail.
api_router.include_router(channel_call_router)
api_router.include_router(notifications_router)
api_router.include_router(admin_router)
api_router.include_router(operability_router)
api_router.include_router(online_mode_public_router)
api_router.include_router(online_mode_admin_router)
api_router.include_router(channel_join_router)
api_router.include_router(channel_slow_mode_router)
api_router.include_router(channel_ttl_router)
api_router.include_router(custom_emoji_router)

# Access codes (user-minted) + secret admin (master-code gated).
from app.api.routes.access_codes import router as _access_codes_router
from app.api.routes.secret_admin import router as _secret_admin_router
api_router.include_router(_access_codes_router)
api_router.include_router(_secret_admin_router, prefix="/secret-admin")

# Cluster mesh — /api/cluster/* public peer-facing endpoints.
from app.api.routes.cluster import router as _cluster_router
api_router.include_router(_cluster_router)

# Service Discovery — /api/discovery/* register/heartbeat/find.
from app.api.routes.discovery import router as _discovery_router
api_router.include_router(_discovery_router)

# Helen-Vault — dedicated secrets management panel (LAN-only).
from app.api.routes.vault import router as _vault_router
api_router.include_router(_vault_router, prefix="/vault")
api_router.include_router(dlq_router)
# Chaos engineering — admin-only AND env-flag gated. The router
# itself returns 403 for everything when HELEN_ENABLE_100_HOP_TEST_MODE
# is not set, so it's safe to register unconditionally.
api_router.include_router(chaos_router)
# Prometheus exposition — admin OR HELEN_METRICS_TOKEN bearer.
api_router.include_router(metrics_router)
# Peer acceptance admin APIs (4 modes: auto/manual/pending/human).
api_router.include_router(admin_peers_router)
api_router.include_router(msg_channel_router)
api_router.include_router(turn_router)
api_router.include_router(voice_messages_router)
api_router.include_router(e2ee_router)
api_router.include_router(whiteboard_router)
api_router.include_router(media_gallery_router)
api_router.include_router(file_drop_router)
api_router.include_router(group_file_offers_router)
api_router.include_router(transport_router)
api_router.include_router(transports_health_router)
api_router.include_router(scheduled_messages_router)
api_router.include_router(device_tokens_router)
api_router.include_router(saved_messages_router)
api_router.include_router(polls_router)
api_router.include_router(channel_polls_router)
api_router.include_router(webhooks_router)
api_router.include_router(drafts_router)
api_router.include_router(channel_categories_router)
api_router.include_router(schedule_router)
api_router.include_router(templates_router)
api_router.include_router(permissions_router)
api_router.include_router(media_policy_user_router)
api_router.include_router(media_policy_admin_router)
api_router.include_router(ingest_user_router)
api_router.include_router(ingest_admin_router)
api_router.include_router(pair_router)
api_router.include_router(calendar_router)
api_router.include_router(transcription_router)

# Public (no-auth, LAN peer discovery)
api_router.include_router(peers_router)

# Inter-server federation (HMAC-gated, not for end-user clients)
api_router.include_router(federation_router)

# Internal loopback — SFU worker → Python callback channel
api_router.include_router(sfu_events_router)

# SIEM / Audit Chain Dashboard — admin-grade audit chain UI + APIs +
# alert rules engine + legal holds + retention + live WebSocket stream.
from app.api.routes.admin_siem import router as _admin_siem_router
api_router.include_router(_admin_siem_router)

# Voice/Video QoS Live View — getStats() + MOS + mesh topology + admin
# overrides + WebSocket fan-out. Mounted last so its WebSocket route
# `/api/admin/ws/qos` doesn't shadow any prior /api/admin path.
from app.api.routes.admin_qos import router as _admin_qos_router
api_router.include_router(_admin_qos_router)

# Disaster Recovery Console v2 — destinations, backups, policies,
# drills, integrity, keys, reports + /ws/dr WebSocket fan-out.
# Router declares its own `/admin/dr` prefix; the parent `api_router`
# adds `/api`, giving final paths under `/api/admin/dr/…`.
try:
    from app.api.routes.admin_dr_v2 import router as _admin_dr_v2_router
    api_router.include_router(_admin_dr_v2_router)
except Exception:
    pass

# Helen-Router admin proxy — forwards /api/admin/router/* and
# /api/admin/mesh/* to the configured Helen-Router instance,
# swapping the admin's JWT for the shared router bearer token
# and auditing every write.
try:
    from app.api.routes.admin_router_control import (
        router as _admin_router_control_router,
    )
    api_router.include_router(_admin_router_control_router)
except Exception:
    pass

# Operator Onboarding Wizard — 14-step bootstrap flow. Pre-finalize the
# endpoints are bootstrap-tolerant (no Bearer needed); post-finalize they
# require admin auth (enforced inside the route's auth dependency).
try:
    from app.api.routes.admin_onboarding import router as _admin_onboarding_router
    api_router.include_router(_admin_onboarding_router)
except Exception:
    pass
