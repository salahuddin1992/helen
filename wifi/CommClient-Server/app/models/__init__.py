"""
Central model registry — import all models here so SQLAlchemy discovers them.
"""

from app.models.user import User
from app.models.session import UserSession
from app.models.contact import Contact
from app.models.profile_photo import ProfilePhoto
from app.models.channel import Channel, ChannelMember
from app.models.message import Message, Reaction
from app.models.file import FileRecord
from app.models.call_log import CallLog
from app.models.message_status import MessageReceipt
from app.models.notification import Notification
from app.models.voice_message import VoiceMessage
from app.models.e2ee_key import IdentityKey, SignedPreKey, OneTimePreKey, E2EESession
from app.models.whiteboard import WhiteboardSession, WhiteboardStroke, WhiteboardSnapshot
from app.models.media_gallery import MediaItem, MediaAlbum, MediaAlbumItem
from app.models.file_drop import FileTransfer, SharedFolder, SharedFolderFile
from app.models.audit_log import AuditLog
from app.models.scheduled_message import ScheduledMessage
from app.models.device_token import DeviceToken
from app.models.saved_message import SavedMessage
from app.models.poll import Poll, PollOption, PollVote
from app.models.webhook import Webhook, WebhookDelivery
from app.models.message_draft import MessageDraft
from app.models.message_edit_history import MessageEditHistory
from app.models.channel_category import ChannelCategory, ChannelCategoryAssignment
from app.models.user_schedule import UserScheduleRule, UserAwayMessage
from app.models.message_template import MessageTemplate
from app.models.channel_permission import (
    ChannelRolePermission,
    ChannelMemberPermission,
)
from app.models.active_call import (
    ActiveCall,
    ActiveCallParticipant,
    CallSignalEvent,
)
from app.models.upload_session import (
    UploadSession,
    UploadChunk,
)
from app.models.file_acceptance import FileAcceptance
from app.models.message_dead_letter import MessageDeadLetter
from app.models.group_file_offer import (
    GroupFileOffer,
    GroupFileChunkAvailability,
)
from app.models.media_policy import (
    MediaPolicy,
    UserMediaOverride,
    IngestSource,
    CameraQualityPreset,
)
from app.models.route_trace import RouteTrace, RouteHop
from app.models.server_node import ServerNode
from app.models.peer_approval_audit import PeerApprovalAudit
from app.models.audit_alert_rule import AuditAlertRule
from app.models.legal_hold import LegalHold
from app.models.retention_policy import RetentionPolicy
from app.models.audit_export_job import AuditExportJob
from app.models.billing_license import (
    BillingLicense,
    LicenseRevocation,
    PlanAuditEntry,
    TenantAdminSession,
    RbacPasswordReset,
)
# Plugin marketplace extensions (Phase 7 / Module AH)
from app.models.plugin_rating import PluginRating
from app.models.plugin_signer import VerifiedSigner
from app.models.plugin_job import PluginJob

# Federation Health Map (operator-facing extensions of federation_v2)
from app.models.federation_peer import FederationPeerMeta
from app.models.federation_shaper_rule import FederationShaperRule
from app.models.federation_policy import FederationPolicy
from app.models.federation_cert import FederationCert
from app.models.federation_event_log import FederationEventLog

# Disaster Recovery v2 — chunked backups, policies, drills, keys.
from app.models.dr_v2 import (
    DRBackup,
    DRBackupChunk,
    DRDestination,
    DRDrillV2,
    DREncryptionKey,
    DRJob,
    DRPolicy,
)

# Operator Onboarding Wizard
from app.models.onboarding_state import OnboardingState
from app.models.system_cert import SystemCert
from app.models.router_pairing import RouterPairing
from app.models.admin_recovery_code import AdminRecoveryCode

# Compliance / eDiscovery Workbench (Module AB part B)
from app.models.compliance_hold import (
    ComplianceHold,
    ComplianceHoldAudit,
)
from app.models.compliance_retention import (
    ComplianceRetentionPolicy,
    ComplianceRetentionJob,
)
from app.models.compliance_case import (
    ComplianceCase,
    ComplianceCaseEvidence,
    ComplianceCaseExport,
)
from app.models.compliance_dsar import DSARRequest
from app.models.compliance_rtbf import RTBFRequest
from app.models.compliance_classification import (
    ClassificationRule,
    ClassificationFinding,
)
from app.models.compliance_report import (
    ComplianceReport,
    ComplianceReportSchedule,
)

# Helen-Router admin proxy — pairing + control config persistence.
from app.models.router_control_config import RouterControlConfig
