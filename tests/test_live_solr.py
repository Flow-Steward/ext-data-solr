"""Opt-in authenticated disposable Solr 9.11.0 integration, through Core gate only."""

from __future__ import annotations
import json
from pathlib import Path
import time
import pytest
import main
import transport
from test_runtime import envelope, dataset
from validation import connection_values, Failure

FIXTURE = Path("/tmp/fs-solr-live.json")
pytestmark = pytest.mark.skipif(
    not FIXTURE.is_file(), reason="Controlled Solr fixture not configured"
)


def conn():
    f = json.loads(FIXTURE.read_text())
    return {
        "connection_id": "conn",
        "connection_type": "solr",
        "config": {"origin": f["origin"], "collections": ["chunks"], "request_timeout_seconds": 10},
        "secrets": {"username": "fs", "password": f["password"]},
    }


def invoke(name, **args):
    p = envelope(name, **args)
    p["action"]["target"]["connection"] = conn()
    if name in {"upsert_document", "upsert_documents", "update_document", "delete_document"}:
        p["action"]["input"].pop("source_fields", None)
    value = main.handle(p)
    assert value["ok"], {"operation": name, "code": value.get("error_code")}
    return value["result"]


def admin(name="preview_query", arguments=None):
    p = dataset(name, arguments)
    c = conn()
    p["action"]["input"].update(connection_config=c["config"], secret_refs=c["secrets"])
    return p


@pytest.fixture(scope="module", autouse=True)
def ready():
    import os

    previous = os.environ.get("FS_ALLOW_PRIVATE_REMOTE_URLS")
    os.environ["FS_ALLOW_PRIVATE_REMOTE_URLS"] = "true"
    config, secrets = connection_values(conn())
    for _ in range(90):
        try:
            value = transport.invoke(
                config, secrets, "GET", "/solr/chunks/schema?wt=json", timeout_seconds=2
            )
            if value["schema"]["uniqueKey"] == "id":
                break
        except Exception:
            pass
        time.sleep(1)
    else:
        pytest.fail("Solr readiness deadline exceeded")
    # Prove absent credentials are rejected, then use a valid Basic connection.
    from urllib.request import Request
    from urllib.error import HTTPError
    from flowsteward_extension_sdk import open_pinned_url, resolve_pinned_ips

    with pytest.raises(HTTPError) as unauthenticated:
        with open_pinned_url(
            Request(config["origin"] + "/solr/chunks/schema?wt=json"),
            timeout_seconds=3,
            purpose="Controlled Solr fixture authentication check",
            pinned_ips=resolve_pinned_ips(
                config["host"], port=config["port"], purpose="Solr fixture"
            ),
        ):
            pass
    assert unauthenticated.value.code == 401
    with pytest.raises(Failure) as caught:
        transport.invoke(
            config,
            {"bearer_token": "deliberately-invalid"},
            "GET",
            "/solr/chunks/schema?wt=json",
            timeout_seconds=3,
        )
    assert caught.value.code == "authentication_failed"
    yield
    if previous is None:
        os.environ.pop("FS_ALLOW_PRIVATE_REMOTE_URLS", None)
    else:
        os.environ["FS_ALLOW_PRIVATE_REMOTE_URLS"] = previous


def visible(query, expected, *, source=None):
    deadline = time.monotonic() + 8
    while time.monotonic() < deadline:
        value = invoke("search", query=query, size=100)
        if value["count"] == expected and (
            source is None or value["documents"][0]["source"] == source
        ):
            return value
        time.sleep(0.1)
    pytest.fail("Bounded commitWithin visibility wait exceeded")


def test_actual_crud_atomic_preservation_get_and_fulltext():
    invoke(
        "upsert_document",
        document_id="live/a?b",
        document={
            "text": "Earth is round",
            "vector": [1, 0, 0],
            "title": "Manual",
            "flag": False,
            "n": 0,
            "document_id": "manual",
            "chunk_id": "intro",
            "source_uri": "https://example.org/manual",
        },
    )
    assert invoke("get_document", document_id="live/a?b")["documents"][0]["source"] == {
        "text": "Earth is round"
    }
    invoke("update_document", document_id="live/a?b", doc={"text": "Earth is approximately round"})
    got = invoke(
        "get_document", document_id="live/a?b", source_fields=["text", "title", "flag", "n"]
    )
    assert got["documents"][0]["source"] == {
        "text": "Earth is approximately round",
        "title": "Manual",
        "flag": False,
        "n": 0,
    }
    assert (
        visible({"match": {"text": "Earth"}}, 1)["documents"][0]["source"]["text"]
        == "Earth is approximately round"
    )
    invoke("delete_document", document_id="live/a?b")
    assert not invoke("get_document", document_id="live/a?b")["found"]
    visible({"match": {"text": "Earth"}}, 0)


def test_actual_vector_prefilter_returns_matching_topk_and_source_metadata():
    invoke(
        "upsert_documents",
        documents=[
            {"id": "near-excluded", "text": "excluded", "vector": [1, 0, 0], "flag": True},
            {
                "id": "less-near",
                "text": "included one",
                "vector": [0.9, 0.1, 0],
                "flag": False,
                "source_uri": "https://example.org/one",
            },
            {
                "id": "farther",
                "text": "included two",
                "vector": [0, 1, 0],
                "flag": False,
                "source_uri": "https://example.org/two",
            },
        ],
    )
    visible({"match_all": {}}, 3)
    result = invoke(
        "knn_search",
        field="vector",
        query_vector=[1, 0, 0],
        k=2,
        filter={"term": {"flag": False}},
        source_fields=["text", "source_uri"],
    )
    assert result["count"] == 2
    assert [x["document_id"] for x in result["documents"]] == ["less-near", "farther"]
    assert all(type(x["score"]) in {int, float} for x in result["documents"])
    assert result["documents"][0]["source"]["source_uri"] == "https://example.org/one"
    p = admin(
        arguments={
            "source_fields": ["text"],
            "knn": {
                "field": "vector",
                "query_vector": {"$param": "v"},
                "k": 2,
                "filter": {"term": {"flag": False}},
            },
        }
    )
    p["action"]["input"]["dataset"]["resource_scope_jsonb"]["resources"][0]["columns"] = [
        "text",
        "flag",
        "vector",
    ]
    p["action"]["input"]["query_template"]["param_schema_jsonb"] = {
        "properties": {"v": {"type": "array"}}
    }
    p["action"]["input"]["params"] = {"v": [1, 0, 0]}
    value = main.handle(p)
    assert value["ok"], value.get("error_code")
    assert value["result"]["rows"] == [{"text": "included one"}, {"text": "included two"}]
    p["action"]["input"]["params"] = {"v": [1, 0]}
    assert main.handle(p)["error_code"] == "unsupported_vector_mapping"
    for docid in ("near-excluded", "less-near", "farther"):
        invoke("delete_document", document_id=docid)
    visible({"match_all": {}}, 0)


def test_actual_dataset_parameters_null_missing_budgets_and_guards():
    invoke(
        "upsert_document",
        document_id="typed",
        document={"text": "typed", "value": False, "flag": False, "n": 0},
    )
    visible({"term": {"flag": False}}, 1)
    p = admin(
        arguments={"source_fields": ["value", "n"], "query": {"term": {"flag": {"$param": "flag"}}}}
    )
    p["action"]["input"]["dataset"]["resource_scope_jsonb"]["resources"][0]["columns"] = [
        "value",
        "n",
        "flag",
    ]
    p["action"]["input"]["query_template"]["param_schema_jsonb"] = {
        "properties": {"flag": {"type": "boolean"}}
    }
    p["action"]["input"]["params"] = {"flag": False}
    value = main.handle(p)
    assert value["ok"], value.get("error_code")
    assert value["result"]["rows"] == [{"value": False, "n": 0}]
    p["action"]["action_id"] = "infer_output_schema"
    assert main.handle(p)["result"]["output_schema"]["properties"] == {"value": {}, "n": {}}
    invoke("update_document", document_id="typed", doc={"value": None})
    assert invoke("get_document", document_id="typed", source_fields=["value", "n"])["documents"][
        0
    ]["source"] == {"n": 0}
    for name in ("test_connection", "introspect_schema"):
        assert main.handle(admin(name))["ok"]
    invoke("update_document", document_id="typed", doc={"text": "x" * 2000})
    visible({"match_all": {}}, 1, source={"text": "x" * 2000})
    p = admin(arguments={"source_fields": ["text"]})
    p["action"]["input"]["limits"] = {"max_bytes": 1024}
    assert not main.handle(p)["ok"]
    p = admin(arguments={"source_fields": ["text"], "query": {"term": {"secret": "hidden"}}})
    assert not main.handle(p)["ok"]
    p = envelope("delete_document", document_id="typed")
    p["action"]["input"].pop("source_fields")
    p["action"]["target"]["connection"] = conn()
    p["runtime_context"]["test_mode"] = True
    assert main.handle(p)["error_code"] == "test_mode_suppressed"
    p["runtime_context"]["test_mode"] = False
    p["runtime_context"].pop("external_effect_id")
    assert main.handle(p)["error_code"] == "effect_claim_required"
    assert invoke("get_document", document_id="typed")["found"]
    invoke("delete_document", document_id="typed")
    visible({"match_all": {}}, 0)
