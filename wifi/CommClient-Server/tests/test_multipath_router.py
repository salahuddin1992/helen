"""Unit tests for multi-path routing (app.services.multipath_router).

We don't spin up a real cluster — instead we exercise:
  * Route construction & cooldown logic
  * score_route weighting + class-floor priority
  * select_strategy() responding to live conditions
  * RouteTable upsert + eviction
  * snapshot() shape

Network-touching paths (send / send_via_route / discover_routes) are
covered separately by the live topology harness.
"""

from __future__ import annotations

import time

import pytest

from app.services.multipath_router import (
    Route,
    RouteType,
    RouteTable,
    get_route_table,
    score_route,
    select_strategy,
    snapshot,
    ROUTE_WEIGHTS,
    PHI_REJECT_THRESHOLD,
    TRUST_REJECT_THRESHOLD,
    COOLDOWN_AFTER_FAIL_SEC,
)


# ── Route construction & properties ─────────────────────────────


def test_route_key_is_unique_per_target_type_hops():
    r1 = Route(target_node_id="t1", route_type=RouteType.DIRECT, hops=["t1"])
    r2 = Route(target_node_id="t1", route_type=RouteType.DIRECT, hops=["t1"])
    assert r1.key == r2.key

    r3 = Route(target_node_id="t1", route_type=RouteType.BRIDGE,
               hops=["bridge-1", "t1"])
    assert r1.key != r3.key


def test_route_hop_count_matches_hops_length():
    r = Route(target_node_id="t1", route_type=RouteType.MULTI_HOP_RELAY,
              hops=["a", "b", "c", "t1"])
    assert r.hop_count == 4


def test_route_cooldown_window():
    r = Route(target_node_id="t1", route_type=RouteType.DIRECT, hops=["t1"])
    assert not r.is_in_cooldown()

    r.failed_until = time.time() + 5.0
    assert r.is_in_cooldown()

    r.failed_until = time.time() - 5.0
    assert not r.is_in_cooldown()


# ── Route table singleton ───────────────────────────────────────


def test_route_table_upsert_dedupes_on_key():
    table = get_route_table()
    initial_count = len(table.all())

    r = Route(target_node_id="dedup-test", route_type=RouteType.DIRECT,
              hops=["dedup-test"], first_host="1.1.1.1", first_port=3000)
    a = table.upsert(r)
    b = table.upsert(r)
    assert a is b
    assert len(table.for_target("dedup-test")) == 1

    # Different route type → new entry.
    r2 = Route(target_node_id="dedup-test", route_type=RouteType.BRIDGE,
               hops=["bridge", "dedup-test"], first_host="2.2.2.2", first_port=3000)
    table.upsert(r2)
    assert len(table.for_target("dedup-test")) == 2


def test_route_table_evict_stale():
    table = RouteTable()
    r = Route(target_node_id="evict-test", route_type=RouteType.DIRECT,
              hops=["evict-test"], first_host="1.1.1.1", first_port=3000)
    r.last_used_at = time.time() - 10_000  # very old
    r.last_success_at = 0
    table.upsert(r)

    fresh = Route(target_node_id="fresh-test", route_type=RouteType.DIRECT,
                  hops=["fresh-test"], first_host="1.1.1.2", first_port=3000)
    fresh.last_success_at = time.time()
    table.upsert(fresh)

    n = table.evict_stale(max_age_sec=600.0)
    assert n >= 1
    assert table.for_target("fresh-test")
    assert not table.for_target("evict-test")


# ── Scoring ─────────────────────────────────────────────────────


def test_score_direct_outranks_multi_hop_when_both_healthy():
    direct = Route(
        target_node_id="rank-test",
        route_type=RouteType.DIRECT,
        hops=["rank-test"],
        first_host="10.0.0.1", first_port=3000,
    )
    multi = Route(
        target_node_id="rank-test",
        route_type=RouteType.MULTI_HOP_RELAY,
        hops=["a", "b", "c", "rank-test"],
        first_host="10.0.0.2", first_port=3000,
    )
    s_direct, _ = score_route(direct)
    s_multi, _ = score_route(multi)
    assert s_direct > s_multi
    assert s_direct > 0
    assert s_multi > 0


def test_score_zero_when_in_cooldown():
    r = Route(target_node_id="cool-test", route_type=RouteType.DIRECT,
              hops=["cool-test"], first_host="1.1.1.1", first_port=3000)
    r.failed_until = time.time() + 30
    s, breakdown = score_route(r)
    assert s == 0.0
    assert breakdown.get("rejected") == "in_cooldown"


def test_score_class_floor_caps_total():
    """Even with perfect inputs, the class floor caps the score."""
    rendezvous = Route(
        target_node_id="floor-test",
        route_type=RouteType.RENDEZVOUS_HINT,
        hops=["floor-test"],
        first_host="1.1.1.1", first_port=3000,
    )
    s, breakdown = score_route(rendezvous)
    # class_floor for RENDEZVOUS_HINT is 0.25 — final ≤ raw × 0.25
    assert s <= breakdown["raw"] * breakdown["class_floor"] + 1e-6
    assert breakdown["class_floor"] == 0.25


def test_score_breakdown_contains_all_factors():
    r = Route(target_node_id="break-test", route_type=RouteType.DIRECT,
              hops=["break-test"], first_host="1.1.1.1", first_port=3000)
    _, breakdown = score_route(r)
    expected_keys = {
        "class_floor", "latency", "bw", "loss", "hops",
        "age", "load", "security", "nat", "raw", "final",
    }
    # trust + phi may be absent for self-target routes; the rest must exist.
    assert expected_keys.issubset(breakdown.keys()), (
        f"missing: {expected_keys - set(breakdown.keys())}"
    )


# ── Strategy ────────────────────────────────────────────────────


def test_select_strategy_returns_lan_paths_in_default_state():
    eligible = select_strategy()
    assert RouteType.DIRECT in eligible
    assert RouteType.LAN_ALIAS in eligible
    assert RouteType.BRIDGE in eligible
    assert RouteType.SINGLE_HOP_RELAY in eligible
    assert RouteType.MULTI_HOP_RELAY in eligible
    assert RouteType.CACHED_FALLBACK in eligible


# ── Weights config sanity ───────────────────────────────────────


def test_route_weights_sum_close_to_one():
    total = sum(ROUTE_WEIGHTS.values())
    assert 0.95 <= total <= 1.05  # tolerance for tweak drift


def test_threshold_constants_are_reasonable():
    assert PHI_REJECT_THRESHOLD >= 4
    assert PHI_REJECT_THRESHOLD <= 16
    assert 0 < TRUST_REJECT_THRESHOLD < 0.5
    assert COOLDOWN_AFTER_FAIL_SEC >= 5


# ── snapshot() shape ────────────────────────────────────────────


def test_snapshot_shape():
    snap = snapshot()
    assert isinstance(snap, dict)
    assert "strategy" in snap
    assert "weights" in snap
    assert "thresholds" in snap
    assert "routes" in snap
    assert isinstance(snap["routes"], list)
    if snap["routes"]:
        first = snap["routes"][0]
        assert "target_node_id" in first
        assert "route_type" in first
        assert "score" in first
        assert "breakdown" in first


# ── End-to-end ranking on synthetic table ───────────────────────


def test_ranking_picks_highest_score_first():
    table = get_route_table()
    # Build three routes for the same target with predictable ordering.
    target_id = "ranking-test"
    r_direct = Route(target_node_id=target_id, route_type=RouteType.DIRECT,
                     hops=[target_id], first_host="10.0.0.10", first_port=3000)
    r_bridge = Route(target_node_id=target_id, route_type=RouteType.BRIDGE,
                     hops=["bridge-x", target_id],
                     first_host="10.0.0.11", first_port=3000)
    r_multi  = Route(target_node_id=target_id, route_type=RouteType.MULTI_HOP_RELAY,
                     hops=["a", "b", "c", target_id],
                     first_host="10.0.0.12", first_port=3000)
    for r in (r_multi, r_bridge, r_direct):  # insert in reverse order
        table.upsert(r)

    routes = table.for_target(target_id)
    scored = [(score_route(r)[0], r) for r in routes]
    scored = [(s, r) for s, r in scored if s > 0]
    scored.sort(key=lambda x: x[0], reverse=True)
    assert scored, "expected at least one valid route"
    # The DIRECT route must come first.
    assert scored[0][1].route_type == RouteType.DIRECT
