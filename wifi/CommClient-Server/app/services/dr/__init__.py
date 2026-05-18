"""Phase 6 / Module AA — Disaster Recovery service package.

Subsystems
----------
backup_engine     — create full / incremental / snapshot backups
destinations      — pluggable upload targets (local / S3 / SFTP / Azure / GCS)
restore_engine    — verify, simulate, and apply restores
drill_scheduler   — periodic DR rehearsals with RTO / RPO measurement
encryption        — AES-256-GCM streaming envelope used by backup_engine
"""
