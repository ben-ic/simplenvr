"""Tests for multi-network scan-target resolution (discovery.networks).

Pure and deterministic: interface enumeration is monkeypatched so no real
NICs, sockets, or DB are touched. The load-bearing guarantees under test:

  * Directly-connected interface subnets are always in scope (implicit
    consent) and de-duplicated across NICs.
  * Off-link networks are scanned ONLY when the user authorises them, and an
    authorised CIDR larger than /22 is rejected — the consent + safety gate.
  * Host iteration never exceeds MAX_SCAN_HOSTS and excludes already-seen IPs,
    network, and broadcast addresses.
"""
from __future__ import annotations

import ipaddress

from backend.discovery import networks as nw


def _fake_ifaces(entries):
    """Return a replacement for _iface_addrs() yielding the given tuples."""
    return lambda: list(entries)


def test_interface_subnets_always_in_scope(monkeypatch):
    monkeypatch.setattr(
        nw, "_iface_addrs", _fake_ifaces([("en0", "192.168.1.50", "255.255.255.0")])
    )
    nets = nw.interface_networks()
    assert [n.cidr for n in nets] == ["192.168.1.0/24"]
    assert nets[0].source == "interface"
    assert nets[0].iface == "en0"


def test_loopback_and_link_local_skipped(monkeypatch):
    monkeypatch.setattr(
        nw,
        "_iface_addrs",
        _fake_ifaces(
            [
                ("lo0", "127.0.0.1", "255.0.0.0"),
                ("en0", "169.254.10.2", "255.255.0.0"),  # link-local
                ("en1", "10.0.0.5", "255.255.255.0"),
            ]
        ),
    )
    assert [n.cidr for n in nw.interface_networks()] == ["10.0.0.0/24"]


def test_duplicate_subnet_across_nics_deduped(monkeypatch):
    monkeypatch.setattr(
        nw,
        "_iface_addrs",
        _fake_ifaces(
            [
                ("en0", "192.168.1.10", "255.255.255.0"),
                ("en1", "192.168.1.11", "255.255.255.0"),  # same subnet
            ]
        ),
    )
    assert [n.cidr for n in nw.interface_networks()] == ["192.168.1.0/24"]


def test_oversized_interface_mask_skipped(monkeypatch):
    # A NIC reporting a /8 should not trigger a 16M-host sweep.
    monkeypatch.setattr(
        nw, "_iface_addrs", _fake_ifaces([("en0", "10.0.0.5", "255.0.0.0")])
    )
    assert nw.interface_networks() == []


def test_authorized_cidr_accepted():
    accepted, rejected = nw.parse_authorized_networks(["10.20.0.0/24", "10.20.1.7"])
    assert rejected == []
    cidrs = {str(n) for n in accepted}
    assert cidrs == {"10.20.0.0/24", "10.20.1.7/32"}


def test_authorized_cidr_too_large_rejected():
    accepted, rejected = nw.parse_authorized_networks(["10.0.0.0/16"])
    assert accepted == []
    assert len(rejected) == 1
    assert "too large" in rejected[0][1]


def test_authorized_garbage_and_multicast_rejected():
    accepted, rejected = nw.parse_authorized_networks(
        ["not-an-ip", "239.255.255.250/32", ""]
    )
    assert accepted == []
    # empty string is silently skipped, so only 2 rejections
    reasons = {cidr: reason for cidr, reason in rejected}
    assert "not-an-ip" in reasons
    assert "239.255.255.250/32" in reasons


def test_resolve_plan_unions_interface_and_authorized(monkeypatch):
    monkeypatch.setattr(
        nw, "_iface_addrs", _fake_ifaces([("en0", "192.168.1.50", "255.255.255.0")])
    )
    plan = nw.resolve_scan_plan(["10.20.0.0/24"])
    sources = {sn.cidr: sn.source for sn in plan.networks}
    assert sources == {"192.168.1.0/24": "interface", "10.20.0.0/24": "authorized"}
    # /24 has 254 usable hosts each → 508 total.
    assert plan.total_hosts == 508
    assert plan.truncated is False


def test_authorized_overlapping_interface_not_double_counted(monkeypatch):
    monkeypatch.setattr(
        nw, "_iface_addrs", _fake_ifaces([("en0", "192.168.1.50", "255.255.255.0")])
    )
    # User re-lists their own subnet; it must not appear twice nor double the
    # host count.
    plan = nw.resolve_scan_plan(["192.168.1.0/24"])
    assert [sn.cidr for sn in plan.networks] == ["192.168.1.0/24"]
    assert plan.total_hosts == 254


def test_host_iteration_excludes_seen_and_network_broadcast(monkeypatch):
    monkeypatch.setattr(
        nw, "_iface_addrs", _fake_ifaces([("en0", "192.168.5.0", "255.255.255.0")])
    )
    plan = nw.resolve_scan_plan([])
    hosts = list(nw.iter_scan_hosts(plan, exclude={"192.168.5.10"}))
    assert "192.168.5.0" not in hosts      # network address
    assert "192.168.5.255" not in hosts    # broadcast
    assert "192.168.5.10" not in hosts     # excluded (already seen)
    assert "192.168.5.1" in hosts
    assert len(hosts) == 253               # 254 usable - 1 excluded


def test_scan_host_cap_enforced(monkeypatch):
    # 32 authorised /24s = ~8128 hosts, well over MAX_SCAN_HOSTS.
    monkeypatch.setattr(nw, "_iface_addrs", _fake_ifaces([]))
    cidrs = [f"10.{i}.0.0/24" for i in range(32)]
    plan = nw.resolve_scan_plan(cidrs)
    assert plan.truncated is True
    assert plan.total_hosts == nw.MAX_SCAN_HOSTS
    emitted = list(nw.iter_scan_hosts(plan))
    assert len(emitted) == nw.MAX_SCAN_HOSTS


def test_bare_host_yields_single_ip(monkeypatch):
    monkeypatch.setattr(nw, "_iface_addrs", _fake_ifaces([]))
    plan = nw.resolve_scan_plan(["10.20.0.7"])
    assert list(nw.iter_scan_hosts(plan)) == ["10.20.0.7"]
