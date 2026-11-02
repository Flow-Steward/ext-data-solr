"""Solr-specific command semantics, never user update commands or query parsers."""

import importlib
import pytest


def commands():
    module = importlib.import_module("commands")
    return module


def schema(unique="id", dims=3):
    return {
        "schema": {
            "uniqueKey": unique,
            "fields": [
                {"name": "id", "type": "string", "stored": True, "indexed": True},
                {"name": "text", "type": "text_general", "stored": True, "indexed": True},
                {"name": "value", "type": "boolean", "stored": True, "indexed": True},
                {"name": "title", "type": "string", "stored": True, "indexed": True},
                {"name": "vector", "type": "knn", "stored": True, "indexed": True},
            ],
            "fieldTypes": [
                {"name": "string", "class": "solr.StrField"},
                {"name": "text_general", "class": "solr.TextField"},
                {"name": "boolean", "class": "solr.BoolField"},
                {"name": "knn", "class": "solr.DenseVectorField", "vectorDimension": dims},
            ],
            "dynamicFields": [{"name": "*_s", "type": "string", "stored": True}],
            "copyFields": [],
        }
    }


def prepare(name, **args):
    return commands().prepare(
        name,
        {"collection": "Chunks", **args},
        {"collections": ["Chunks"]},
        schema=commands().parse_schema(schema()),
    )


def test_documents_only_update_and_atomic_set_not_replacement():
    assert prepare("upsert_document", document_id="a/b?x", document={"text": "hello"}) == (
        "POST",
        "/solr/Chunks/update/json/docs?commitWithin=1000",
        [{"id": "a/b?x", "text": "hello"}],
    )
    assert prepare("update_document", document_id="one", doc={"text": "new", "title": None}) == (
        "POST",
        "/solr/Chunks/update?commitWithin=1000",
        {"add": {"doc": {"id": "one", "text": {"set": "new"}, "title": {"set": None}}}},
    )
    assert prepare("delete_document", document_id="a/b?x") == (
        "POST",
        "/solr/Chunks/update?commitWithin=1000",
        {"delete": {"id": "a/b?x"}},
    )


def test_batch_upsert_bounded_and_unique_key_discovered():
    docs = [{"id": "one", "text": "one"}, {"id": "two", "text": "two"}]
    assert prepare("upsert_documents", documents=docs)[2] == docs
    with pytest.raises(Exception):
        prepare("upsert_documents", documents=docs * 51)
    with pytest.raises(Exception):
        prepare("upsert_documents", documents=[{"text": "missing"}])
    with pytest.raises(Exception):
        commands().parse_schema(schema(unique="missing"))


@pytest.mark.parametrize(
    "doc",
    [{"text": {"set": "oops"}}, {"_childDocuments_": []}, {"id": "other"}, {"vector": [1, 2]}],
)
def test_documents_reject_commands_children_identity_and_wrong_vector(doc):
    with pytest.raises(Exception):
        prepare("upsert_document", document_id="one", document=doc)


def test_query_literals_cannot_select_parser_or_other_field():
    value = "{!join from=secret to=id}*:* OR secret:*"
    body = prepare("search", source_fields=["text"], query={"term": {"text": value}})[2]
    assert body["query"] == {"term": {"f": "text", "query": value}}
    assert body["fields"] == ["text", "__fs_id:id", "__fs_score:score"]
    body = prepare("search", source_fields=["text"], query={"match": {"text": value}})[2]
    assert body["query"] == {"field": {"f": "text", "query": value}}


@pytest.mark.parametrize(
    "query",
    [
        {"lucene": {"query": "*:*"}},
        {"join": {}},
        {"term": {"text": {"query": "x"}}},
        {"match_all": {"boost": 2}},
        {"range": {"value": {"gte": "x", "unknown": 1}}},
        {"term": {"secret": "x"}},
        {"term": {"text": "$secret"}},
    ],
)
def test_closed_query_no_parser_or_parameter_dereference(query):
    with pytest.raises(Exception):
        prepare("search", source_fields=["text"], query=query)


@pytest.mark.parametrize(
    "field",
    ["text,secret", "*", "score", "__fs_id", "text:[subquery]", "sum(value)", "text} v=$secret"],
)
def test_field_projection_never_injects(field):
    with pytest.raises(Exception):
        prepare("search", source_fields=[field])


def test_vector_main_query_filters_precede_selection():
    body = prepare(
        "knn_search",
        field="vector",
        query_vector=[1, 0, 0],
        k=2,
        filter={"term": {"value": False}},
        source_fields=["text"],
    )[2]
    assert body["query"] == "{!knn f=vector topK=2}[1,0,0]"
    assert body["filter"] == [{"term": {"f": "value", "query": "false"}}]
    assert body["limit"] == 2
    with pytest.raises(Exception):
        prepare("knn_search", field="vector", query_vector=[1, 0], source_fields=["text"])
    with pytest.raises(Exception):
        prepare("knn_search", field="text", query_vector=[1, 0, 0], source_fields=["text"])
    with pytest.raises(Exception):
        prepare("knn_search", field="vector", query_vector=[True, 0, 0], source_fields=["text"])


def test_projection_enforces_provider_exact_columns_and_reserved_metadata():
    result = commands().documents(
        "search",
        {
            "responseHeader": {"status": 0},
            "response": {"docs": [{"__fs_id": "one", "__fs_score": 0.9, "text": "hello"}]},
        },
        ["text"],
        10,
    )
    assert result["documents"] == [
        {"document_id": "one", "score": 0.9, "source": {"text": "hello"}}
    ]
    with pytest.raises(Exception):
        commands().documents(
            "search",
            {
                "responseHeader": {"status": 0},
                "response": {"docs": [{"__fs_id": "one", "text": "x", "secret": 1}]},
            },
            ["text"],
            10,
        )


def test_atomic_update_requires_reconstructable_collection():
    data = schema()
    data["schema"]["fields"].append(
        {"name": "lost", "type": "string", "stored": False, "indexed": True}
    )
    with pytest.raises(Exception):
        commands().prepare(
            "update_document",
            {"collection": "Chunks", "document_id": "one", "doc": {"text": "new"}},
            {"collections": ["Chunks"]},
            schema=commands().parse_schema(data),
        )


def test_batch_preflight_cannot_assume_default_id_before_schema_discovery():
    # Syntax preflight must leave identity discovery to the actual schema call.
    commands().prepare(
        "upsert_documents",
        {"collection": "Chunks", "documents": [{"key": "one", "text": "x"}]},
        {"collections": ["Chunks"]},
    )
    data = schema(unique="key")
    data["schema"]["fields"][0]["name"] = "key"
    parsed = commands().parse_schema(data)
    result = commands().prepare(
        "upsert_documents",
        {"collection": "Chunks", "documents": [{"key": "one", "text": "x"}]},
        {"collections": ["Chunks"]},
        schema=parsed,
    )
    assert result[2] == [{"key": "one", "text": "x"}]


def test_native_schema_string_dimension_is_validated_as_actual_dimension():
    parsed = commands().parse_schema(schema(dims="3"))
    result = commands().prepare(
        "knn_search",
        {
            "collection": "Chunks",
            "field": "vector",
            "query_vector": [1, 0, 0],
            "source_fields": ["text"],
        },
        {"collections": ["Chunks"]},
        schema=parsed,
    )
    assert result[2]["query"] == "{!knn f=vector topK=5}[1,0,0]"
    with pytest.raises(Exception):
        commands().prepare(
            "knn_search",
            {
                "collection": "Chunks",
                "field": "vector",
                "query_vector": [1, 0],
                "source_fields": ["text"],
            },
            {"collections": ["Chunks"]},
            schema=parsed,
        )
