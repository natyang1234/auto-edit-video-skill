from __future__ import annotations

import sys
import tempfile
import multiprocessing
import time
import unittest
from collections.abc import Iterable, Sequence
from pathlib import Path


SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))

from hardened_downloader import (  # noqa: E402
    DNSPolicyError,
    DownloadResult,
    DownloadTimeout,
    FilePolicyError,
    PinnedHTTPSTransport,
    ResponsePolicyError,
    TimeoutPolicy,
    TransportRequest,
    TransportResponse,
    TransportSecurityError,
    URLPolicyError,
    ValidationError,
    download_https,
    system_resolver,
)


PUBLIC_IP = "93.184.216.34"


def blocking_dns_lookup(host: str, port: int) -> Sequence[str]:
    del host, port
    time.sleep(60)
    return [PUBLIC_IP]


class FakeClock:
    def __init__(self) -> None:
        self.value = 0.0

    def __call__(self) -> float:
        return self.value

    def advance(self, seconds: float) -> None:
        self.value += seconds


class FakeTransport:
    def __init__(self, responses: Sequence[TransportResponse]) -> None:
        self._responses = list(responses)
        self.requests: list[TransportRequest] = []

    def open(self, request: TransportRequest) -> TransportResponse:
        self.requests.append(request)
        if not self._responses:
            raise AssertionError("unexpected transport request")
        return self._responses.pop(0)


def response(
    body: Iterable[bytes] = (),
    *,
    status: int = 200,
    headers: Sequence[tuple[str, str]] = (),
    peer_ip: str = PUBLIC_IP,
    close_callback=None,
) -> TransportResponse:
    return TransportResponse(
        status_code=status,
        headers=tuple(headers),
        peer_ip=peer_ip,
        body=body,
        close_callback=close_callback,
    )


class HardenedDownloaderTests(unittest.TestCase):
    def test_production_dns_deadline_terminates_worker_without_leaking_process(self) -> None:
        before = {child.pid for child in multiprocessing.active_children()}
        started = time.monotonic()

        with self.assertRaises(DownloadTimeout):
            system_resolver(
                "cdn.example.test",
                443,
                timeout=0.05,
                lookup=blocking_dns_lookup,
            )

        self.assertLess(time.monotonic() - started, 2.0)
        leaked = [
            child
            for child in multiprocessing.active_children()
            if child.pid not in before and child.is_alive()
        ]
        self.assertEqual(leaked, [])

    def run_download(
        self,
        project: Path,
        transport: FakeTransport,
        *,
        url: str = "https://cdn.example.test/asset.bin",
        allowed_hosts: set[str] | None = None,
        resolver=lambda host, port: [PUBLIC_IP],
        max_bytes: int = 100,
        validator=lambda path: None,
        clock=lambda: 0.0,
        max_redirects: int = 3,
    ) -> DownloadResult:
        destination = project / "assets" / "asset.bin"
        destination.parent.mkdir(parents=True, exist_ok=True)
        return download_https(
            url,
            destination,
            project_dir=project,
            allowed_hosts=allowed_hosts or {"cdn.example.test"},
            max_bytes=max_bytes,
            resolver=resolver,
            transport=transport,
            validator=validator,
            timeouts=TimeoutPolicy(connect=1, read=2, total=3),
            clock=clock,
            max_redirects=max_redirects,
        )

    def test_downloads_valid_https_asset_then_validates_and_atomically_publishes(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            project = Path(temporary) / "project"
            destination = project / "assets" / "asset.bin"
            destination.parent.mkdir(parents=True)
            transport = FakeTransport(
                [
                    response(
                        [b"safe", b" bytes"],
                        headers=[("Content-Length", "10")],
                    )
                ]
            )
            validated: list[Path] = []

            result = download_https(
                "https://cdn.example.test/asset.bin",
                destination,
                project_dir=project,
                allowed_hosts={"cdn.example.test"},
                max_bytes=100,
                resolver=lambda host, port: [PUBLIC_IP],
                transport=transport,
                validator=lambda path: validated.append(path),
                timeouts=TimeoutPolicy(connect=1, read=2, total=3),
            )

            self.assertEqual(result, DownloadResult(destination.resolve(), 10, 0))
            self.assertEqual(destination.read_bytes(), b"safe bytes")
            self.assertEqual(len(validated), 1)
            self.assertTrue(validated[0].name.endswith(".part"))
            self.assertFalse(validated[0].exists())
            self.assertEqual(list(destination.parent.glob("*.part")), [])
            self.assertEqual(transport.requests[0].approved_ips, (PUBLIC_IP,))
            self.assertEqual(transport.requests[0].hostname, "cdn.example.test")
            self.assertEqual(transport.requests[0].headers["Host"], "cdn.example.test")
            self.assertEqual(transport.requests[0].headers["Accept-Encoding"], "identity")

    def test_rejects_malformed_or_out_of_policy_urls_before_transport(self) -> None:
        cases = [
            "http://cdn.example.test/asset",
            "https:///asset",
            "https://cdn.example.test:444/asset",
            "https://sub.cdn.example.test/asset",
            "https://cdn.example.test./asset",
            "https://cdn.example.test/asset#fragment",
            "https://cdn.example.test\\@127.0.0.1/asset",
        ]
        with tempfile.TemporaryDirectory() as temporary:
            project = Path(temporary) / "project"
            for url in cases:
                with self.subTest(url=url):
                    transport = FakeTransport([])
                    with self.assertRaises(URLPolicyError):
                        self.run_download(project, transport, url=url)
                    self.assertEqual(transport.requests, [])

            secret_url = "https://alice:do-not-leak@cdn.example.test/asset"
            with self.assertRaises(URLPolicyError) as raised:
                self.run_download(project, FakeTransport([]), url=secret_url)
            self.assertNotIn("alice", str(raised.exception))
            self.assertNotIn("do-not-leak", str(raised.exception))

    def test_dns_fails_closed_for_any_non_public_or_invalid_answer(self) -> None:
        unsafe_answers = [
            [],
            ["not-an-ip"],
            ["127.0.0.1"],
            ["10.0.0.1"],
            ["169.254.169.254"],
            ["224.0.0.1"],
            ["192.0.2.1"],
            ["0.0.0.0"],
            ["::1"],
            ["fc00::1"],
            ["fe80::1"],
            ["ff02::1"],
            ["::"],
            [PUBLIC_IP, "10.0.0.1"],
        ]
        with tempfile.TemporaryDirectory() as temporary:
            project = Path(temporary) / "project"
            for answers in unsafe_answers:
                with self.subTest(answers=answers):
                    transport = FakeTransport([])
                    with self.assertRaises(DNSPolicyError):
                        self.run_download(
                            project,
                            transport,
                            resolver=lambda host, port, values=answers: values,
                        )
                    self.assertEqual(transport.requests, [])

    def test_revalidates_url_allowlist_dns_and_peer_on_every_redirect_hop(self) -> None:
        public_ips = {
            "api.example.test": "8.8.8.8",
            "cdn-a.example.test": "1.1.1.1",
            "cdn-b.example.test": "9.9.9.9",
            "cdn-c.example.test": PUBLIC_IP,
        }
        resolved: list[tuple[str, int]] = []

        def resolver(host: str, port: int) -> list[str]:
            resolved.append((host, port))
            return [public_ips[host]]

        transport = FakeTransport(
            [
                response(status=302, headers=[("Location", "https://cdn-a.example.test/a")], peer_ip="8.8.8.8"),
                response(status=307, headers=[("Location", "https://cdn-b.example.test/b")], peer_ip="1.1.1.1"),
                response(status=308, headers=[("Location", "https://cdn-c.example.test/final")], peer_ip="9.9.9.9"),
                response([b"ok"], headers=[("Content-Length", "2")]),
            ]
        )
        with tempfile.TemporaryDirectory() as temporary:
            project = Path(temporary) / "project"
            result = self.run_download(
                project,
                transport,
                url="https://api.example.test/start",
                allowed_hosts=set(public_ips),
                resolver=resolver,
            )

        self.assertEqual(result.redirects, 3)
        self.assertEqual(resolved, [(host, 443) for host in public_ips])
        self.assertEqual(
            [request.approved_ips for request in transport.requests],
            [(address,) for address in public_ips.values()],
        )

    def test_rejects_transport_peer_not_in_current_approved_dns_set(self) -> None:
        def body_must_not_be_read() -> Iterable[bytes]:
            raise AssertionError("body was read before peer verification")
            yield b"unreachable"

        with tempfile.TemporaryDirectory() as temporary:
            project = Path(temporary) / "project"
            for peer in ("8.8.8.8", "127.0.0.1"):
                with self.subTest(peer=peer):
                    with self.assertRaises(TransportSecurityError):
                        self.run_download(
                            project,
                            FakeTransport([response(body_must_not_be_read(), peer_ip=peer)]),
                        )
                    self.assertFalse((project / "assets" / "asset.bin").exists())

    def test_rejects_unsafe_malformed_looping_or_excessive_redirects(self) -> None:
        redirect_failures = [
            [response(status=302)],
            [response(status=302, headers=[("Location", "/a"), ("Location", "/b")])],
            [response(status=302, headers=[("Location", "%ZZ")])],
            [response(status=302, headers=[("Location", " ")])],
            [response(status=302, headers=[("Location", "https://evil.example/asset")])],
            [response(status=302, headers=[("Location", "http://cdn.example.test/asset")])],
            [response(status=302, headers=[("Location", "/asset.bin")])],
            [
                response(status=302, headers=[("Location", "/one")]),
                response(status=302, headers=[("Location", "/two")]),
                response(status=302, headers=[("Location", "/three")]),
                response(status=302, headers=[("Location", "/four")]),
            ],
        ]
        with tempfile.TemporaryDirectory() as temporary:
            project = Path(temporary) / "project"
            for responses in redirect_failures:
                with self.subTest(responses=len(responses), location=responses[0].headers):
                    with self.assertRaises((URLPolicyError, ResponsePolicyError)):
                        self.run_download(project, FakeTransport(responses))

            dns_answers = iter(([PUBLIC_IP], ["127.0.0.1"]))
            with self.assertRaises(DNSPolicyError):
                self.run_download(
                    project,
                    FakeTransport(
                        [response(status=302, headers=[("Location", "/next")])]
                    ),
                    resolver=lambda host, port: next(dns_answers),
                )

    def test_closes_every_response_across_redirect_and_failure(self) -> None:
        closed: list[str] = []
        transport = FakeTransport(
            [
                response(
                    status=302,
                    headers=[("Location", "/next")],
                    close_callback=lambda: closed.append("redirect"),
                ),
                response(status=503, close_callback=lambda: closed.append("failure")),
            ]
        )
        with tempfile.TemporaryDirectory() as temporary:
            with self.assertRaises(ResponsePolicyError):
                self.run_download(Path(temporary) / "project", transport)
        self.assertEqual(closed, ["redirect", "failure"])

    def test_rejects_hostile_status_encoding_and_framing_headers_before_writing(self) -> None:
        hostile_responses = [
            response(status=404),
            response(headers=[("Content-Encoding", "gzip")]),
            response(headers=[("Content-Encoding", "br")]),
            response(headers=[("Content-Length", "-1")]),
            response(headers=[("Content-Length", "1"), ("Content-Length", "1")]),
            response(headers=[("Content-Length", "101")]),
            response(headers=[("Transfer-Encoding", "gzip, chunked")]),
            response(headers=[("Transfer-Encoding", "chunked"), ("Content-Length", "1")]),
        ]
        with tempfile.TemporaryDirectory() as temporary:
            project = Path(temporary) / "project"
            for hostile in hostile_responses:
                with self.subTest(status=hostile.status_code, headers=hostile.headers):
                    with self.assertRaises(ResponsePolicyError):
                        self.run_download(project, FakeTransport([hostile]))
                    assets = project / "assets"
                    self.assertEqual(list(assets.glob("*.part")), [])
                    self.assertFalse((assets / "asset.bin").exists())

    def test_streaming_hard_cap_and_length_mismatch_clean_partial_and_preserve_final(self) -> None:
        hostile_responses = [
            response([b"a" * 60, b"b" * 50], headers=[("Transfer-Encoding", "chunked")]),
            response([b"a" * 100], headers=[("Content-Length", "99")]),
            response([b"short"], headers=[("Content-Length", "10")]),
        ]
        with tempfile.TemporaryDirectory() as temporary:
            project = Path(temporary) / "project"
            final = project / "assets" / "asset.bin"
            final.parent.mkdir(parents=True)
            final.write_bytes(b"previous-good-file")

            for hostile in hostile_responses:
                with self.subTest(headers=hostile.headers):
                    with self.assertRaises(ResponsePolicyError):
                        self.run_download(project, FakeTransport([hostile]))
                    self.assertEqual(final.read_bytes(), b"previous-good-file")
                    self.assertEqual(list(final.parent.glob("*.part")), [])

    def test_validator_rejection_cleans_private_partial_and_preserves_existing_final(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            project = Path(temporary) / "project"
            final = project / "assets" / "asset.bin"
            final.parent.mkdir(parents=True)
            final.write_bytes(b"previous-good-file")
            observed: list[tuple[bytes, int]] = []

            def reject(part: Path) -> None:
                observed.append((part.read_bytes(), part.stat().st_mode & 0o777))
                raise ValueError("invalid hostile payload")

            with self.assertRaises(ValidationError):
                self.run_download(
                    project,
                    FakeTransport(
                        [response([b"payload"], headers=[("Content-Length", "7")])]
                    ),
                    validator=reject,
                )

            self.assertEqual(observed, [(b"payload", 0o600)])
            self.assertEqual(final.read_bytes(), b"previous-good-file")
            self.assertEqual(list(final.parent.glob("*.part")), [])

    def test_rejects_output_path_traversal_and_symlinks(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            project = base / "project"
            assets = project / "assets"
            outside = base / "outside"
            assets.mkdir(parents=True)
            outside.mkdir()

            destinations = [outside / "escaped.bin"]
            linked_parent = project / "linked-assets"
            linked_parent.symlink_to(outside, target_is_directory=True)
            destinations.append(linked_parent / "escaped.bin")
            linked_final = assets / "linked.bin"
            linked_final.symlink_to(outside / "target.bin")
            destinations.append(linked_final)

            for destination in destinations:
                with self.subTest(destination=destination):
                    with self.assertRaises(FilePolicyError):
                        download_https(
                            "https://cdn.example.test/asset",
                            destination,
                            project_dir=project,
                            allowed_hosts={"cdn.example.test"},
                            max_bytes=100,
                            validator=lambda path: None,
                            resolver=lambda host, port: [PUBLIC_IP],
                            transport=FakeTransport([]),
                        )
            self.assertFalse((outside / "escaped.bin").exists())
            self.assertFalse((outside / "target.bin").exists())

    def test_injected_clock_enforces_total_deadline_without_sleep(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            project = Path(temporary) / "project"
            clock = FakeClock()

            def slow_body() -> Iterable[bytes]:
                yield b"first"
                clock.advance(4)
                yield b"late"

            transport = FakeTransport([response(slow_body())])
            with self.assertRaises(DownloadTimeout):
                self.run_download(project, transport, clock=clock)

            request = transport.requests[0]
            self.assertEqual(request.connect_timeout, 1)
            self.assertEqual(request.read_timeout, 2)
            self.assertEqual(request.deadline, 3)
            self.assertEqual(list((project / "assets").glob("*.part")), [])

    def test_production_transport_rejects_forged_binding_before_any_connection(self) -> None:
        base = dict(
            hostname="cdn.example.test",
            port=443,
            target="/asset",
            headers={"Host": "cdn.example.test", "Accept-Encoding": "identity"},
            approved_ips=(PUBLIC_IP,),
            connect_timeout=1.0,
            read_timeout=1.0,
            deadline=3.0,
            clock=lambda: 0.0,
        )
        forged = [
            {**base, "port": 444},
            {**base, "approved_ips": ("127.0.0.1",)},
            {**base, "headers": {"Host": "internal.example", "Accept-Encoding": "identity"}},
            {**base, "headers": {"Host": "cdn.example.test", "Accept-Encoding": "gzip"}},
            {**base, "target": "https://internal.example/asset"},
        ]
        transport = PinnedHTTPSTransport()
        for fields in forged:
            with self.subTest(fields=fields):
                with self.assertRaises(TransportSecurityError):
                    transport.open(TransportRequest(**fields))


if __name__ == "__main__":
    unittest.main()
