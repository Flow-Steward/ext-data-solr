"""Connection, response and Dataset scope boundaries."""

import pytest
import main
from test_runtime import dataset, envelope, fake
from test_commands import schema


@pytest.mark.parametrize(
    "origin",
    [
        "https://user:pass@solr.example",
        "https://solr.example/solr",
        "https://solr.example?x=1",
        "https://solr.example#x",
        "file:///tmp/a",
        "https://solr.example:bad",
        "https://solr.example\\evil",
        "https://@solr.example",
        "https://solr.example?",
        "https://solr.example#",
        "https://solr.example:0",
    ],
)
def test_origin_injection(monkeypatch, origin):
    calls = fake(monkeypatch)
    p = envelope()
    p["action"]["target"]["connection"]["config"]["origin"] = origin
    assert main.handle(p)["error_code"] == "invalid_connection"
    assert not calls


@pytest.mark.parametrize(
    "collections",
    [
        [],
        ["*"],
        ["chunks/../x"],
        ["chunks,other"],
        ["remote:chunks"],
        ["chunks", "chunks"],
        ["x"] * 33,
    ],
)
def test_explicit_collection_names(monkeypatch, collections):
    calls = fake(monkeypatch)
    p = envelope()
    p["action"]["target"]["connection"]["config"]["collections"] = collections
    assert main.handle(p)["error_code"] == "invalid_connection"
    assert not calls


@pytest.mark.parametrize(
    "secrets",
    [
        {},
        {"bearer_token": "a", "username": "u", "password": "p"},
        {"username": "u"},
        {"bearer_token": "a\r\nX: y"},
        {"username": "u", "password": ""},
        {"username": "u:admin", "password": "p"},
    ],
)
def test_auth_modes(monkeypatch, secrets):
    calls = fake(monkeypatch)
    p = envelope()
    p["action"]["target"]["connection"]["secrets"] = secrets
    assert main.handle(p)["error_code"] == "invalid_connection"
    assert not calls


def test_introspection_stored_concrete_fields_only_honest_dynamic_fields(monkeypatch):
    import transport

    data = schema()
    data["schema"]["fields"].extend(
        [
            {"name": "secret", "type": "string", "stored": False},
            {"name": "unsafe.field", "type": "string", "stored": True},
        ]
    )
    calls = []

    def invoke(config, secrets, method, path, body=None, **kwargs):
        calls.append(path)
        return data

    monkeypatch.setattr(transport, "invoke", invoke)
    p = dataset("introspect_schema")
    p["action"]["input"].pop("dataset")
    value = main.handle(p)
    assert value["ok"], value
    columns = value["result"]["resources"][0]["columns"]
    assert {c["name"] for c in columns} == {"id", "text", "title", "value", "vector"}
    assert all(c["output_schema"] == {} for c in columns)
    assert value["result"]["resources"][0]["metadata"]["dynamic_fields_omitted"] is True
    assert calls == ["/solr/chunks/schema?wt=json"]


def test_read_only_dataset_and_no_resource_override(monkeypatch):
    calls = fake(monkeypatch)
    p = dataset()
    definition = p["action"]["input"]["query_template"]["query_definition_jsonb"]
    definition["operation"] = "delete_document"
    assert not main.handle(p)["ok"] and not calls
    definition["operation"] = "search"
    definition["arguments"]["collection"] = "foreign"
    assert not main.handle(p)["ok"] and not calls


def test_unknown_write_outcome_never_claims_not_applied(monkeypatch):
    from validation import Failure
    import transport

    def invoke(config, secrets, method, path, body=None, **kwargs):
        if "/schema" in path:
            return schema()
        raise Failure("timeout", "Operation deadline exceeded.", dispatched=True)

    monkeypatch.setattr(transport, "invoke", invoke)
    p = envelope("delete_document", document_id="one")
    p["action"]["input"].pop("source_fields")
    result = main.handle(p)
    assert not result["ok"] and result["retryable"] is False
    assert result["definitely_no_external_effect"] is False
    assert result["external_effect_status"] == "timeout_unknown"


def test_response_row_limit_and_bad_status(monkeypatch):
    fake(
        monkeypatch,
        {
            "responseHeader": {"status": 0},
            "response": {"docs": [{"value": i, "__fs_id": str(i)} for i in range(3)]},
        },
    )
    p = dataset()
    p["action"]["input"]["limits"] = {"max_preview_rows": 2}
    assert not main.handle(p)["ok"]
    fake(monkeypatch, {"responseHeader": {"status": 1}, "response": {"docs": []}})
    assert not main.handle(dataset())["ok"]


def test_all_schema_requests_share_dataset_deadline(monkeypatch):
    import transport
    import time

    seen = []

    def invoke(config, secrets, method, path, body=None, **kwargs):
        seen.append(kwargs["timeout_seconds"])
        time.sleep(0.02)
        if "/schema" in path:
            return schema()
        return {"responseHeader": {"status": 0}, "response": {"docs": []}}

    monkeypatch.setattr(transport, "invoke", invoke)
    result = main.handle(dataset())
    assert result["ok"], result
    assert len(seen) == 2 and seen[1] < seen[0]
