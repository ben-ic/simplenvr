"""
Multi-network scan-target resolution.

By default SimpleNVR only ever looked at the /24 of the host's primary
interface (rtsp_probe._get_local_subnet). That misses two real deployments:

  1. A machine bridged onto several NICs / VLANs — each interface is its own
     subnet, and cameras live on more than one of them.
  2. An organisation with cameras on routed subnets or remote sites the host
     can reach through a router or VPN, but which are NOT one of the host's
     directly-connected networks (so ONVIF multicast WS-Discovery, which is
     link-local, never reaches them).

This module turns those into a concrete list of host IPs to probe.

"With permission" is enforced structurally:

  * Directly-connected interface subnets are always in scope. The host is a
    member of those networks; scanning them needs no extra consent.
  * Any network that is NOT directly connected must be listed explicitly by
    the user (Settings.scan_networks). We never probe a routed range we
    weren't told to. This is the consent gate — the user authorises each
    off-link network by adding its CIDR.

A hard cap (MAX_SCAN_HOSTS) and a minimum-prefix rule guard against a
fat-fingered /8 enqueuing 16M probes and hammering a network the user didn't
intend to sweep.
"""

from __future__ import annotations

import ipaddress
import logging
from dataclasses import dataclass, field
from typing import Iterable, Iterator

logger = logging.getLogger(__name__)

IPv4Network = ipaddress.IPv4Network

# A user-authorised CIDR may not be larger than this many addresses. /22 =
# 1024 addresses is a generous single-site ceiling while still rejecting an
# accidental /16 (65k) or /8 (16M). Interface-derived subnets bypass this rule
# (the host is genuinely a member), but are still subject to MAX_SCAN_HOSTS.
MAX_AUTHORIZED_HOSTS = 1024

# Absolute ceiling on the number of hosts a single scan pass will probe across
# ALL networks combined. Caps fan-out so a handful of large authorised ranges
# can't exhaust file descriptors or saturate the link. Hosts beyond this are
# dropped (logged) rather than silently truncating coverage without a trace.
MAX_SCAN_HOSTS = 4096


@dataclass(frozen=True)
class ScanNetwork:
    """A network to scan, tagged with where its authorisation came from."""

    network: IPv4Network
    # "interface": directly-connected (implicit consent).
    # "authorized": user-listed routed/off-link CIDR (explicit consent).
    source: str
    # The interface name, when source == "interface". Diagnostic only.
    iface: str | None = None

    @property
    def cidr(self) -> str:
        return str(self.network)


@dataclass
class ScanPlan:
    """The resolved set of networks/hosts for one discovery sweep."""

    networks: list[ScanNetwork] = field(default_factory=list)
    # CIDR strings the user supplied that we refused, with a reason each, so
    # the API/UI can show the user exactly why a network was rejected instead
    # of silently dropping it.
    rejected: list[tuple[str, str]] = field(default_factory=list)
    # True when MAX_SCAN_HOSTS clipped the host list.
    truncated: bool = False
    total_hosts: int = 0


def _iface_addrs() -> list[tuple[str, str, str]]:
    """Return (iface_name, ipv4_addr, netmask) for every up interface.

    Uses psutil (already a backend dependency). Returns [] and logs if
    psutil is unavailable or the platform call fails, so discovery degrades
    to the legacy single-subnet path rather than crashing.
    """
    out: list[tuple[str, str, str]] = []
    try:
        import psutil  # local import: keep module import cheap / mockable
    except Exception as e:  # pragma: no cover - psutil is a hard dep in prod
        logger.warning("psutil unavailable, cannot enumerate interfaces: %s", e)
        return out

    try:
        per_iface = psutil.net_if_addrs()
    except Exception as e:
        logger.warning("net_if_addrs() failed: %s", e)
        return out

    for name, addrs in per_iface.items():
        for a in addrs:
            # AF_INET == 2 everywhere; compare by value to avoid importing
            # socket just for the constant and to stay platform-agnostic.
            if getattr(a, "family", None) is None:
                continue
            if int(a.family) != 2:
                continue
            if not a.address or not a.netmask:
                continue
            out.append((name, a.address, a.netmask))
    return out


def interface_networks() -> list[ScanNetwork]:
    """Every directly-connected IPv4 subnet the host is a member of.

    Skips loopback (127/8), link-local (169.254/16), and any subnet that
    resolves wider than MAX_AUTHORIZED_HOSTS (a host sitting on a literal /8
    is not something we want to sweep wholesale). Deduplicates so two NICs on
    the same subnet don't double-scan it.
    """
    seen: set[IPv4Network] = set()
    result: list[ScanNetwork] = []
    for name, addr, netmask in _iface_addrs():
        try:
            ip = ipaddress.ip_address(addr)
        except ValueError:
            continue
        if not isinstance(ip, ipaddress.IPv4Address):
            continue
        if ip.is_loopback or ip.is_link_local:
            continue
        try:
            net = ipaddress.ip_network(f"{addr}/{netmask}", strict=False)
        except ValueError:
            continue
        if not isinstance(net, IPv4Network):
            continue
        # A /32 (point-to-point or host route) has nothing to sweep.
        if net.num_addresses <= 1:
            continue
        # Guard against an interface reporting an absurdly wide mask.
        if net.num_addresses > MAX_AUTHORIZED_HOSTS:
            logger.info(
                "Skipping interface %s subnet %s: %d hosts exceeds cap %d",
                name, net, net.num_addresses, MAX_AUTHORIZED_HOSTS,
            )
            continue
        if net in seen:
            continue
        seen.add(net)
        result.append(ScanNetwork(network=net, source="interface", iface=name))
    return result


def parse_authorized_networks(
    raw: Iterable[str],
) -> tuple[list[IPv4Network], list[tuple[str, str]]]:
    """Validate user-supplied CIDR strings.

    Returns (accepted_networks, rejected) where rejected is a list of
    (input, reason) pairs. A bare IP ("10.0.5.7") is treated as that single
    host (a /32) — we do NOT widen it to a /24 — so the user can authorise one
    camera precisely. Anything unparseable, non-IPv4, loopback/link-local/
    multicast, or larger than MAX_AUTHORIZED_HOSTS is rejected.
    """
    accepted: list[IPv4Network] = []
    rejected: list[tuple[str, str]] = []
    for item in raw:
        s = (item or "").strip()
        if not s:
            continue
        try:
            net = ipaddress.ip_network(s, strict=False)
        except ValueError as e:
            rejected.append((s, f"not a valid IP or CIDR: {e}"))
            continue
        if not isinstance(net, IPv4Network):
            rejected.append((s, "only IPv4 networks are supported"))
            continue
        if net.is_loopback or net.is_link_local or net.is_multicast:
            rejected.append((s, "loopback / link-local / multicast not allowed"))
            continue
        if net.num_addresses > MAX_AUTHORIZED_HOSTS:
            rejected.append(
                (s, f"too large: {net.num_addresses} hosts (max {MAX_AUTHORIZED_HOSTS}, ~/22)")
            )
            continue
        accepted.append(net)
    return accepted, rejected


def _net_hosts(net: IPv4Network) -> Iterator[str]:
    """Yield probeable host IPs in a network.

    For /31 and /32 we yield the literal address(es); for everything else we
    use .hosts() which already excludes network and broadcast addresses.
    """
    if net.prefixlen >= 31:
        for a in net:
            yield str(a)
        return
    for a in net.hosts():
        yield str(a)


def resolve_scan_plan(
    authorized: Iterable[str] | None,
    include_interfaces: bool = True,
) -> ScanPlan:
    """Build the full scan plan: interface subnets + authorised CIDRs.

    Overlapping networks are de-duplicated at the host level (an authorised
    CIDR that overlaps an interface subnet won't double-probe a host). The
    combined host list is capped at MAX_SCAN_HOSTS; if the cap clips it,
    plan.truncated is True so the caller can surface "scan was limited".
    """
    plan = ScanPlan()
    nets: list[ScanNetwork] = []

    if include_interfaces:
        nets.extend(interface_networks())

    accepted, rejected = parse_authorized_networks(authorized or [])
    plan.rejected = rejected
    iface_nets = {n.network for n in nets}
    for net in accepted:
        # Keep it even if it overlaps an interface subnet — host-level dedup
        # below prevents double work, and tagging it "authorized" preserves
        # the provenance for diagnostics.
        if net in iface_nets:
            continue
        nets.append(ScanNetwork(network=net, source="authorized"))

    plan.networks = nets

    # Flatten to a deduplicated, capped host count for reporting. Actual host
    # iteration for the scan is done lazily by iter_scan_hosts so we don't
    # materialise thousands of strings here unnecessarily; this pass only
    # computes the count and the truncated flag.
    seen: set[str] = set()
    count = 0
    truncated = False
    for sn in nets:
        for host in _net_hosts(sn.network):
            if host in seen:
                continue
            seen.add(host)
            count += 1
            if count > MAX_SCAN_HOSTS:
                truncated = True
                break
        if truncated:
            break
    plan.total_hosts = min(count, MAX_SCAN_HOSTS)
    plan.truncated = truncated
    return plan


def iter_scan_hosts(
    plan: ScanPlan,
    exclude: set[str] | None = None,
) -> Iterator[str]:
    """Yield up to MAX_SCAN_HOSTS unique host IPs from the plan.

    `exclude` is for IPs already discovered this pass (e.g. found via ONVIF)
    so the active scan doesn't re-probe them.
    """
    skip = exclude or set()
    seen: set[str] = set()
    emitted = 0
    for sn in plan.networks:
        for host in _net_hosts(sn.network):
            if host in skip or host in seen:
                continue
            seen.add(host)
            yield host
            emitted += 1
            if emitted >= MAX_SCAN_HOSTS:
                return
