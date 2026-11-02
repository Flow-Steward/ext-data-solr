"""DNS, slow I/O and response limits remain inside a killed worker."""

from email.message import Message
import io
import multiprocessing
import time
from urllib.error import HTTPError
import pytest
import transport
from validation import Failure, connection_values


def config():
    return connection_values(
        {
            "config": {"origin": "https://solr.example", "collections": ["chunks"]},
            "secrets": {"bearer_token": "dummy"},
        }
    )[0]


class Response(io.BytesIO):
    status = 200

    def __init__(self, raw=b"{}"):
        super().__init__(raw)

    def __enter__(self):
        return self

    def __exit__(self, *a):
        self.close()


def test_transport_pinned_headers(monkeypatch):
    monkeypatch.setattr(transport, "resolve_pinned_ips", lambda *a, **k: ["1.1.1.1", "8.8.8.8"])

    def open_url(request, **kw):
        assert request.full_url == "https://solr.example/solr/chunks/query"
        assert request.headers["Authorization"] == "Bearer dummy"
        assert kw["pinned_ips"] == ("1.1.1.1", "8.8.8.8")
        return Response(b'{"ok":true}')

    monkeypatch.setattr(transport, "open_pinned_url", open_url)
    assert transport.invoke(
        config(), {"bearer_token": "dummy"}, "POST", "/solr/chunks/query", {}, timeout_seconds=1
    ) == {"ok": True}


def test_dns_deadline_kills_worker(monkeypatch):
    def resolve(*a, **k):
        time.sleep(10)

    monkeypatch.setattr(transport, "resolve_pinned_ips", resolve)
    before = {p.pid for p in multiprocessing.active_children()}
    start = time.monotonic()
    with pytest.raises(Failure) as error:
        transport.invoke(
            config(),
            {"bearer_token": "dummy"},
            "GET",
            "/solr/chunks/schema?wt=json",
            timeout_seconds=0.15,
        )
    assert error.value.code == "timeout" and not error.value.dispatched
    assert time.monotonic() - start < 1
    assert {p.pid for p in multiprocessing.active_children()} == before


def test_slow_body_total_deadline(monkeypatch):
    monkeypatch.setattr(transport, "resolve_pinned_ips", lambda *a, **k: ["1.1.1.1"])

    class Slow(Response):
        def read(self, n=-1, /):
            time.sleep(10)
            return b"{}"

    monkeypatch.setattr(transport, "open_pinned_url", lambda *a, **k: Slow())
    start = time.monotonic()
    with pytest.raises(Failure) as error:
        transport.invoke(
            config(),
            {"bearer_token": "dummy"},
            "POST",
            "/solr/chunks/update/json/docs?commitWithin=1000",
            {},
            timeout_seconds=0.15,
        )
    assert error.value.code == "timeout" and error.value.dispatched
    assert time.monotonic() - start < 1


@pytest.mark.parametrize("ips", [["1.1.1.1", "127.0.0.1"], ["127.0.0.1"], []])
def test_all_addresses_policy(monkeypatch, ips):
    monkeypatch.delenv("FS_ALLOW_PRIVATE_REMOTE_URLS", raising=False)
    monkeypatch.setattr(transport, "resolve_pinned_ips", lambda *a, **k: ips)
    with pytest.raises(Failure) as error:
        transport.invoke(
            config(),
            {"bearer_token": "dummy"},
            "GET",
            "/solr/chunks/schema?wt=json",
            timeout_seconds=1,
        )
    assert error.value.code == "network_blocked"


@pytest.mark.parametrize(
    "status,code",
    [
        (301, "provider_unavailable"),
        (401, "authentication_failed"),
        (403, "authentication_failed"),
        (429, "rate_limited"),
    ],
)
def test_safe_http_errors(monkeypatch, status, code):
    monkeypatch.setattr(transport, "resolve_pinned_ips", lambda *a, **k: ["1.1.1.1"])

    def fail(*a, **k):
        raise HTTPError("secret", status, "private provider body", Message(), None)

    monkeypatch.setattr(transport, "open_pinned_url", fail)
    with pytest.raises(Failure) as error:
        transport.invoke(
            config(),
            {"bearer_token": "dummy"},
            "GET",
            "/solr/chunks/schema?wt=json",
            timeout_seconds=1,
        )
    assert error.value.code == code
    assert "secret" not in str(error.value) and "private" not in str(error.value)


def test_response_size_limit(monkeypatch):
    monkeypatch.setattr(transport, "resolve_pinned_ips", lambda *a, **k: ["1.1.1.1"])
    monkeypatch.setattr(
        transport, "open_pinned_url", lambda *a, **k: Response(b" " * (1024 * 1024 + 1))
    )
    with pytest.raises(Failure) as error:
        transport.invoke(
            config(),
            {"bearer_token": "dummy"},
            "GET",
            "/solr/chunks/schema?wt=json",
            timeout_seconds=1,
        )
    assert error.value.code == "response_too_large"


def test_real_sdk_redirect_never_forwards_authentication(monkeypatch):
    from http.server import BaseHTTPRequestHandler, HTTPServer
    import queue

    context = multiprocessing.get_context("fork")
    requests = context.Queue()

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            requests.put((self.path, self.headers.get("Authorization")))
            if self.path == "/stolen":
                self.send_response(200)
                self.end_headers()
                self.wfile.write(b"{}")
            else:
                self.send_response(302)
                self.send_header("Location", f"http://127.0.0.1:{server.server_port}/stolen")
                self.end_headers()

        def log_message(self, *args):
            pass

    server = HTTPServer(("127.0.0.1", 0), Handler)
    worker = context.Process(target=server.serve_forever, daemon=True)
    worker.start()
    monkeypatch.setenv("FS_ALLOW_PRIVATE_REMOTE_URLS", "true")
    c, s = connection_values(
        {
            "config": {
                "origin": f"http://127.0.0.1:{server.server_port}",
                "collections": ["chunks"],
            },
            "secrets": {"username": "user", "password": "temporary"},
        }
    )
    try:
        with pytest.raises(Failure):
            transport.invoke(c, s, "GET", "/solr/chunks/schema?wt=json", timeout_seconds=2)
        assert requests.get(timeout=1) == (
            "/solr/chunks/schema?wt=json",
            "Basic dXNlcjp0ZW1wb3Jhcnk=",
        )
        with pytest.raises(queue.Empty):
            requests.get(timeout=0.05)
    finally:
        worker.terminate()
        worker.join(timeout=1)
        worker.close()
        server.server_close()
        requests.close()
        requests.join_thread()
