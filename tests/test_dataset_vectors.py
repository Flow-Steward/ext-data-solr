"""Native JSON uses only scoped fields, supplied vectors and tested prefilters."""

import main
import pytest
from test_runtime import dataset, fake


def vector_request(name="preview_query"):
    p = dataset(
        name,
        {
            "source_fields": ["text"],
            "knn": {
                "field": "vector",
                "query_vector": [1, 0, 0],
                "k": 2,
                "filter": {"term": {"value": False}},
            },
        },
    )
    p["action"]["input"]["dataset"]["resource_scope_jsonb"]["resources"][0]["columns"].append(
        "vector"
    )
    return p


def test_vector_dataset_prefilter(monkeypatch):
    calls = fake(monkeypatch)
    result = main.handle(vector_request())
    assert result["ok"], result
    assert result["result"]["rows"] == [{"text": "hello"}]
    body = calls[1][2]
    assert body["query"] == "{!knn f=vector topK=2}[1,0,0]"
    assert body["filter"] == [{"term": {"f": "value", "query": "false"}}]


@pytest.mark.parametrize(
    "part", ["field", "filter", "projection", "override", "query", "sort", "size"]
)
def test_vector_scope_and_closed_controls_before_network(monkeypatch, part):
    calls = fake(monkeypatch)
    p = vector_request()
    a = p["action"]["input"]["query_template"]["query_definition_jsonb"]["arguments"]
    if part == "field":
        a["knn"]["field"] = "secret"
    elif part == "filter":
        a["knn"]["filter"] = {"term": {"secret": "hidden"}}
    elif part == "projection":
        a["source_fields"] = ["secret"]
    elif part == "override":
        a["knn"]["collection"] = "foreign"
    else:
        a[part] = {"match_all": {}} if part == "query" else [] if part == "sort" else 1
    assert not main.handle(p)["ok"] and not calls


def test_unbound_vector_requires_sample_never_fake_dimension(monkeypatch):
    calls = fake(monkeypatch)
    p = vector_request("validate_query")
    a = p["action"]["input"]
    a["query_template"]["query_definition_jsonb"]["arguments"]["knn"]["query_vector"] = {
        "$param": "v"
    }
    a["query_template"]["param_schema_jsonb"] = {"properties": {"v": {"type": "array"}}}
    result = main.handle(p)
    assert result["ok"], result
    assert result["result"]["validation_complete"] is False
    assert result["result"]["required_sample_params"] == ["v"]
    assert not calls
