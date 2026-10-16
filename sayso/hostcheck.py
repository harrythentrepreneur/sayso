"""Host header check for the built-in web servers (DNS-rebinding guard).

A web page on any site can point its own domain at 127.0.0.1 ("DNS rebinding") and then
read the loopback dashboard, or post to the console, from the visitor's browser. The
browser still sends the attacker's name in the `Host` header, so each server answers only
to the names it was started on:

- bound to loopback (127.0.0.1, localhost, ::1): `127.0.0.1:<port>`, `localhost:<port>`,
  `[::1]:<port>` (and any other loopback IP literal, e.g. `[0:0:0:0:0:0:0:1]`, with that port);
- bound to another address: that address (or name) with the port;
- bound to every address (0.0.0.0 or ::): any IP literal with the port, plus the loopback
  names. An IP literal cannot be rebound, because rebinding needs a domain name;
- every name the operator passed with `--allowed-host` (for a reverse proxy or tunnel).
  `name` matches that name on any port; `name:port` matches only that port.

Anything else, a missing Host or two Host headers gets 421 Misdirected Request in plain
text before the request reaches any page, form or login. Names compare case-insensitively
and exactly: `localhost.` (trailing dot) is not `localhost`.
"""
from __future__ import annotations

import ipaddress
from http.server import BaseHTTPRequestHandler
from typing import Iterable

LOOPBACK_NAMES = ("127.0.0.1", "localhost", "[::1]")
REFUSAL = ("Misdirected request: this server does not answer to that Host name.\n"
           "Open it at the address it was started on, or start it with --allowed-host <name>.\n")


def _ip(name: str):
    """The IP address in a Host name (`[v6]` bracketed), or None for a domain name."""
    if name.startswith("[") and name.endswith("]"):
        name = name[1:-1]
    elif ":" in name:
        return None                       # a bare IPv6 address must be bracketed in Host
    try:
        return ipaddress.ip_address(name)
    except ValueError:
        return None


def split_host(value: str) -> tuple[str, str | None] | None:
    """`name[:port]` -> (lowercased name, port digits or None); None when malformed."""
    value = value.strip().lower()
    if not value or any(c.isspace() or c in "/\\@?#" for c in value):
        return None
    if value.startswith("["):
        end = value.find("]")
        if end < 0:
            return None
        name, rest = value[:end + 1], value[end + 1:]
        if rest and not rest.startswith(":"):
            return None
        port = rest[1:] if rest else None
    elif value.count(":") == 1:
        name, port = value.split(":")
    elif ":" in value:
        return None
    else:
        name, port = value, None
    if not name or (port is not None and not (port.isdigit() and port.isascii() and len(port) <= 5)):
        return None
    return name, port


def _bind_name(host: str) -> str:
    host = host.strip().lower()
    return f"[{host}]" if ":" in host and not host.startswith("[") else host


def _is_wildcard(host: str) -> bool:
    ip = _ip(_bind_name(host))
    return ip is not None and ip.is_unspecified


def _is_loopback(host: str) -> bool:
    name = _bind_name(host)
    if name == "localhost":
        return True
    ip = _ip(name)
    return ip is not None and ip.is_loopback


class HostPolicy:
    """Which Host headers a server bound to (host, port) answers to."""

    def __init__(self, bind_host: str, port: int, allowed: Iterable[str] | None = None):
        self.port = str(int(port))
        self.bind = bind_host
        self.wildcard = _is_wildcard(bind_host)
        self.loopback = _is_loopback(bind_host) or self.wildcard
        names: set[str] = set(LOOPBACK_NAMES) if self.loopback else set()
        if not self.wildcard:
            names.add(_bind_name(bind_host))
        self.names = frozenset(names)
        self.any_port: set[str] = set()
        self.exact: set[tuple[str, str]] = set()
        for entry in allowed or ():
            parts = split_host(entry)
            if parts is None:
                raise ValueError(f"--allowed-host {entry!r} is not a host name or host:port")
            name, port = parts
            if port is None:
                self.any_port.add(name)
            else:
                self.exact.add((name, str(int(port))))

    def allows(self, values: list[str] | None) -> bool:
        """values = every Host header the request carried (exactly one is required)."""
        if not values or len(values) != 1:
            return False
        parts = split_host(values[0])
        if parts is None:
            return False
        name, given = parts
        port = str(int(given)) if given is not None else "80"
        if name in self.any_port or (name, port) in self.exact:
            return True
        if port != self.port:
            return False
        if name in self.names:
            return True
        ip = _ip(name)                    # an IP literal cannot be rebound; compare it by value
        if ip is None:
            return False
        return self.wildcard or (self.loopback and ip.is_loopback) or ip == _ip(_bind_name(self.bind))


class HostCheckHandler(BaseHTTPRequestHandler):
    """Base request handler that refuses a wrong Host before any method handler runs.

    The server object carries `host_policy` (a HostPolicy). Checking inside parse_request
    covers GET, HEAD, POST and every other method, including the 405 refusals.
    """

    def parse_request(self) -> bool:  # noqa: D401 - BaseHTTPRequestHandler hook
        if not super().parse_request():
            return False
        policy = getattr(self.server, "host_policy", None)
        if policy is None or not policy.allows(self.headers.get_all("Host")):
            self.close_connection = True
            data = REFUSAL.encode("utf-8")
            self.send_response(421, "Misdirected Request")
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Connection", "close")
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(data)
            return False
        return True


def attach(server, bind_host: str, allowed: Iterable[str] | None = None):
    """Give a bound server its HostPolicy (port read after binding, so port 0 works)."""
    server.host_policy = HostPolicy(bind_host, server.server_address[1], allowed)
    return server
