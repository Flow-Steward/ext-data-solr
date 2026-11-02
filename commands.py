"""Closed Solr query grammar, actual schemas and documents-only update builders."""

from __future__ import annotations
import json
import math
from urllib.parse import quote, urlencode
from validation import require, integer, text, field, collection_name, bounded_json

WRITES = {"upsert_document", "upsert_documents", "update_document", "delete_document"}


def parse_schema(value):
    require(
        isinstance(value, dict) and isinstance(value.get("schema"), dict),
        "Invalid collection schema.",
        code="invalid_provider_response",
    )
    source = value["schema"]
    fields = source.get("fields")
    types = source.get("fieldTypes")
    require(
        isinstance(fields, list) and isinstance(types, list),
        "Invalid schema fields.",
        code="invalid_provider_response",
    )
    require(len(fields) <= 10000 and len(types) <= 1000, "Schema complexity exceeded.")
    type_map = {
        v["name"]: v for v in types if isinstance(v, dict) and isinstance(v.get("name"), str)
    }
    result = {}
    for item in fields:
        require(
            isinstance(item, dict) and isinstance(item.get("name"), str),
            "Invalid schema field.",
            code="invalid_provider_response",
        )
        result[item["name"]] = {**type_map.get(item.get("type"), {}), **item}
    unique = source.get("uniqueKey")
    field(unique)
    u = result.get(unique, {})
    require(
        u.get("class") in {"solr.StrField", "org.apache.solr.schema.StrField"}
        and u.get("indexed", True) is True
        and not u.get("multiValued", False),
        "A concrete indexed single-valued string unique key is required.",
        code="unsupported_schema",
    )
    dynamic = source.get("dynamicFields", [])
    copies = source.get("copyFields", [])
    require(isinstance(dynamic, list) and isinstance(copies, list), "Invalid schema metadata.")
    destinations = {v.get("dest") for v in copies if isinstance(v, dict)}
    reconstructable = True
    for item in [*result.values(), *dynamic]:
        require(isinstance(item, dict), "Invalid schema metadata.")
        effective = {**type_map.get(item.get("type"), {}), **item}
        if effective.get("name") in destinations:
            if (
                effective.get("stored", False)
                or effective.get("docValues", False)
                and effective.get("useDocValuesAsStored", True)
            ):
                reconstructable = False
        elif not effective.get("stored", False) and not effective.get("docValues", False):
            reconstructable = False
    return {
        "fields": result,
        "unique_key": unique,
        "dynamic": bool(dynamic),
        "atomic_safe": reconstructable,
    }


def scalar(v):
    require(type(v) in {str, int, float, bool}, "Non-null scalar query values required.")
    if isinstance(v, str):
        text(v, "query value", 4096)
        require(
            "$" not in v and not any(ord(c) < 32 for c in v),
            "Parameter references and control characters are not query literals.",
        )
    bounded_json(v)
    return ("true" if v else "false") if type(v) is bool else str(v)


def query(v, check_field=field):
    require(isinstance(v, dict) and len(v) == 1, "One closed query operator required.")
    op, value = next(iter(v.items()))
    require(
        op in {"match_all", "term", "terms", "match", "range", "bool"} and isinstance(value, dict),
        "Unsupported query operator.",
    )
    if op == "match_all":
        require(not value, "match_all takes no controls.")
        return "*:*"
    if op == "bool":
        require(
            bool(value)
            and set(value) <= {"must", "must_not", "filter", "should", "minimum_should_match"},
            "Unsupported bool control.",
        )
        output = {}
        for k, children in value.items():
            if k == "minimum_should_match":
                output["mm"] = integer(children, "minimum_should_match", 0, 32)
            else:
                require(
                    isinstance(children, list) and 1 <= len(children) <= 32,
                    "Bounded query clauses required.",
                )
                output[k] = [query(c, check_field) for c in children]
        if "must" not in output and "filter" not in output:
            if "should" not in output:
                output["must"] = ["*:*"]
            elif "mm" not in output:
                output["mm"] = 1
        return {"bool": output}
    require(len(value) == 1, "One field per query operator.")
    name, operand = next(iter(value.items()))
    check_field(name)
    if op == "range":
        require(
            isinstance(operand, dict)
            and bool(operand)
            and set(operand) <= {"gt", "gte", "lt", "lte"}
            and not {"gt", "gte"} <= set(operand)
            and not {"lt", "lte"} <= set(operand),
            "Closed range boundaries required.",
        )

        def bound(a, b):
            if a not in operand and b not in operand:
                return "*"
            raw = scalar(operand.get(a, operand.get(b)))
            return '"' + raw.replace("\\", "\\\\").replace('"', '\\"') + '"'

        low = bound("gt", "gte")
        high = bound("lt", "lte")
        expression = (
            name
            + (":{" if "gt" in operand else ":[")
            + low
            + " TO "
            + high
            + ("}" if "lt" in operand else "]")
        )
        return {"lucene": {"query": expression}}
    if op == "terms":
        require(isinstance(operand, list) and 1 <= len(operand) <= 100, "Bounded terms required.")
        return {
            "bool": {
                "should": [{"term": {"f": name, "query": scalar(x)}} for x in operand],
                "mm": 1,
            }
        }
    if op == "match":
        require(isinstance(operand, str), "match requires text.")
    raw = scalar(operand)
    return {"field" if op == "match" else "term": {"f": name, "query": raw}}


def projection(v, check_field=field):
    require(isinstance(v, list) and 1 <= len(v) <= 100, "Explicit source_fields required.")
    for name in v:
        check_field(name)
    require(len(set(v)) == len(v), "Duplicate fields.")
    return v


def vector(value, schema=None, name=None):
    require(
        isinstance(value, list)
        and 1 <= len(value) <= 4096
        and all(type(x) in {int, float} and math.isfinite(x) for x in value),
        "Finite bounded vector required.",
    )
    if schema is not None:
        m = schema["fields"].get(name, {})
        dimension = m.get("vectorDimension")
        # Native Schema API retains non-boolean field-type attributes as strings.
        if (
            isinstance(dimension, str)
            and dimension.isascii()
            and dimension.isdecimal()
            and len(dimension) <= 8
        ):
            dimension = int(dimension)
        require(
            m.get("class") in {"solr.DenseVectorField", "org.apache.solr.schema.DenseVectorField"}
            and m.get("indexed", True) is True
            and not m.get("multiValued", False)
            and type(dimension) is int
            and 1 <= dimension <= 4096
            and dimension == len(value),
            "Indexed single-valued DenseVectorField with matching dimensions required.",
            code="unsupported_vector_mapping",
        )
    return value


def prepare(name, args, config, *, limit=100, check_field=field, schema=None):
    allowed = {
        "get_document": {"document_id", "source_fields"},
        "search": {"query", "source_fields", "sort", "size"},
        "upsert_document": {"document_id", "document"},
        "upsert_documents": {"documents"},
        "update_document": {"document_id", "doc"},
        "delete_document": {"document_id"},
        "knn_search": {"field", "query_vector", "k", "filter", "source_fields"},
    }
    require(name in allowed, "Unknown action.")
    require(
        isinstance(args, dict) and set(args) <= allowed[name] | {"collection", "connection_ref"},
        "Unknown action input.",
    )
    collection = collection_name(args.get("collection"))
    require(collection in config["collections"], "Collection outside configured allowlist.")
    path = "/solr/" + quote(collection, safe="")
    unique = schema["unique_key"] if schema is not None else "id"

    def check(k, *, stored=False):
        check_field(k)
        if schema is not None:
            m = schema["fields"].get(k)
            require(
                isinstance(m, dict),
                "Only concrete schema fields are supported.",
                code="unsupported_schema",
            )
            require(
                m.get("stored", False) if stored else m.get("indexed", True),
                "Field must be stored for projection or indexed for querying.",
                code="unsupported_schema",
            )

    if name in {"get_document", "search", "knn_search"}:
        selected = projection(args.get("source_fields"), lambda k: check(k, stored=True))
        fields = [*selected, "__fs_id:" + unique]
        if name == "get_document":
            docid = text(args.get("document_id"), "document_id", 512, nonempty=True)
            return (
                "GET",
                path + "/get?" + urlencode({"id": docid, "fl": ",".join(fields), "wt": "json"}),
                None,
            )
        fields.append("__fs_score:score")
        body = {
            "fields": fields,
            "limit": min(
                integer(
                    args.get("size", args.get("k", 100 if name == "search" else 5)), "size", 1, 100
                ),
                limit,
            ),
        }
        if name == "search":
            body["query"] = query(args.get("query", {"match_all": {}}), check)
            if "sort" in args:
                sort = args["sort"]
                require(isinstance(sort, list) and len(sort) <= 8, "Bounded sort required.")
                parts = []
                for item in sort:
                    require(
                        isinstance(item, dict) and len(item) == 1, "Explicit sort field required."
                    )
                    key, direction = next(iter(item.items()))
                    check(key)
                    require(direction in {"asc", "desc"}, "Invalid sort direction.")
                    parts.append(key + " " + direction)
                if parts:
                    body["sort"] = ",".join(parts)
        else:
            f = args.get("field")
            check(f)
            values = vector(args.get("query_vector"), schema, f)
            k = min(integer(args.get("k", 5), "k", 1, 100), limit)
            body["query"] = (
                "{!knn f="
                + f
                + " topK="
                + str(k)
                + "}"
                + json.dumps(values, separators=(",", ":"), allow_nan=False)
            )
            if "filter" in args:
                body["filter"] = [query(args["filter"], check)]
        bounded_json(body)
        return "POST", path + "/query", body
    if name == "upsert_documents":
        docs = args.get("documents")
        require(isinstance(docs, list) and 1 <= len(docs) <= 100, "Supply 1–100 documents.")
    else:
        docid = text(args.get("document_id"), "document_id", 512, nonempty=True)
        if name == "delete_document":
            return "POST", path + "/update?commitWithin=1000", {"delete": {"id": docid}}
        doc = args.get("document" if name == "upsert_document" else "doc")
        require(isinstance(doc, dict) and bool(doc), "Nonempty document required.")
        require(schema is None or unique not in doc, "Supply identity only in document_id.")
        docs = [{unique: docid, **doc}]
    output: list[dict] = []
    seen = set()
    for doc in docs:
        require(isinstance(doc, dict) and bool(doc), "Nonempty document required.")
        docid = text(
            doc.get(unique) if schema is not None else str(len(output)),
            "unique key",
            512,
            nonempty=True,
        )
        require(docid not in seen, "Duplicate document ID.")
        seen.add(docid)
        result = {unique: docid}
        for key, value in doc.items():
            field(key)
            require(
                key not in {"_childDocuments_", "_root_", "_nest_path_", "_version_"},
                "Nested documents and server fields are not supported.",
            )
            if schema is not None:
                require(
                    key in schema["fields"],
                    "Only concrete schema fields are writable.",
                    code="unsupported_schema",
                )
                m = schema["fields"][key]
                if m.get("class") in {
                    "solr.DenseVectorField",
                    "org.apache.solr.schema.DenseVectorField",
                }:
                    vector(value, schema, key)

            def simple(v):
                return v is None or type(v) in {str, int, float, bool}

            require(
                simple(value) or isinstance(value, list) and all(simple(c) for c in value),
                "Flat scalar or scalar-array fields required; no update modifiers.",
            )
            bounded_json(value)
            if key != unique:
                result[key] = {"set": value} if name == "update_document" else value
        output.append(result)
    if name == "update_document" and schema is not None:
        require(
            schema["atomic_safe"],
            "Atomic updates require a reconstructable stored/docValues schema.",
            code="unsupported_schema",
        )
    bounded_json(output)
    if name == "update_document":
        # json/docs transforms object values as children; command mode parses atomic modifiers.
        return "POST", path + "/update?commitWithin=1000", {"add": {"doc": output[0]}}
    return "POST", path + "/update/json/docs?commitWithin=1000", output


def documents(name, value, selected, limit):
    require(
        isinstance(value, dict)
        and isinstance(value.get("responseHeader"), dict)
        and value["responseHeader"].get("status") == 0,
        "Invalid provider result.",
        code="invalid_provider_response",
    )
    if name in WRITES:
        return {"applied": True, "result": "accepted"}
    if name == "get_document":
        require("doc" in value, "Missing real-time get result.", code="invalid_provider_response")
        hits = [] if value["doc"] is None else [value["doc"]]
    else:
        hits = value.get("response", {}).get("docs")
    require(
        isinstance(hits, list) and len(hits) <= limit,
        "Excessive provider hits.",
        code="response_too_large",
    )
    result = []
    for hit in hits:
        require(
            isinstance(hit, dict) and set(hit) <= set(selected) | {"__fs_id", "__fs_score"},
            "Provider source escaped projection.",
            code="invalid_provider_response",
        )
        require(
            isinstance(hit.get("__fs_id"), str),
            "Missing document ID.",
            code="invalid_provider_response",
        )
        score = hit.get("__fs_score")
        require(
            score is None or type(score) in {int, float} and math.isfinite(score),
            "Invalid score.",
            code="invalid_provider_response",
        )
        source = {k: v for k, v in hit.items() if k in selected}
        result.append({"document_id": hit["__fs_id"], "score": score, "source": source})
    bounded_json(result, 1024 * 1024)
    return {"documents": result, "count": len(result), "found": bool(result)}
