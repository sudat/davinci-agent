"""Loopback-only Builder network posture validation (PRD 27).

Pure validation of the DECLARED endpoint: this module performs no socket
calls, no DNS resolution, and no connection attempts — only literal loopback
addresses (127.0.0.1 / ::1) and unix socket paths satisfy the loopback
posture. Names like ``localhost`` are refused because resolving them would
require the very socket layer this guard must not touch. Scope: the
single-host spike posture declared in the resolved configuration.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Literal

from pydantic import Field

from services.contracts.primitives import StrictModel

if TYPE_CHECKING:
    from services.config.models import ResolvedConfig

_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "::1"})
_MIN_PORT = 1
_MAX_PORT = 65535


class NetworkPolicyError(ValueError):
    """The declared builder endpoint violates the loopback-only posture."""


class ParsedEndpoint(StrictModel):
    kind: Literal["tcp", "unix"]
    host: str | None
    port: int | None = Field(default=None, ge=_MIN_PORT, le=_MAX_PORT, strict=True)
    socket_path: str | None = Field(default=None, min_length=1)


def checked_parse_endpoint(endpoint: str) -> ParsedEndpoint:
    """Parse a declared endpoint into a strict tcp/unix structure, or refuse."""

    text = endpoint.strip()
    if not text:
        raise NetworkPolicyError("builder endpoint is empty")
    unix = _parse_unix(text)
    if unix is not None:
        return unix
    authority = _extract_authority(text)
    if authority.startswith("["):
        return _parse_ipv6_authority(text, authority)
    return _parse_tcp_authority(text, authority)


def _parse_unix(text: str) -> ParsedEndpoint | None:
    if text.startswith("unix://"):
        socket_path = text[len("unix://") :]
        if not socket_path.startswith("/"):
            raise NetworkPolicyError(
                f"unix builder endpoint must be an absolute socket path: {text}"
            )
        return ParsedEndpoint(kind="unix", host=None, socket_path=socket_path)
    if text.startswith("/"):
        return ParsedEndpoint(kind="unix", host=None, socket_path=text)
    return None


def _extract_authority(text: str) -> str:
    rest = text
    if "://" in rest:
        scheme, _, rest = rest.partition("://")
        if scheme not in {"http", "https", "tcp"}:
            raise NetworkPolicyError(f"unsupported builder endpoint scheme '{scheme}'")
    if "@" in rest:
        raise NetworkPolicyError("builder endpoints must not embed userinfo credentials")
    authority = rest.split("/", 1)[0]
    if not authority:
        raise NetworkPolicyError(f"builder endpoint has no host: {text}")
    return authority


def _parse_tcp_authority(endpoint: str, authority: str) -> ParsedEndpoint:
    if ":" in authority:
        host, port_text = authority.rsplit(":", 1)
        if not host:
            raise NetworkPolicyError(f"builder endpoint has no host: {endpoint}")
        return ParsedEndpoint(kind="tcp", host=host, port=_checked_port(port_text, endpoint))
    return ParsedEndpoint(kind="tcp", host=authority, port=None)


def _parse_ipv6_authority(endpoint: str, authority: str) -> ParsedEndpoint:
    end = authority.find("]")
    if end == -1:
        raise NetworkPolicyError(f"unterminated IPv6 literal in builder endpoint: {endpoint}")
    host = authority[1:end]
    if not host:
        raise NetworkPolicyError(f"empty IPv6 literal in builder endpoint: {endpoint}")
    remainder = authority[end + 1 :]
    port: int | None = None
    if remainder.startswith(":"):
        port = _checked_port(remainder[1:], endpoint)
    elif remainder:
        raise NetworkPolicyError(f"garbage after IPv6 literal in builder endpoint: {endpoint}")
    return ParsedEndpoint(kind="tcp", host=host, port=port)


def _checked_port(port_text: str, endpoint: str) -> int:
    if not port_text.isdigit():
        raise NetworkPolicyError(f"builder endpoint port is not numeric: {endpoint}")
    port = int(port_text)
    if not _MIN_PORT <= port <= _MAX_PORT:
        raise NetworkPolicyError(f"builder endpoint port out of range: {endpoint}")
    return port


def assert_loopback_builder(resolved: ResolvedConfig) -> ParsedEndpoint:
    """Refuse any builder endpoint that is not literal loopback or a unix socket."""

    posture = resolved.network
    if posture.builder != "loopback":
        raise NetworkPolicyError(
            f"builder network posture {posture.builder!r} is not loopback-only"
        )
    parsed = checked_parse_endpoint(posture.builder_endpoint)
    if parsed.kind == "unix" or parsed.host in _LOOPBACK_HOSTS:
        return parsed
    raise NetworkPolicyError(
        f"builder endpoint '{posture.builder_endpoint}' is not loopback "
        "(127.0.0.1/::1) or a unix socket; refused"
    )


__all__ = [
    "NetworkPolicyError",
    "ParsedEndpoint",
    "assert_loopback_builder",
    "checked_parse_endpoint",
]
