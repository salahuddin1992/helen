"""
Operator Onboarding services package.

Public re-exports for the FastAPI router and the alembic-bootstrapped
state machine. Each submodule isolates one concern:

    state_machine     — declarative 14-step DAG + validators
    system_inspector  — host/CPU/RAM/disk/NIC probing
    firewall_manager  — cross-OS firewall rule application
    cert_manager      — TLS material generation + import
    totp              — RFC-6238 TOTP (pyotp-preferred)
    recovery_codes    — one-time admin recovery code generator
    router_pairing    — TOFU router public-key exchange
    finalizer         — atomic transaction wrapping all 14 steps

All services are async-safe and structlog-instrumented.
"""
from __future__ import annotations

from app.services.onboarding.state_machine import (  # noqa: F401
    OnboardingStateMachine,
    STEP_DEFINITIONS,
    StepDefinition,
    StepValidationError,
)
from app.services.onboarding.system_inspector import SystemInspector  # noqa: F401
from app.services.onboarding.firewall_manager import FirewallManager  # noqa: F401
from app.services.onboarding.cert_manager import OnboardingCertManager  # noqa: F401
from app.services.onboarding.totp import TOTPManager  # noqa: F401
from app.services.onboarding.recovery_codes import (  # noqa: F401
    generate_recovery_codes,
    hash_recovery_code,
    verify_recovery_code,
)
from app.services.onboarding.router_pairing import RouterPairingService  # noqa: F401
from app.services.onboarding.finalizer import OnboardingFinalizer  # noqa: F401

__all__ = [
    "OnboardingStateMachine",
    "STEP_DEFINITIONS",
    "StepDefinition",
    "StepValidationError",
    "SystemInspector",
    "FirewallManager",
    "OnboardingCertManager",
    "TOTPManager",
    "generate_recovery_codes",
    "hash_recovery_code",
    "verify_recovery_code",
    "RouterPairingService",
    "OnboardingFinalizer",
]
