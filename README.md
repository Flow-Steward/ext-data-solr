# Apache Solr for Flow Steward

Use Apache Solr in Flow Steward for searchable document collections, supplied-vector retrieval and read-only Dataset queries. Workflow actions retrieve a document by its unique key, search selected stored fields, replace one document or a batch of up to 100, set individual fields, and delete an exact document ID. Search supports text matching, exact terms, ranges, Boolean filters and sorting. Nearest-neighbor retrieval uses an existing dense-vector field and returns matching documents with scores.

Read-only JSON Datasets add schema inspection, saved parameterized searches, previews and output-schema inference over explicitly allowed collections and fields. Included example workflows demonstrate chunk ingestion, field updates, deletion and question answering with source citations. You supply embeddings and configure the project's answer model; the extension does not generate vectors.

Create collections and schemas before connecting. Document fields must be flat scalars or arrays, and reads return at most 100 rows. Solr controls when accepted writes become searchable. The connector covers document access rather than schema administration and does not expose raw query syntax or delete-by-query. Multi-chunk changes require tracking and removing obsolete chunk IDs yourself.

## Runtime and operation requirements

Standalone `flowsteward.solr` 1.0.2 provides exact-ID document CRUD, bounded
full-text search, supplied-vector retrieval, and native read-only Dataset/DataQuery
queries. No triggers, schema administration, Solr server, Java runtime, or driver are
bundled. Apache Solr is a trademark of The Apache Software Foundation; this
independent connector does not imply endorsement.

Install the separate `linux-amd64` or `linux-arm64` ZIP through Flow Steward's
extension installer. The host supplies Python 3.12 and
`flowsteward-extension-sdk==0.4.0`. The declared minimum host is 1.5.0.
Only Linux transport workers are supported. Archive limits are 50 MiB compressed
and 150 MiB expanded; this pure Python connector is substantially smaller.

## Connection and collection setup

Create a Solr core/collection yourself. Configure a project `solr` connection:

```json
{"origin":"https://solr.example.org","collections":["knowledge"],"request_timeout_seconds":15}
```

`origin` is an HTTP(S) origin without credentials, path, query, or fragment.
The connector adds the fixed `/solr` prefix. Custom reverse-proxy prefixes are
not supported. List 1–32 exact, case-sensitive collection/core names; wildcards,
alias discovery, multi-collection selectors, routing overrides, and global admin
permissions are not supported. The operator must ensure each name is a concrete
collection/core, not a Solr alias.

Choose either secret `bearer_token` or secrets `username` and `password` (HTTP
Basic). Credentials are never accepted in URLs or action inputs. A Basic username
cannot contain a colon. Public endpoints require TLS; all resolved addresses must
pass the SDK policy. Private endpoints additionally require the host's explicit
`FS_ALLOW_PRIVATE_REMOTE_URLS` policy. DNS pinning preserves TLS SNI and certificate
verification; no redirect, retry, or provider error-body passthrough is enabled.

The connection test reads only the first allowed collection's `/schema`.
Introspection reads schemas only for the explicitly allowed/scoped collections.
It lists concrete stored fields; dynamic-field patterns are omitted and reported.
Unknown field classes retain an unconstrained JSON output schema `{}`. Solr decides
actual field representation; missing fields remain absent, and JSON null generally
removes a field rather than preserving a stored null. Solr does not preserve
arbitrary nested JSON objects: document inputs accept flat scalars/flat arrays only.

Use a single-valued indexed `solr.StrField` unique key (normally `id`). Every
operation discovers and validates the real unique key. For RAG examples create
stored fields `document_id`, `chunk_id`, `text`, `source_uri`, `title`, and `vector`.
Define `text` using a tokenizing text type; metadata should use appropriate string
fields. A minimal **test-only**, three-dimensional vector schema is:

```xml
<fieldType name="knn_vector" class="solr.DenseVectorField"
           vectorDimension="3" similarityFunction="cosine"/>
<field name="vector" type="knn_vector" indexed="true" stored="true"/>
```

Production vectors must use the real model's fixed dimensions (up to 4096 here),
the same model/version for documents and queries, and matching similarity
configuration. All vector values must be finite numeric values; booleans, empty,
wrong-dimensional, and nested vectors are rejected. This connector never generates
embeddings or calls a paid model automatically. An existing published embedding
action such as `flowsteward.cohere-retrieval` `embed` can be wired by an operator:
index text with `input_type: search_document`, query text with `search_query`,
then explicitly map its numeric vector into these actions. The included workflows
accept supplied vectors and make no claims about unexecuted paid model calls.

## Workflow actions

All connected actions require `connection_ref` and `collection`.

- `get_document`: `document_id`, explicit `source_fields`; real-time exact-ID get.
- `search`: explicit `source_fields`, optional closed `query`, `sort`, `size` ≤100.
- `upsert_document`: `document_id`, `document`; replaces the entire existing document.
- `upsert_documents`: 1–100 `documents`, each containing the actual unique key.
- `update_document`: `document_id`, `doc`; atomic `set` for each supplied field.
- `delete_document`: `document_id`; connector-built exact-ID delete command only.
- `knn_search`: `field`, `query_vector`, `k` ≤100, optional closed `filter`, and
  explicit `source_fields`.

The connector constructs Solr update commands. Full replacements go to
`/update/json/docs`; atomic updates use a controlled `/update` `add.doc` command
so Solr interprets generated `set` modifiers correctly. an input field named `delete` is data if present in the
schema, never a command. No user-supplied command objects, delete by query,
commit, optimize, script, update processor, route, or arbitrary endpoint is exposed.
Writes require a durable external-effect claim and actual account/project context;
test mode suppresses them. A timeout after dispatch reports an unknown external
effect, is never automatically retried, and must be reconciled by exact-ID reads.

All writes use fixed `commitWithin=1000` milliseconds. Solr's configured update
handler determines soft/hard commit behavior and search visibility. A write result
acknowledges server acceptance; it does not promise durable fsync or immediate
search visibility. Poll bounded exact-ID/search reads when a workflow needs
visibility; no explicit commit is sent. Atomic `set` with null/empty list removes
values. Atomic updates can create a previously absent ID and do not provide
compare-and-swap. They require a reconstructable schema: all ordinary fields and
dynamic rules must be stored or have docValues, with derived copy destinations
configured correctly. Unsupported schemas fail before any update is sent.

The closed query shape is `match_all`, `term`, `terms`, `match`, `range` or `bool`.
For example:

```json
{"bool":{"must":[{"match":{"text":"maintenance schedule"}}],"filter":[{"term":{"document_id":"manual"}}]}}
```

`term` uses Solr's term parser (exact indexed term), `match` uses its field parser
and field analyzer. Neither accepts Lucene syntax as query code. Range boundaries
are escaped literals; bool has bounded `must`, `must_not`, `filter`, `should`
arrays and optional `minimum_should_match`. No raw `q`, local params, join,
function, streaming, subquery, transformer, field wildcard, or parameter reference
is accepted. Field names are explicit top-level names. `score` and the `__fs_`
prefix are reserved; generated `__fs_id` and `__fs_score` aliases remain separate
from stored source payloads.

Dense retrieval sends a generated main `knn` query and ordinary closed `filter`
clauses, which Solr 9.11 applies as graph prefilters. It returns up to `k` matching
nearest neighbors, not `k` rows after a postfilter. Retrieval is approximate HNSW;
fewer results can exist than requested. Scores are Solr similarity scores (cosine
is normalized by Solr), not a universal relevance probability.

## Dataset and DataQuery

The public manifest advertises `dataset_connector` and mirrors native connector
metadata in the descriptor. `supported_connectors` and `runtime_health` support
null administrative runtime context. The six Dataset actions are
`test_connection`, `introspect_schema`, `validate_query`, `preview_query`,
`execute_template`, and `infer_output_schema`.

A saved `json` query has exactly:

```json
{"resource_id":"knowledge","operation":"search","arguments":{"source_fields":["text","title"],"query":{"match":{"text":{"$param":"question"}}},"size":10}}
```

Declare `question` as string in `param_schema_jsonb.properties` and supply a typed
parameter value. Only `get` and `search` operations are read-only query templates;
vector retrieval uses `arguments.knn` with `field`, `query_vector`, `k`, optional
`filter`, plus `source_fields`. Hybrid `query`/`sort`/`size` controls with `knn` are
rejected. Column scopes apply to projections, query/filter fields, sort fields,
and the vector field. Resource names are exact and case-sensitive. No nested
field access is exposed, and unselected provider fields fail closed.

Unbound typed parameters validate only known grammar and report
`sample_parameters_required` with `validation_complete:false`. They never execute
synthetic sample embeddings or claim full schema validation. Bound execution
checks the actual collection schema. Definitions and complete Dataset inputs are
bounded to 64 KiB, depth 20, and 10,000 nodes. Results have at most 100 rows and
400 KiB **including the host envelope**. One deadline of at most 30 seconds covers
all schema calls, DNS, TLS, socket I/O, and slow bodies. Deadline workers are
terminated and reaped immediately.

## RAG examples and limits

Four published workflow artifacts demonstrate single-chunk ingest, atomic field-set
update, exact-ID delete, and retrieve→answer with citations. `prepare_chunk` derives
a stable physical ID from document/chunk IDs and validates source URI/title/text;
`prepare_sources` builds bounded untrusted context; `finalize_answer` validates
citation IDs against retrieved source metadata. The agent inherits the project's
configured provider/model. When chunk text changes, supply its matching new
embedding in `doc.vector`; metadata-only updates can preserve the vector. Its skill allows an empty optional answer when evidence
is insufficient; the finalizer emits a nonempty abstention. Empty retrieval bypasses
the agent. These examples use actual Core action references and import/mapping/
output-contract validation, not a custom fake workflow engine.

Chunk sets are not transactions. To update an entire multi-chunk source, inventory
all old physical IDs externally, replace current chunks, and delete obsolete IDs
explicitly. A batch failure can have partial effects; reconcile each ID. No delete
by source query, automatic chunk orphan cleanup, document transaction, or automatic
embedding service is claimed. See `artifacts/README.md` for action references.

## Development

Run checks through the Flow Steward Core Docker extension gate (not host pytest):

```sh
FS_COMPOSE_PROJECT_NAME=fs-nosql-solr ./core/entrypoints/cli/scripts/lint.sh \
  --checks extension-static,extension-tests \
  --extension-root /absolute/path/ext-data-solr \
  --extension-id flowsteward.solr --evidence-mode off
```

Set `FS_COMPOSE_FILES` to the approved bounded audit override for your environment.
Live tests are opt-in and use only operator-created disposable collections and
explicit ephemeral credentials. See `tests/test_live_solr.py`. Build deterministic
architecture-labelled archives with `scripts/package_extension.py --runtime-target
linux-amd64` (or `linux-arm64`); the runtime payload is pure Python for both.

Official API references (Solr 9.11):
[dense vectors](https://solr.apache.org/guide/solr/9_11/query-guide/dense-vector-search.html),
[JSON Query DSL](https://solr.apache.org/guide/solr/9_11/query-guide/json-query-dsl.html),
[document updates](https://solr.apache.org/guide/solr/9_11/indexing-guide/indexing-with-update-handlers.html),
[atomic updates](https://solr.apache.org/guide/solr/9_11/indexing-guide/partial-document-updates.html),
[schema API](https://solr.apache.org/guide/solr/9_11/indexing-guide/schema-api.html).
The live version tested for this release is recorded in the acceptance report;
newer Solr versions are not implicitly certified.
