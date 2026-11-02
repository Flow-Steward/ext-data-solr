"""Read-only Apache Solr Dataset host actions with exact field scopes."""

from __future__ import annotations
import copy
import time
from commands import prepare, documents, parse_schema
from validation import require, integer, bounded_json, field, collection_name, connection_values

ACTIONS = frozenset(
    {
        "test_connection",
        "introspect_schema",
        "validate_query",
        "preview_query",
        "execute_template",
        "infer_output_schema",
    }
)
TYPES = {"string", "integer", "number", "boolean", "null", "object", "array"}


def bounded(v):
    bounded_json(v, 65536)


def catalog():
    return [
        {
            "connector_id": "solr",
            "connector_type_id": "solr",
            "database_kind": "solr",
            "source_kind": "solr",
            "display_name": "Apache Solr",
            "source_extension_id": "flowsteward.solr",
            "source_extension_name": "Apache Solr",
            "source_provider": "Apache Solr",
            "query_language": "json",
            "access_mode": "read_only",
        }
    ]


def kind(value):
    if value is None:
        return "null"
    if type(value) is bool:
        return "boolean"
    if type(value) is int:
        return "integer"
    if type(value) is float:
        return "number"
    if isinstance(value, str):
        return "string"
    if isinstance(value, list):
        return "array"
    return "object"


def bind(arguments, template, params, execution):
    schema = template.get("param_schema_jsonb", {})
    require(
        isinstance(schema, dict) and isinstance(schema.get("properties", {}), dict),
        "Invalid parameter schema.",
    )
    declarations = schema.get("properties", {})
    require(isinstance(params, dict), "Parameters must be an object.")
    names = set()
    missing = {}

    def visit(v):
        if isinstance(v, dict):
            if "$param" in v:
                require(
                    set(v) == {"$param"} and isinstance(v["$param"], str),
                    "Use single-member parameter markers.",
                )
                name = v["$param"]
                names.add(name)
                require(
                    name in declarations and isinstance(declarations[name], dict),
                    "Undeclared parameter.",
                )
                types = declarations[name].get("type")
                types = types if isinstance(types, list) else [types]
                require(bool(types) and all(t in TYPES for t in types), "Invalid parameter types.")
                if name not in params:
                    require(not execution, "Missing query parameter.")
                    missing[name] = set(types)
                    return dict(v)
                value = params[name]
                require(
                    kind(value) in types or kind(value) == "integer" and "number" in types,
                    "Parameter type mismatch.",
                )
                bounded(value)
                return copy.deepcopy(value)
            return {k: visit(c) for k, c in v.items()}
        if isinstance(v, list):
            return [visit(c) for c in v]
        return v

    value = visit(arguments)
    require(set(params) <= names, "Unknown query parameter.")
    bounded(value)
    return value, missing


def scope(dataset, config):
    require(isinstance(dataset, dict), "Dataset required.")
    raw = dataset.get("resource_scope_jsonb", {}).get("resources")
    require(isinstance(raw, list) and 1 <= len(raw) <= 32, "Select explicit resources.")
    result = {}
    for item in raw:
        require(isinstance(item, dict), "Invalid resource scope.")
        r = collection_name(item.get("resource_id"))
        require(r in config["collections"] and r not in result, "Resource outside allowlist.")
        columns = item.get("columns")
        require(
            columns is None or isinstance(columns, list) and len(columns) <= 100, "Invalid columns."
        )
        result[r] = None if columns is None else {field(c) for c in columns}
    return result


def compile_query(args, config, limit, execution, schema=None):
    dset = args.get("dataset")
    allowed = scope(dset, config)
    t = args.get("query_template")
    require(
        isinstance(t, dict)
        and t.get("query_language") == "json"
        and isinstance(t.get("query_template_id"), str)
        and t["query_template_id"],
        "Saved JSON query required.",
    )
    require(
        isinstance(dset.get("dataset_id"), str)
        and dset["dataset_id"]
        and t.get("dataset_id") == dset["dataset_id"],
        "Query belongs to another Dataset.",
    )
    d = t.get("query_definition_jsonb")
    bounded(d)
    require(
        isinstance(d, dict) and set(d) == {"resource_id", "operation", "arguments"},
        "Exact query definition required.",
    )
    r = d["resource_id"]
    require(isinstance(r, str) and r in allowed, "Resource outside scope.")
    op = d["operation"]
    require(op in {"get", "search"}, "Read-only get/search required.")
    require(isinstance(d["arguments"], dict), "Arguments must be an object.")
    a, missing = bind(d["arguments"], t, args.get("params", {}), execution)
    permitted = (
        {"document_id", "source_fields"}
        if op == "get"
        else {"query", "source_fields", "sort", "size", "knn"}
    )
    require(isinstance(a, dict) and set(a) <= permitted, "Unknown Dataset argument.")

    # Unknown structures cannot be claimed as fully checked. Validate the known
    # surroundings with position-compatible local samples, never sent upstream.
    def unknown(v, types, sample):
        if isinstance(v, dict) and set(v) == {"$param"} and v["$param"] in missing:
            require(
                bool(missing[v["$param"]] & types), "Parameter type cannot satisfy query position."
            )
            return copy.deepcopy(sample), True
        return v, False

    def scalar_sample(v):
        types = {"string", "integer", "number", "boolean", "null"}
        if isinstance(v, dict) and set(v) == {"$param"} and v["$param"] in missing:
            choices = missing[v["$param"]] & types
            require(bool(choices), "Scalar query parameter required.")
            values = {
                "string": "sample",
                "integer": 1,
                "number": 1.0,
                "boolean": False,
                "null": None,
            }
            return values[sorted(choices)[0]]
        return v

    def query_sample(v):
        v, skipped = unknown(v, {"object"}, {"match_all": {}})
        if skipped or not isinstance(v, dict) or len(v) != 1:
            return v
        operator, operand = next(iter(v.items()))
        if not isinstance(operand, dict):
            return v
        result = {}
        for key, child in operand.items():
            if operator == "bool" and key in {"must", "must_not", "should", "filter"}:
                child, _ = unknown(child, {"array"}, [{"match_all": {}}])
                result[key] = [query_sample(c) for c in child] if isinstance(child, list) else child
            elif operator == "bool" and key == "minimum_should_match":
                result[key], _ = unknown(child, {"integer"}, 1)
            elif operator == "terms":
                child, _ = unknown(child, {"array"}, ["sample"])
                result[key] = (
                    [scalar_sample(c) for c in child] if isinstance(child, list) else child
                )
            elif operator == "range":
                child, _ = unknown(child, {"object"}, {"gte": 1})
                result[key] = (
                    {k: scalar_sample(c) for k, c in child.items()}
                    if isinstance(child, dict)
                    else child
                )
            elif operator == "match":
                result[key], _ = unknown(child, {"string"}, "sample")
            else:
                result[key] = scalar_sample(child)
        return {operator: result}

    sampled = dict(a)
    if "query" in sampled:
        sampled["query"] = query_sample(sampled["query"])
    if "document_id" in sampled:
        sampled["document_id"], _ = unknown(sampled["document_id"], {"string"}, "sample")
    if "size" in sampled:
        sampled["size"], _ = unknown(sampled["size"], {"integer"}, 1)
    if "sort" in sampled:
        sort, _ = unknown(sampled["sort"], {"array"}, [])
        if isinstance(sort, list):
            sort = [
                {k: unknown(v, {"string"}, "asc")[0] for k, v in item.items()}
                if isinstance(item, dict)
                else item
                for item in sort
            ]
        sampled["sort"] = sort
    sample_field = next(iter(allowed[r]), "sample") if allowed[r] is not None else "sample"
    if "source_fields" in sampled:
        fields, _ = unknown(sampled["source_fields"], {"array"}, [sample_field])
        if isinstance(fields, list):
            fields = [unknown(v, {"string"}, sample_field)[0] for v in fields]
        sampled["source_fields"] = fields

    def check(k):
        field(k)
        require(allowed[r] is None or k in allowed[r], "Field outside Dataset scope.")

    action = "get_document" if op == "get" else "search"
    if op == "search" and "knn" in sampled:
        require(
            set(sampled) <= {"source_fields", "knn"},
            "Dataset kNN cannot combine query, sort or size controls.",
        )
        knn, _ = unknown(sampled["knn"], {"object"}, {"field": sample_field, "query_vector": [1.0]})
        require(
            isinstance(knn, dict) and set(knn) <= {"field", "query_vector", "k", "filter"},
            "Closed kNN object required.",
        )
        knn = dict(knn)
        if "field" in knn:
            knn["field"], _ = unknown(knn["field"], {"string"}, sample_field)
        if "query_vector" in knn:
            vector, _ = unknown(knn["query_vector"], {"array"}, [1.0])
            if isinstance(vector, list):
                vector = [unknown(v, {"number", "integer"}, 1.0)[0] for v in vector]
            knn["query_vector"] = vector
        for key in ("k",):
            if key in knn:
                knn[key], _ = unknown(knn[key], {"integer"}, 5 if key == "k" else 100)
        if "filter" in knn:
            knn["filter"] = query_sample(knn["filter"])
        # Membership is checked by the same closed workflow vector compiler.
        sampled = {"source_fields": sampled.get("source_fields"), **knn}
        action = "knn_search"
        op = "knn_search"
    method, path, body = prepare(
        action, {"collection": r, **sampled}, config, limit=limit, check_field=check, schema=schema
    )
    return method, path, body, a.get("source_fields", []), r, sorted(missing), op


def handle(name, args, invoke):
    bounded(args)
    require(
        set(args)
        <= {"connection_config", "secret_refs", "dataset", "query_template", "params", "limits"},
        "Unknown Dataset input.",
    )
    refs = args.get("secret_refs", {})
    require(isinstance(refs, dict), "Invalid credentials.")
    config, secrets = connection_values(
        {
            "config": args.get("connection_config"),
            "secrets": refs.get("values", refs.get("refs", refs)),
        }
    )
    limits = args.get("limits", {})
    require(
        isinstance(limits, dict)
        and set(limits) <= {"max_preview_rows", "max_runtime_rows", "timeout_seconds", "max_bytes"},
        "Invalid limits.",
    )
    preview = integer(limits.get("max_preview_rows", 20), "preview", 1, 100)
    runtime = integer(limits.get("max_runtime_rows", 100), "runtime", 1, 100)
    budget = integer(limits.get("max_bytes", 400 * 1024), "bytes", 1024, 400 * 1024)
    timeout = integer(
        limits.get("timeout_seconds", config["request_timeout_seconds"]), "timeout", 1, 30
    )
    deadline = time.monotonic() + min(timeout, config["request_timeout_seconds"])

    def call(method, path, body=None):
        left = deadline - time.monotonic()
        require(left > 0, "Dataset deadline exceeded.", code="timeout")
        value = invoke(config, secrets, method, path, body, timeout_seconds=left)
        require(time.monotonic() < deadline, "Dataset deadline exceeded.", code="timeout")
        bounded_json(value, 1024 * 1024)
        return value

    if name == "test_connection":
        value = call("GET", "/solr/" + config["collections"][0] + "/schema?wt=json")
        parse_schema(value)
        return {
            "status": "ok",
            "connector_id": "solr",
            "evidence": "allowed_collection_schema",
        }
    if name == "introspect_schema":
        allowed = (
            scope(args["dataset"], config)
            if "dataset" in args
            else {r: None for r in config["collections"]}
        )
        resources = []
        remaining = 100
        skipped = 0
        truncated = False
        for r, cols in allowed.items():
            parsed = parse_schema(call("GET", "/solr/" + r + "/schema?wt=json"))
            columns = []
            for k, v in parsed["fields"].items():
                if cols is not None and k not in cols:
                    continue
                if v.get("stored", False) is not True:
                    continue
                try:
                    field(k)
                except Exception:
                    skipped += 1
                    truncated = True
                    continue
                if not remaining:
                    truncated = True
                    continue
                remaining -= 1
                columns.append(
                    {
                        "name": k,
                        "type": "json",
                        "data_type": v.get("class", "unknown"),
                        "value_type": "json",
                        "nullable": True,
                        "output_schema": {},
                    }
                )
            truncated = truncated or parsed["dynamic"]
            resources.append(
                {
                    "resource_id": r,
                    "name": r,
                    "resource_type": "collection",
                    "selectable": True,
                    "columns": columns,
                    "indexes": [],
                    "metadata": {
                        "schema_only": True,
                        "dynamic_fields_omitted": parsed["dynamic"],
                        "unique_key": parsed["unique_key"],
                    },
                }
            )
        snapshot = {
            "schema_version": "dataset_schema_snapshot.v1",
            "source_kind": "solr",
            "resources": resources,
            "truncated": truncated,
            "metadata": {
                "connector_id": "solr",
                "skipped_unsupported_fields": skipped,
                "column_limit": 100,
            },
        }
        result = {
            "schema_snapshot": snapshot,
            "resources": resources,
            "resource_count": len(resources),
            "truncated": truncated,
        }
        bounded_json(result, budget)
        return result
    limit = runtime if name == "execute_template" else preview
    method, path, body, selected, r, missing, op = compile_query(
        args, config, limit, name != "validate_query"
    )
    if name == "validate_query":
        result = {
            "valid": True,
            "read_only": True,
            "validation_complete": not missing,
            "referenced_resources": [r],
        }
        if missing:
            result.update(
                validation_status="sample_parameters_required", required_sample_params=missing
            )
        return result
    parsed = parse_schema(call("GET", "/solr/" + r + "/schema?wt=json"))
    method, path, body, selected, r, missing, op = compile_query(args, config, limit, True, parsed)
    value = call(method, path, body)
    result = documents("get_document" if op == "get" else op, value, selected, limit)
    rows = [h["source"] for h in result["documents"]]
    schema = {"type": "object", "properties": {c: {} for c in selected}}
    result = (
        {"output_schema": schema}
        if name == "infer_output_schema"
        else {
            "rows": rows,
            "row_count": len(rows),
            "output_schema": schema,
            "truncated": len(rows) == limit,
        }
    )
    bounded_json(result, budget)
    return result
