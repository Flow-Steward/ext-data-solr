"""Solr SDK v2 host entrypoint."""

from __future__ import annotations
import json
import sys
import time
from flowsteward_extension_sdk import runtime_context_from_payload
import rag
import datasets
import transport
from commands import WRITES, prepare, documents, parse_schema
from validation import Failure, require, connection_values, bounded_json


def handle(payload):
    write = False
    dispatched = False
    try:
        runtime_context_from_payload(payload)
        context = payload["runtime_context"]
        require(type(context.get("test_mode", False)) is bool, "Invalid test mode.")
        if payload.get("mode") == "query":
            name = (payload.get("query") or {}).get("query_id")
            require(name in {"supported_connectors", "runtime_health"}, "Unknown query.")
            return {
                "ok": True,
                "result": {"connectors": datasets.catalog()}
                if name == "supported_connectors"
                else {"status": "healthy", "extension_id": "flowsteward.solr"},
                "errors": [],
            }
        require(payload.get("mode") == "action", "Action required.")
        a = payload.get("action") or {}
        name = a.get("action_id")
        args = a.get("input") or {}
        require(isinstance(args, dict), "Object input required.")
        bounded_json(args)
        if name in rag.ACTIONS:
            result = rag.handle(name, args)
        elif name in datasets.ACTIONS:
            result = datasets.handle(name, args, transport.invoke)
        else:
            conn = (a.get("target") or {}).get("connection") or {}
            require(
                isinstance(conn, dict)
                and isinstance(args.get("connection_ref"), str)
                and args["connection_ref"]
                and conn.get("connection_id") == args["connection_ref"]
                and conn.get("connection_type") == "solr",
                "Matching Solr connection required.",
                code="invalid_connection",
            )
            for key in ("account_id", "project_id"):
                require(
                    isinstance(context.get(key), str) and context[key].strip(),
                    "Account and project scope required.",
                    code="invalid_connection",
                )
            config, secrets = connection_values(conn)
            method, path, body = prepare(name, args, config)
            write = name in WRITES
            if write:
                require(
                    not context.get("test_mode"),
                    "Solr writes suppressed in test mode.",
                    code="test_mode_suppressed",
                )
                claim = context.get("external_effect_id")
                require(
                    isinstance(claim, str) and claim.strip() and len(claim) <= 512,
                    "Durable effect claim required.",
                    code="effect_claim_required",
                )
            deadline = time.monotonic() + config["request_timeout_seconds"]
            schema = parse_schema(
                transport.invoke(
                    config,
                    secrets,
                    "GET",
                    "/solr/" + args["collection"] + "/schema?wt=json",
                    None,
                    timeout_seconds=deadline - time.monotonic(),
                )
            )
            method, path, body = prepare(name, args, config, schema=schema)
            try:
                value = transport.invoke(
                    config, secrets, method, path, body, timeout_seconds=deadline - time.monotonic()
                )
                dispatched = write
            except Failure as exc:
                dispatched = exc.dispatched
                raise
            result = documents(
                name, value, args.get("source_fields", []), args.get("size", args.get("k", 100))
            )
            if name in {"upsert_document", "update_document", "delete_document"}:
                result["document_id"] = args["document_id"]
        bounded_json(result, 480 * 1024)
        response = {"ok": True, "result": result, "errors": []}
        if name in datasets.ACTIONS:
            budget = args.get("limits", {}).get("max_bytes", 400 * 1024)
            bounded_json(response, budget)
        return response
    except Exception as exc:
        code = exc.code if isinstance(exc, Failure) else "invalid_payload"
        message = (
            str(exc)
            if isinstance(exc, Failure)
            else "Invalid bounded envelope or provider response."
        )
        uncertain = write and dispatched
        return {
            "ok": False,
            "error_code": code,
            "error": message,
            "errors": [{"code": code, "message": message}],
            "retryable": False,
            "definitely_no_external_effect": not uncertain,
            "external_effect_status": "timeout_unknown" if uncertain else "not_applied",
        }


def main():
    try:
        raw = sys.stdin.buffer.read(9 * 1024 * 1024 + 1)
        require(len(raw) <= 9 * 1024 * 1024, "Input byte budget exceeded.")
        result = handle(json.loads(raw))
    except Exception:
        result = {
            "ok": False,
            "error_code": "invalid_payload",
            "error": "Invalid bounded JSON input.",
            "errors": [],
        }
    print(json.dumps(result, allow_nan=False))
    return 0 if result["ok"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
