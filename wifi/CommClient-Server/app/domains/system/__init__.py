"""
app.domains.system — Config, audit, crypto, secrets, backup, monitoring.

Existing implementation locations:
    app.core.config             — Settings, get_settings()
    app.core.crypto             — AES helpers
    app.core.audit              — audit_log helpers
    app.core.persistent_secrets — encrypted .env store
    app.core.secrets_resolver   — Phase 1 Module B
    app.core.tls                — TLS context builders
    app.core.logging            — structlog setup
    app.services.backup_scheduler — periodic backup runner
    app.services.backup_verifier  — integrity check
    app.monitoring.*              — metrics, health, alerts, latency
"""

from __future__ import annotations

from app.domains._safe_import import safe_import, safe_module

_exports: dict = {}

# Core
_exports.update(safe_import(
    "app.core.config",
    ["Settings", "get_settings", "settings"],
))
_exports.update(safe_import(
    "app.core.crypto",
    ["encrypt_at_rest", "decrypt_at_rest", "fernet_key"],
))
_exports.update(safe_import(
    "app.core.audit",
    ["audit_event", "AuditLog", "log_action"],
))
_exports.update(safe_import(
    "app.core.persistent_secrets",
    ["PersistentSecrets", "get_persistent_secrets"],
))
_exports.update(safe_import(
    "app.core.secrets_resolver",
    [
        "SecretsResolver",
        "resolve_secret",
        "get_secrets_resolver",
        "SecretSource",
    ],
))
_exports.update(safe_import(
    "app.core.tls",
    ["build_ssl_context", "load_or_generate_cert"],
))
_exports.update(safe_import(
    "app.core.logging",
    ["configure_logging", "get_logger"],
))

# Backup
_exports.update(safe_import(
    "app.services.backup_scheduler",
    ["BackupScheduler", "start_backup_scheduler"],
))
_exports.update(safe_import(
    "app.services.backup_verifier",
    ["BackupVerifier", "verify_backup"],
))

# Monitoring sub-package — re-export whole namespace
_mon = safe_module("app.monitoring")
if _mon is not None:
    _exports["monitoring"] = _mon
_exports.update(safe_import(
    "app.monitoring.monitoring_manager",
    ["MonitoringManager", "get_monitoring_manager"],
))
_exports.update(safe_import(
    "app.monitoring.health_checker",
    ["HealthChecker"],
))
_exports.update(safe_import(
    "app.monitoring.metrics_collector",
    ["MetricsCollector"],
))
_exports.update(safe_import(
    "app.monitoring.alert_manager",
    ["AlertManager"],
))

# Models
_exports.update(safe_import("app.models.audit_log", ["AuditLog"]))

globals().update(_exports)
__all__ = sorted(_exports.keys())
