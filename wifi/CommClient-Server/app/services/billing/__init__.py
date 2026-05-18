"""Phase 7 / Module AG — Billing & Usage Metering service package.

Public re-exports and submodule access for billing, metering, license
signing, dunning, and reporting subsystems. Submodules are imported on
demand by routers/services; this package intentionally keeps top-level
imports light to avoid pulling optional crypto deps unless they are used.
"""
