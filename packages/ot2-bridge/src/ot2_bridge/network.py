"""Explicit IP bindings for the lab's private network; no wildcard or DNS targets."""

import ipaddress

PRIVATE_NETWORKS = tuple(ipaddress.ip_network(value) for value in (
    "10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "100.64.0.0/10", "127.0.0.0/8",
))
TAILSCALE_NETWORK = ipaddress.ip_network("100.64.0.0/10")


def is_tailscale_address(host):
    return ipaddress.IPv4Address(private_ipv4(host)) in TAILSCALE_NETWORK


def sila_tunnel_host(host):
    address = private_ipv4(host)
    if not (address.startswith("127.") or is_tailscale_address(address)):
        raise ValueError("SiLA requires a localhost SSH forward or an encrypted Tailscale address")
    return address


def private_ipv4(host):
    if host == "localhost":
        return "127.0.0.1"
    try:
        address = ipaddress.IPv4Address(host)
    except (ValueError, TypeError):
        raise ValueError("Use an explicit private IPv4 address for the lab connection") from None
    if not any(address in network for network in PRIVATE_NETWORKS):
        raise ValueError("Public, wildcard and link-local addresses are not supported")
    return str(address)
