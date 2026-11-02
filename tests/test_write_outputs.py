"""Successful single writes expose the validated ID, never an invented batch ID."""

import pytest
import main
import transport
from test_runtime import envelope, fake
from test_commands import schema
from validation import Failure


@pytest.mark.parametrize(
    "name,extra",
    [
        ("upsert_document", {"document": {"text": "replacement"}}),
        ("update_document", {"doc": {"text": "updated"}}),
        ("delete_document", {}),
    ],
)
def test_single_write_dispatch_emits_validated_input_id_after_acceptance(monkeypatch, name, extra):
    calls = fake(
        monkeypatch, {"responseHeader": {"status": 0}, "document_id": "untrusted-provider-id"}
    )
    p = envelope(name, document_id="source/a?x#y", **extra)
    p["action"]["input"].pop("source_fields")
    result = main.handle(p)
    assert result["ok"], result
    assert result["result"] == {
        "applied": True,
        "result": "accepted",
        "document_id": "source/a?x#y",
    }
    assert len(calls) == 2


@pytest.mark.parametrize(
    "name,extra",
    [
        ("upsert_document", {"document": {"text": "replacement"}}),
        ("update_document", {"doc": {"text": "updated"}}),
        ("delete_document", {}),
    ],
)
@pytest.mark.parametrize("provider_result", [{}, {"responseHeader": {"status": 1}}])
def test_invalid_provider_write_never_emits_success_identity(
    monkeypatch, name, extra, provider_result
):
    fake(monkeypatch, provider_result)
    p = envelope(name, document_id="source/a", **extra)
    p["action"]["input"].pop("source_fields")
    result = main.handle(p)
    assert not result["ok"]
    assert result["error_code"] == "invalid_provider_response"
    assert "result" not in result and "document_id" not in result


def test_dispatched_write_failure_does_not_emit_identity(monkeypatch):
    def invoke(config, secrets, method, path, body=None, **kw):
        if "/schema" in path:
            return schema()
        raise Failure("timeout", "Operation deadline exceeded.", dispatched=True)

    monkeypatch.setattr(transport, "invoke", invoke)
    p = envelope("delete_document", document_id="source/a")
    p["action"]["input"].pop("source_fields")
    result = main.handle(p)
    assert not result["ok"] and result["external_effect_status"] == "timeout_unknown"
    assert result["definitely_no_external_effect"] is False
    assert "result" not in result and "document_id" not in result


def test_batch_acceptance_does_not_invent_a_singular_document_id(monkeypatch):
    fake(monkeypatch, {"responseHeader": {"status": 0}, "document_id": "last-id-is-not-the-batch"})
    p = envelope(
        "upsert_documents", documents=[{"id": "one", "text": "a"}, {"id": "two", "text": "b"}]
    )
    p["action"]["input"].pop("source_fields")
    result = main.handle(p)
    assert result["ok"], result
    assert result["result"] == {"applied": True, "result": "accepted"}
