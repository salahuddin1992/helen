"""Outbound-connectivity strategies — reach Helen-Server from outside NAT.

Order of escalation (fastest → slowest):
  1. LAN direct   — same-subnet clients (handled elsewhere via discovery).
  2. UPnP/NAT-PMP — router-assisted port mapping (see ``admin_app.router``).
  3. Reverse tunnel via Helen-Rendezvous — outbound WebSocket holds a
     permanent backhaul; external clients hit the rendezvous URL and the
     request is proxied back into this server.
  4. UDP hole punching (skeleton — ICE-complete impl deferred).
  5. TCP relay (blind byte forwarder, last resort).

The :class:`ConnectivityOrchestrator` runs the probes and exposes the
chosen method(s) to the admin dashboard.
"""

from app.services.connectivity.reverse_tunnel import ReverseTunnelClient  # noqa: F401
from app.services.connectivity.orchestrator import ConnectivityOrchestrator, orchestrator  # noqa: F401
