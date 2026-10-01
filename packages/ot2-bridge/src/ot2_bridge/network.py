"""Explicit lab bindings and configured Tailscale origins; no wildcard targets."""

import ipaddress
import http.client
import json
import re
import socket
from urllib.parse import urlsplit

PRIVATE_NETWORKS = tuple(
    ipaddress.ip_network(value)
    for value in (
        "10.0.0.0/8",
        "172.16.0.0/12",
        "192.168.0.0/16",
        "100.64.0.0/10",
        "127.0.0.0/8",
    )
)
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


def hub_endpoint(value):
    """Validate an exact configured origin and resolve only into the tailnet.

    This is address validation, not proof of tailnet membership. Network policy,
    gateway credentials and (for HTTPS) normal certificate validation still apply.
    """
    url = urlsplit(value)
    if (
        url.scheme not in ("http", "https")
        or url.username
        or url.password
        or url.path not in ("", "/")
        or url.query
        or url.fragment
    ):
        raise ValueError("Use an http:// or https:// Hub origin without credentials or a path")
    hostname = url.hostname or ""
    if hostname.endswith(".ts.net") and re.fullmatch(r"[a-z0-9-]+(?:\.[a-z0-9-]+)+", hostname):
        addresses = {item[4][0] for item in socket.getaddrinfo(hostname, None, socket.AF_INET, socket.SOCK_STREAM)}
        if not addresses or not all(is_tailscale_address(address) for address in addresses):
            raise ValueError("Configured MagicDNS name must resolve only to Tailscale IPv4 addresses")
        address = sorted(addresses)[0]
    else:
        address = private_ipv4(hostname)
        hostname = address
        if not (address.startswith("127.") or is_tailscale_address(address)):
            raise ValueError("Hub requires localhost or the encrypted Tailscale network")
        if url.scheme == "https":
            raise ValueError("HTTPS requires the exact configured MagicDNS certificate name")
    port = url.port if url.port is not None else (443 if url.scheme == "https" else 80)
    if not 1 <= port <= 65535:
        raise ValueError("Invalid Hub port")
    authority = hostname if port == (443 if url.scheme == "https" else 80) else f"{hostname}:{port}"
    return f"{url.scheme}://{authority}", hostname, address, port


def hub_json(value, path, body, headers):
    """No proxy env, redirects or DNS re-resolution between validation/connect.

    TLS retains the configured hostname for certificate/SNI validation, while the
    underlying connection is pinned to the validated tailnet address.
    """
    origin, hostname, address, port = hub_endpoint(value)
    cls = http.client.HTTPSConnection if origin.startswith("https:") else http.client.HTTPConnection
    connection = cls(hostname, port, timeout=25)
    connection._create_connection = lambda target, timeout, source_address=None: socket.create_connection(
        (address, port), timeout, source_address
    )
    try:
        connection.request("POST", path, json.dumps(body, allow_nan=False), headers)
        response = connection.getresponse()
        raw = response.read(1_000_001)
        if response.status != 200 or len(raw) > 1_000_000:
            raise RuntimeError(f"Hub request rejected (HTTP {response.status}); no action will be replayed")
        return json.loads(raw)
    finally:
        connection.close()
