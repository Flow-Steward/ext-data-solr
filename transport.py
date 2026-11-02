"""Linux worker bounds SDK DNS, TLS and all response I/O by one deadline."""

from __future__ import annotations
import base64
import ipaddress
import json
import multiprocessing
import os
import platform
import selectors
import time
from urllib.error import HTTPError
from urllib.request import Request
from flowsteward_extension_sdk import open_pinned_url, resolve_pinned_ips
from validation import Failure, require, bounded_json

MAX_RESPONSE = 1024 * 1024


def _request(config, secrets, method, path, body, deadline, dispatched):
    try:
        ips = tuple(resolve_pinned_ips(config["host"], port=config["port"], purpose="Solr API"))
        allow = os.environ.get("FS_ALLOW_PRIVATE_REMOTE_URLS", "").strip().lower() in {
            "1",
            "true",
            "yes",
            "on",
        }
        require(bool(ips), "No endpoint addresses.", code="network_blocked")
        public = [ipaddress.ip_address(ip).is_global for ip in ips]
        require(allow or all(public), "Endpoint blocked by host policy.", code="network_blocked")
        require(
            config["tls"] or not any(public),
            "Public endpoints require TLS.",
            code="network_blocked",
        )
    except (ValueError, OSError):
        raise Failure("network_blocked", "Endpoint blocked by host policy.") from None
    require(time.monotonic() < deadline, "Deadline exceeded.", code="timeout")
    headers = {"Accept": "application/json", "Content-Type": "application/json"}
    if "bearer_token" in secrets:
        headers["Authorization"] = "Bearer " + secrets["bearer_token"]
    else:
        headers["Authorization"] = (
            "Basic "
            + base64.b64encode((secrets["username"] + ":" + secrets["password"]).encode()).decode()
        )
    data = (
        None if body is None else json.dumps(body, allow_nan=False, separators=(",", ":")).encode()
    )
    require(data is None or len(data) <= 8 * 1024 * 1024, "Request byte budget exceeded.")
    request = Request(config["origin"] + path, data=data, headers=headers, method=method)
    dispatched.value = method == "POST" and "/update" in path
    try:
        with open_pinned_url(
            request,
            timeout_seconds=deadline - time.monotonic(),
            purpose="Solr API",
            pinned_ips=ips,
        ) as response:
            require(
                response.status in {200, 201},
                "Unexpected provider status.",
                code="invalid_provider_response",
            )
            raw = response.read(MAX_RESPONSE + 1)
            require(
                len(raw) <= MAX_RESPONSE,
                "Response byte budget exceeded.",
                code="response_too_large",
            )
        value = json.loads(raw, parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
        bounded_json(value, MAX_RESPONSE)
        return value
    except HTTPError as exc:
        code = {
            400: "invalid_payload",
            401: "authentication_failed",
            403: "authentication_failed",
            404: "not_found",
            409: "conflict",
            429: "rate_limited",
        }.get(exc.code, "provider_unavailable")
        raise Failure(code, f"Solr returned HTTP {exc.code}.") from None
    except (OSError, ValueError, TypeError, RecursionError):
        raise Failure("provider_unavailable", "Solr request failed.") from None


def _worker(output, config, secrets, method, path, body, deadline, dispatched):
    try:
        result = {
            "ok": True,
            "value": _request(config, secrets, method, path, body, deadline, dispatched),
        }
    except Failure as exc:
        result = {"ok": False, "code": exc.code, "message": str(exc)}
    except Exception:
        result = {
            "ok": False,
            "code": "provider_unavailable",
            "message": "Solr request failed.",
        }
    try:
        data = memoryview(json.dumps(result, allow_nan=False, separators=(",", ":")).encode())
        while data:
            count = os.write(output, data[:65536])
            data = data[count:]
    finally:
        os.close(output)


def invoke(config, secrets, method, path, body=None, *, timeout_seconds):
    require(
        platform.system() == "Linux",
        "Solr transport requires Linux.",
        code="platform_unsupported",
    )
    require(timeout_seconds > 0, "Deadline exceeded.", code="timeout")
    # Only validated internal builders call this boundary; reject endpoint injection defensively.
    require(
        method in {"GET", "POST", "PUT", "DELETE"}
        and isinstance(path, str)
        and path.startswith("/")
        and not path.startswith("//")
        and "#" not in path
        and "\\" not in path,
        "Invalid request endpoint.",
    )
    deadline = time.monotonic() + min(timeout_seconds, 30)
    context = multiprocessing.get_context("fork")
    state = context.RawValue("b", False)
    incoming, outgoing = os.pipe()
    worker = context.Process(
        target=_worker,
        args=(outgoing, config, secrets, method, path, body, deadline, state),
        daemon=True,
        name="fs-solr-request",
    )
    try:
        worker.start()
        os.close(outgoing)
        outgoing = -1
        os.set_blocking(incoming, False)
        raw = bytearray()
        with selectors.DefaultSelector() as selector:
            selector.register(incoming, selectors.EVENT_READ)
            while True:
                left = deadline - time.monotonic()
                require(
                    left > 0 and selector.select(left),
                    "Operation deadline exceeded.",
                    code="timeout",
                )
                try:
                    chunk = os.read(incoming, 65536)
                except BlockingIOError:
                    continue
                if not chunk:
                    break
                raw.extend(chunk)
                require(
                    len(raw) <= MAX_RESPONSE + 65536,
                    "Worker response budget exceeded.",
                    code="response_too_large",
                )
        require(time.monotonic() < deadline, "Operation deadline exceeded.", code="timeout")
        result = json.loads(raw)
        if not result.get("ok"):
            raise Failure(result["code"], result["message"])
        return result["value"]
    except Failure as exc:
        exc.dispatched = bool(state.value)
        raise
    except (OSError, EOFError, ValueError, KeyError, TypeError):
        raise Failure(
            "provider_unavailable", "Solr worker failed.", dispatched=bool(state.value)
        ) from None
    finally:
        if worker.pid is not None:
            if worker.is_alive():
                worker.terminate()
            worker.join(timeout=0.1)
            if worker.is_alive():
                worker.kill()
                worker.join()
            worker.close()
        os.close(incoming)
        if outgoing != -1:
            os.close(outgoing)
