#!/usr/bin/env python3
"""HTTPS-only downloader with DNS/peer pinning and bounded streaming.

The transport boundary is intentionally explicit: callers may inject a fake
transport, but every transport receives only addresses that passed the current
redirect hop's DNS policy.  The production transport below connects directly
to one of those addresses while retaining the original hostname for HTTP Host,
TLS SNI, and certificate verification.
"""

from __future__ import annotations

import http.client
import ipaddress
import math
import multiprocessing
import os
import re
import socket
import ssl
import tempfile
import time
import urllib.parse
from collections.abc import Callable, Collection, Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from types import TracebackType
from typing import Protocol


DEFAULT_CHUNK_SIZE = 64 * 1024
MAX_REDIRECTS = 3
REDIRECT_STATUSES = frozenset({301, 302, 303, 307, 308})
_CONTROL_OR_BACKSLASH = re.compile(r"[\x00-\x20\x7f\\]")
_VALID_PERCENT = re.compile(r"%(?![0-9A-Fa-f]{2})")


class DownloadError(RuntimeError):
    """Base class for a policy or transport failure."""


class URLPolicyError(DownloadError):
    """The request URL is outside the configured HTTPS policy."""


class DNSPolicyError(DownloadError):
    """DNS resolution did not produce only public unicast addresses."""


class TransportSecurityError(DownloadError):
    """The transport did not preserve the approved peer binding."""


class ResponsePolicyError(DownloadError):
    """The HTTP response violates framing, status, or size policy."""


class DownloadTimeout(DownloadError):
    """The configured connect, read, or total deadline expired."""


class FilePolicyError(DownloadError):
    """The output path is not a safe project-private destination."""


class ValidationError(DownloadError):
    """The caller's hostile-input validator rejected the partial file."""


class TransportError(DownloadError):
    """The HTTPS transport failed without a more specific policy error."""


Clock = Callable[[], float]
Resolver = Callable[[str, int], Sequence[str]]
DNSLookup = Callable[[str, int], Sequence[str]]
Validator = Callable[[Path], None]


@dataclass(frozen=True)
class TimeoutPolicy:
    connect: float = 5.0
    read: float = 15.0
    total: float = 60.0

    def validate(self) -> None:
        for value in (self.connect, self.read, self.total):
            if (
                isinstance(value, bool)
                or not isinstance(value, (int, float))
                or not math.isfinite(value)
                or value <= 0
            ):
                raise ValueError("timeouts must be finite positive numbers")


@dataclass(frozen=True)
class TransportRequest:
    """A single prevalidated HTTPS hop.

    ``approved_ips`` is the complete fail-closed DNS result for this hop.  A
    conforming transport MUST connect directly to one of these IPs and MUST use
    ``hostname`` (not the IP) for Host, SNI, and certificate verification.
    """

    hostname: str
    port: int
    target: str
    headers: Mapping[str, str]
    approved_ips: tuple[str, ...]
    connect_timeout: float
    read_timeout: float
    deadline: float
    clock: Clock


@dataclass
class TransportResponse:
    status_code: int
    headers: Sequence[tuple[str, str]]
    peer_ip: str
    body: Iterable[bytes] = ()
    close_callback: Callable[[], None] | None = None

    def iter_bytes(self, chunk_size: int = DEFAULT_CHUNK_SIZE) -> Iterable[bytes]:
        del chunk_size
        yield from self.body

    def close(self) -> None:
        if self.close_callback is not None:
            callback, self.close_callback = self.close_callback, None
            callback()

    def __enter__(self) -> TransportResponse:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        del exc_type, exc, traceback
        self.close()


class Transport(Protocol):
    def open(self, request: TransportRequest) -> TransportResponse: ...


@dataclass(frozen=True)
class DownloadResult:
    path: Path
    bytes_written: int
    redirects: int


@dataclass(frozen=True)
class _ValidatedURL:
    url: str
    hostname: str
    port: int
    target: str

    @property
    def loop_key(self) -> tuple[str, str, int, str]:
        return ("https", self.hostname, self.port, self.target)


def _system_dns_lookup(hostname: str, port: int) -> Sequence[str]:
    answers = socket.getaddrinfo(hostname, port, type=socket.SOCK_STREAM)
    return tuple(answer[4][0] for answer in answers)


def _resolver_process_entry(
    lookup: DNSLookup, hostname: str, port: int, sender: object
) -> None:
    try:
        result = tuple(lookup(hostname, port))
        sender.send(("ok", result))  # type: ignore[attr-defined]
    except BaseException:
        sender.send(("error", ()))  # type: ignore[attr-defined]
    finally:
        sender.close()  # type: ignore[attr-defined]


def system_resolver(
    hostname: str,
    port: int,
    *,
    timeout: float | None = None,
    lookup: DNSLookup | None = None,
) -> Sequence[str]:
    """Resolve in a disposable process when a real deadline is required."""
    resolver = lookup or _system_dns_lookup
    if timeout is None:
        return resolver(hostname, port)
    if not math.isfinite(timeout) or timeout <= 0:
        raise DownloadTimeout("DNS resolution timeout expired")
    context = multiprocessing.get_context("spawn")
    receiver, sender = context.Pipe(duplex=False)
    process = context.Process(
        target=_resolver_process_entry,
        args=(resolver, hostname, port, sender),
        name="auto-edit-dns-resolver",
    )
    try:
        process.start()
        sender.close()
        if not receiver.poll(timeout):
            raise DownloadTimeout("DNS resolution timeout expired")
        status, answers = receiver.recv()
        if status != "ok":
            raise DNSPolicyError("DNS resolution failed")
        return tuple(answers)
    except EOFError as exc:
        raise DNSPolicyError("DNS resolution failed") from exc
    finally:
        receiver.close()
        sender.close()
        if process.is_alive():
            process.terminate()
        process.join(timeout=1.0)
        if process.is_alive():
            process.kill()
            process.join()


def _canonical_hostname(value: str) -> str:
    if not isinstance(value, str) or not value or value != value.strip():
        raise ValueError("invalid hostname")
    if value.endswith(".") or _CONTROL_OR_BACKSLASH.search(value):
        raise ValueError("invalid hostname")
    try:
        return ipaddress.ip_address(value).compressed.lower()
    except ValueError:
        pass
    try:
        hostname = value.encode("idna").decode("ascii").lower()
    except UnicodeError as exc:
        raise ValueError("invalid hostname") from exc
    if not hostname or len(hostname) > 253:
        raise ValueError("invalid hostname")
    labels = hostname.split(".")
    if any(
        not label
        or len(label) > 63
        or label.startswith("-")
        or label.endswith("-")
        or not re.fullmatch(r"[a-z0-9-]+", label)
        for label in labels
    ):
        raise ValueError("invalid hostname")
    return hostname


def _canonical_allowlist(allowed_hosts: Collection[str]) -> frozenset[str]:
    if not allowed_hosts:
        raise ValueError("allowed_hosts must not be empty")
    try:
        result = frozenset(_canonical_hostname(item) for item in allowed_hosts)
    except (TypeError, ValueError) as exc:
        raise ValueError("allowed_hosts contains an invalid hostname") from exc
    if not result:
        raise ValueError("allowed_hosts must not be empty")
    return result


def _validate_url(url: str, allowed_hosts: frozenset[str]) -> _ValidatedURL:
    if not isinstance(url, str) or not url or _CONTROL_OR_BACKSLASH.search(url):
        raise URLPolicyError("URL is malformed")
    try:
        parsed = urllib.parse.urlsplit(url)
        username = parsed.username
        password = parsed.password
        raw_hostname = parsed.hostname
        port = parsed.port
    except (UnicodeError, ValueError) as exc:
        raise URLPolicyError("URL is malformed") from exc
    if parsed.scheme.lower() != "https":
        raise URLPolicyError("only HTTPS URLs are allowed")
    if username is not None or password is not None:
        raise URLPolicyError("URL credentials are forbidden")
    if raw_hostname is None:
        raise URLPolicyError("URL hostname is required")
    if parsed.netloc.endswith(":"):
        raise URLPolicyError("URL port is malformed")
    try:
        hostname = _canonical_hostname(raw_hostname)
    except ValueError as exc:
        raise URLPolicyError("URL hostname is malformed") from exc
    if hostname not in allowed_hosts:
        raise URLPolicyError("URL hostname is not allowlisted")
    port = 443 if port is None else port
    if port != 443:
        raise URLPolicyError("only HTTPS port 443 is allowed")
    if parsed.fragment:
        raise URLPolicyError("URL fragments are forbidden")
    if _VALID_PERCENT.search(parsed.path) or _VALID_PERCENT.search(parsed.query):
        raise URLPolicyError("URL contains invalid percent encoding")
    path = urllib.parse.quote(
        parsed.path or "/", safe="/%:@!$&'()*+,;=-._~"
    )
    query = urllib.parse.quote(
        parsed.query, safe="%/:@!$&'()*+,;=?-._~"
    )
    target = path + (("?" + query) if parsed.query else "")
    authority = f"[{hostname}]" if ":" in hostname else hostname
    normalized_url = urllib.parse.urlunsplit(("https", authority, path, query, ""))
    return _ValidatedURL(normalized_url, hostname, port, target)


def _is_public_unicast(address: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    return bool(
        address.is_global
        and not address.is_private
        and not address.is_loopback
        and not address.is_link_local
        and not address.is_multicast
        and not address.is_reserved
        and not address.is_unspecified
    )


def _resolve_approved(
    hostname: str,
    port: int,
    resolver: Resolver,
    timeout: float,
) -> tuple[str, ...]:
    try:
        if resolver is system_resolver:
            answers = tuple(system_resolver(hostname, port, timeout=timeout))
        else:
            answers = tuple(resolver(hostname, port))
    except DownloadError:
        raise
    except Exception as exc:
        raise DNSPolicyError("DNS resolution failed") from exc
    if not answers:
        raise DNSPolicyError("DNS returned no addresses")
    approved: list[str] = []
    seen: set[ipaddress.IPv4Address | ipaddress.IPv6Address] = set()
    for answer in answers:
        try:
            address = ipaddress.ip_address(answer)
        except (TypeError, ValueError) as exc:
            raise DNSPolicyError("DNS returned an invalid address") from exc
        if not _is_public_unicast(address):
            raise DNSPolicyError("DNS returned a non-public address")
        if address not in seen:
            approved.append(address.compressed)
            seen.add(address)
    return tuple(approved)


def _remaining(deadline: float, clock: Clock) -> float:
    now = clock()
    remaining = deadline - now
    if not math.isfinite(now) or not math.isfinite(deadline) or remaining <= 0:
        raise DownloadTimeout("total download timeout expired")
    return remaining


def _verified_peer(peer_ip: str, approved_ips: tuple[str, ...]) -> None:
    try:
        peer = ipaddress.ip_address(peer_ip)
        approved = {ipaddress.ip_address(value) for value in approved_ips}
    except (TypeError, ValueError) as exc:
        raise TransportSecurityError("transport returned an invalid peer IP") from exc
    if not _is_public_unicast(peer) or peer not in approved:
        raise TransportSecurityError("transport peer IP was not approved for this hop")


def _header_values(response: TransportResponse, name: str) -> list[str]:
    wanted = name.lower()
    values: list[str] = []
    for key, value in response.headers:
        if not isinstance(key, str) or not isinstance(value, str):
            raise ResponsePolicyError("response contains a malformed header")
        if "\r" in value or "\n" in value:
            raise ResponsePolicyError("response contains a malformed header")
        if key.lower() == wanted:
            values.append(value.strip())
    return values


def _validate_content_encoding(response: TransportResponse) -> None:
    values = _header_values(response, "Content-Encoding")
    tokens = [token.strip().lower() for value in values for token in value.split(",")]
    if any(not token or token != "identity" for token in tokens):
        raise ResponsePolicyError("compressed response bodies are forbidden")


def _content_length(response: TransportResponse) -> int | None:
    values = _header_values(response, "Content-Length")
    if not values:
        return None
    if len(values) != 1 or re.fullmatch(r"[0-9]+", values[0]) is None:
        raise ResponsePolicyError("Content-Length is invalid or ambiguous")
    try:
        return int(values[0])
    except ValueError as exc:
        raise ResponsePolicyError("Content-Length is invalid or ambiguous") from exc


def _safe_destination(project_dir: Path, destination: Path) -> tuple[Path, Path]:
    try:
        root = project_dir.resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise FilePolicyError("project directory does not exist") from exc
    if not root.is_dir():
        raise FilePolicyError("project path is not a directory")
    try:
        parent = destination.parent.resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise FilePolicyError("destination parent does not exist") from exc
    try:
        parent.relative_to(root)
    except ValueError as exc:
        raise FilePolicyError("destination escapes the project directory") from exc
    final = parent / destination.name
    try:
        if final.is_symlink():
            raise FilePolicyError("destination must not be a symlink")
    except OSError as exc:
        raise FilePolicyError("destination cannot be inspected safely") from exc
    return root, final


def _open_private_partial(destination: Path) -> tuple[int, Path]:
    try:
        descriptor, name = tempfile.mkstemp(
            prefix=f".{destination.name}.", suffix=".part", dir=destination.parent
        )
        os.fchmod(descriptor, 0o600)
    except OSError as exc:
        raise FilePolicyError("could not create project-private partial file") from exc
    part = Path(name)
    if part.is_symlink():
        os.close(descriptor)
        part.unlink(missing_ok=True)
        raise FilePolicyError("partial path must not be a symlink")
    return descriptor, part


def _unlink_partial(part: Path | None) -> None:
    if part is None:
        return
    try:
        part.unlink(missing_ok=True)
    except OSError as exc:
        raise FilePolicyError("could not remove partial file after failure") from exc


def download_https(
    url: str,
    destination: str | os.PathLike[str],
    *,
    project_dir: str | os.PathLike[str],
    allowed_hosts: Collection[str],
    max_bytes: int,
    validator: Validator,
    resolver: Resolver = system_resolver,
    transport: Transport | None = None,
    timeouts: TimeoutPolicy = TimeoutPolicy(),
    clock: Clock = time.monotonic,
    max_redirects: int = MAX_REDIRECTS,
    chunk_size: int = DEFAULT_CHUNK_SIZE,
) -> DownloadResult:
    """Download one validated asset and atomically publish it inside a project.

    The validator receives the completed ``.part`` path and must raise on any
    MIME, magic, decode, hash, license, or other caller-specific rejection.
    """

    timeouts.validate()
    if isinstance(max_bytes, bool) or not isinstance(max_bytes, int) or max_bytes <= 0:
        raise ValueError("max_bytes must be a positive integer")
    if (
        isinstance(max_redirects, bool)
        or not isinstance(max_redirects, int)
        or not 0 <= max_redirects <= MAX_REDIRECTS
    ):
        raise ValueError(f"max_redirects must be between 0 and {MAX_REDIRECTS}")
    if isinstance(chunk_size, bool) or not isinstance(chunk_size, int) or chunk_size <= 0:
        raise ValueError("chunk_size must be a positive integer")
    if not callable(validator) or not callable(resolver) or not callable(clock):
        raise TypeError("validator, resolver, and clock must be callable")

    allowlist = _canonical_allowlist(allowed_hosts)
    _root, final = _safe_destination(Path(project_dir), Path(destination))
    deadline = clock() + timeouts.total
    client = transport if transport is not None else PinnedHTTPSTransport()
    current_url = url
    visited: set[tuple[str, str, int, str]] = set()
    redirect_count = 0
    part: Path | None = None

    try:
        while True:
            _remaining(deadline, clock)
            validated_url = _validate_url(current_url, allowlist)
            if validated_url.loop_key in visited:
                raise ResponsePolicyError("redirect loop detected")
            visited.add(validated_url.loop_key)
            approved_ips = _resolve_approved(
                validated_url.hostname,
                validated_url.port,
                resolver,
                min(timeouts.connect, _remaining(deadline, clock)),
            )
            _remaining(deadline, clock)
            host_header = (
                f"[{validated_url.hostname}]"
                if ":" in validated_url.hostname
                else validated_url.hostname
            )
            request = TransportRequest(
                hostname=validated_url.hostname,
                port=validated_url.port,
                target=validated_url.target,
                headers={
                    "Host": host_header,
                    "Accept-Encoding": "identity",
                    "Connection": "close",
                    "User-Agent": "auto-edit-hardened-downloader/1",
                },
                approved_ips=approved_ips,
                connect_timeout=min(timeouts.connect, _remaining(deadline, clock)),
                read_timeout=timeouts.read,
                deadline=deadline,
                clock=clock,
            )
            try:
                response = client.open(request)
            except DownloadError:
                raise
            except Exception as exc:
                raise TransportError("HTTPS transport failed") from exc

            with response:
                _remaining(deadline, clock)
                _verified_peer(response.peer_ip, approved_ips)
                _validate_content_encoding(response)
                status = response.status_code
                if isinstance(status, bool) or not isinstance(status, int) or not 100 <= status <= 599:
                    raise ResponsePolicyError("HTTP status is invalid")

                if status in REDIRECT_STATUSES:
                    if redirect_count >= max_redirects:
                        raise ResponsePolicyError("redirect limit exceeded")
                    locations = _header_values(response, "Location")
                    if len(locations) != 1 or not locations[0]:
                        raise ResponsePolicyError("redirect Location is missing or ambiguous")
                    location = locations[0]
                    if _CONTROL_OR_BACKSLASH.search(location):
                        raise ResponsePolicyError("redirect Location is malformed")
                    current_url = urllib.parse.urljoin(validated_url.url, location)
                    redirect_count += 1
                    continue

                if not 200 <= status <= 299:
                    raise ResponsePolicyError("HTTP status is not successful")

                content_length = _content_length(response)
                transfer_values = _header_values(response, "Transfer-Encoding")
                if transfer_values:
                    tokens = [
                        token.strip().lower()
                        for value in transfer_values
                        for token in value.split(",")
                    ]
                    if tokens != ["chunked"] or content_length is not None:
                        raise ResponsePolicyError("Transfer-Encoding is unsupported or ambiguous")
                if content_length is not None and content_length > max_bytes:
                    raise ResponsePolicyError("Content-Length exceeds the byte limit")

                descriptor, part = _open_private_partial(final)
                bytes_written = 0
                with os.fdopen(descriptor, "wb") as output:
                    for chunk in response.iter_bytes(chunk_size):
                        _remaining(deadline, clock)
                        if not isinstance(chunk, bytes):
                            raise ResponsePolicyError("transport yielded a non-bytes body chunk")
                        bytes_written += len(chunk)
                        if bytes_written > max_bytes:
                            raise ResponsePolicyError("response body exceeds the byte limit")
                        if content_length is not None and bytes_written > content_length:
                            raise ResponsePolicyError("response exceeds declared Content-Length")
                        output.write(chunk)
                    _remaining(deadline, clock)
                    if content_length is not None and bytes_written != content_length:
                        raise ResponsePolicyError("response does not match declared Content-Length")
                    output.flush()
                    os.fsync(output.fileno())

                try:
                    validator(part)
                except DownloadError:
                    raise
                except Exception as exc:
                    raise ValidationError("download validator rejected the partial file") from exc
                os.replace(part, final)
                part = None
                return DownloadResult(final, bytes_written, redirect_count)
    finally:
        _unlink_partial(part)


class _PinnedHTTPSConnection(http.client.HTTPSConnection):
    def __init__(
        self,
        hostname: str,
        port: int,
        connect_ip: str,
        *,
        timeout: float,
        context: ssl.SSLContext,
        approved_ips: tuple[str, ...],
        read_timeout: float,
        deadline: float,
        clock: Clock,
    ) -> None:
        super().__init__(hostname, port, timeout=timeout, context=context)
        self._connect_ip = ipaddress.ip_address(connect_ip)
        self._approved_ips = tuple(ipaddress.ip_address(value) for value in approved_ips)
        self._read_timeout = read_timeout
        self._deadline = deadline
        self._clock = clock
        self.peer_ip = ""

    def connect(self) -> None:
        remaining = _remaining(self._deadline, self._clock)
        family = socket.AF_INET6 if self._connect_ip.version == 6 else socket.AF_INET
        address: tuple[object, ...]
        if self._connect_ip.version == 6:
            address = (self._connect_ip.compressed, self.port, 0, 0)
        else:
            address = (self._connect_ip.compressed, self.port)
        raw = socket.socket(family, socket.SOCK_STREAM)
        wrapped: ssl.SSLSocket | None = None
        try:
            raw.settimeout(min(float(self.timeout), remaining))
            raw.connect(address)
            raw.settimeout(
                min(float(self.timeout), _remaining(self._deadline, self._clock))
            )
            wrapped = self._context.wrap_socket(raw, server_hostname=self.host)
            peer = ipaddress.ip_address(wrapped.getpeername()[0])
            if peer not in self._approved_ips or not _is_public_unicast(peer):
                wrapped.close()
                raise TransportSecurityError(
                    "connected peer IP was not approved for this hop"
                )
            self.peer_ip = peer.compressed
            wrapped.settimeout(
                min(self._read_timeout, _remaining(self._deadline, self._clock))
            )
            self.sock = wrapped
        except Exception:
            if wrapped is not None:
                wrapped.close()
            else:
                raw.close()
            raise


class _HTTPBody:
    def __init__(
        self,
        response: http.client.HTTPResponse,
        connection: _PinnedHTTPSConnection,
        request: TransportRequest,
    ) -> None:
        self._response = response
        self._connection = connection
        self._request = request

    def __iter__(self) -> Iterable[bytes]:
        while True:
            remaining = _remaining(self._request.deadline, self._request.clock)
            if self._connection.sock is None:
                raise TransportError("HTTPS connection closed before body completed")
            self._connection.sock.settimeout(min(self._request.read_timeout, remaining))
            try:
                chunk = self._response.read(DEFAULT_CHUNK_SIZE)
            except (TimeoutError, socket.timeout) as exc:
                raise DownloadTimeout("response read timeout expired") from exc
            except (OSError, http.client.HTTPException) as exc:
                raise TransportError("response body read failed") from exc
            if not chunk:
                return
            yield chunk


class PinnedHTTPSTransport:
    """Production transport that never resolves or connects outside approved IPs."""

    def open(self, request: TransportRequest) -> TransportResponse:
        try:
            canonical_hostname = _canonical_hostname(request.hostname)
        except ValueError as exc:
            raise TransportSecurityError("transport hostname is invalid") from exc
        if canonical_hostname != request.hostname or request.port != 443 or not request.approved_ips:
            raise TransportSecurityError("transport request is not approved for production")
        expected_host = (
            f"[{canonical_hostname}]" if ":" in canonical_hostname else canonical_hostname
        )
        host_headers = [
            value for key, value in request.headers.items() if key.lower() == "host"
        ]
        encoding_headers = [
            value
            for key, value in request.headers.items()
            if key.lower() == "accept-encoding"
        ]
        if host_headers != [expected_host] or encoding_headers != ["identity"]:
            raise TransportSecurityError("transport Host or encoding policy is invalid")
        if (
            not isinstance(request.target, str)
            or not request.target.startswith("/")
            or _CONTROL_OR_BACKSLASH.search(request.target)
        ):
            raise TransportSecurityError("transport request target is invalid")
        for value in (
            request.connect_timeout,
            request.read_timeout,
            request.deadline,
        ):
            if (
                not isinstance(value, (int, float))
                or not math.isfinite(value)
                or value <= 0
            ):
                raise TransportSecurityError("transport timeout policy is invalid")
        if not callable(request.clock):
            raise TransportSecurityError("transport clock is invalid")
        approved: list[str] = []
        for value in request.approved_ips:
            try:
                address = ipaddress.ip_address(value)
            except ValueError as exc:
                raise TransportSecurityError("transport request contains an invalid IP") from exc
            if not _is_public_unicast(address):
                raise TransportSecurityError("transport request contains a non-public IP")
            approved.append(address.compressed)

        context = ssl.create_default_context()
        context.check_hostname = True
        context.verify_mode = ssl.CERT_REQUIRED
        last_error: BaseException | None = None
        for connect_ip in approved:
            connection: _PinnedHTTPSConnection | None = None
            try:
                connection = _PinnedHTTPSConnection(
                    request.hostname,
                    request.port,
                    connect_ip,
                    timeout=min(request.connect_timeout, _remaining(request.deadline, request.clock)),
                    context=context,
                    approved_ips=tuple(approved),
                    read_timeout=request.read_timeout,
                    deadline=request.deadline,
                    clock=request.clock,
                )
                connection.request(
                    "GET",
                    request.target,
                    headers=dict(request.headers),
                )
                if connection.sock is None:
                    raise TransportError("HTTPS connection closed before response headers")
                connection.sock.settimeout(
                    min(request.read_timeout, _remaining(request.deadline, request.clock))
                )
                raw_response = connection.getresponse()
                peer_ip = connection.peer_ip
                body = _HTTPBody(raw_response, connection, request)

                active_connection = connection

                def close() -> None:
                    try:
                        raw_response.close()
                    finally:
                        active_connection.close()

                return TransportResponse(
                    status_code=raw_response.status,
                    headers=tuple(raw_response.getheaders()),
                    peer_ip=peer_ip,
                    body=body,
                    close_callback=close,
                )
            except DownloadTimeout:
                if connection is not None:
                    connection.close()
                raise
            except (TimeoutError, socket.timeout) as exc:
                if connection is not None:
                    connection.close()
                last_error = exc
                if request.deadline - request.clock() <= 0:
                    raise DownloadTimeout("total download timeout expired") from exc
            except (OSError, ssl.SSLError, http.client.HTTPException) as exc:
                if connection is not None:
                    connection.close()
                last_error = exc
                if request.deadline - request.clock() <= 0:
                    raise DownloadTimeout("total download timeout expired") from exc
        if isinstance(last_error, TimeoutError):
            raise DownloadTimeout("HTTPS connect or response timeout expired") from last_error
        raise TransportError("HTTPS connection to approved addresses failed") from last_error


__all__ = [
    "DNSPolicyError",
    "DownloadError",
    "DownloadResult",
    "DownloadTimeout",
    "FilePolicyError",
    "PinnedHTTPSTransport",
    "ResponsePolicyError",
    "TimeoutPolicy",
    "Transport",
    "TransportError",
    "TransportRequest",
    "TransportResponse",
    "TransportSecurityError",
    "URLPolicyError",
    "ValidationError",
    "download_https",
    "system_resolver",
]
