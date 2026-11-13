# Apache Solr workflow actions

Configure a project connection with the server URL, credentials and an explicit allowlist of existing indices or collections. Select that connection for document and search actions. The local RAG preparation/citation actions do not use a connection. Server fields and vector mappings must already exist; these actions do not create indices, collections, schemas or embeddings. Generate document and question embeddings with the same model and full dimension count.

Action names below match the workflow builder. IDs in parentheses remain compatible with saved workflows. Examples show field values; bind values from earlier steps as needed.

## Choose and configure an action

### Read a document by ID (`get_document`)

Read one document by its exact ID, returning only explicitly selected source fields.

```json
{
  "collection": "knowledge_chunks",
  "document_id": "manual_42",
  "source_fields": [
    "text",
    "document_id",
    "chunk_id",
    "source_uri",
    "title"
  ]
}
```

### Search documents by text or filters (`search`)

Read up to 100 matching documents using the supported term, match, range and boolean query format. Select the source fields to return; no arbitrary query scripts are accepted.

```json
{
  "collection": "knowledge_chunks",
  "query": {
    "match": {
      "text": "refund policy"
    }
  },
  "source_fields": [
    "text",
    "document_id",
    "chunk_id",
    "source_uri",
    "title"
  ],
  "sort": [
    {
      "title": "asc"
    }
  ]
}
```

### Create or replace a complete document (`upsert_document`)

Save a document at a stable ID, creating it if missing and replacing its stored fields if present. Omitted fields are removed.

```json
{
  "collection": "knowledge_chunks",
  "document_id": "manual_42",
  "document": {
    "text": "Refunds are accepted within 30 days.",
    "title": "Refund policy",
    "vector": [
      0.1,
      0.2,
      0.3
    ]
  }
}
```

### Create or replace a batch of documents (`upsert_documents`)

Save 1–100 complete Solr documents, each containing the collection’s actual unique-key field. Existing documents with those IDs are replaced.

```json
{
  "collection": "knowledge_chunks",
  "documents": [
    {
      "id": "manual_42",
      "text": "Refunds are accepted within 30 days."
    }
  ]
}
```

### Patch selected document fields (`update_document`)

Set the supplied fields on the selected document while preserving omitted fields. Supply plain field values, not scripts or update modifiers. Requires a reconstructable stored/docValues schema; the connector generates atomic set updates.

```json
{
  "collection": "knowledge_chunks",
  "document_id": "manual_42",
  "doc": {
    "title": "Updated refund policy"
  }
}
```

### Delete one document by ID (`delete_document`)

Delete the exact document ID from the selected collection. Other documents remain stored.

```json
{
  "collection": "knowledge_chunks",
  "document_id": "manual_42"
}
```

### Vector: Search similar documents (`knn_search`)

Retrieve up to 100 nearest documents using a supplied numeric embedding and optional supported filter. Use the same model and dimensions used to index the vector field; no embeddings are generated.

```json
{
  "collection": "knowledge_chunks",
  "field": "vector",
  "query_vector": [
    0.1,
    0.2,
    0.3
  ],
  "filter": {
    "term": {
      "document_id": "manual_42"
    }
  },
  "source_fields": [
    "text",
    "document_id",
    "chunk_id",
    "source_uri",
    "title"
  ]
}
```

### RAG: Prepare a text fragment for indexing (`prepare_chunk`)

Build a document with text, source details and a supplied embedding, plus a stable physical ID derived from document and fragment IDs. Pass both outputs to Create or replace a complete document; this step does not write or generate embeddings.

```json
{
  "document_id": "manual_42",
  "chunk_id": "part_1",
  "text": "Refund requests are accepted within 30 days.",
  "source_uri": "https://example.com/manual",
  "title": "Refund policy",
  "vector": [
    0.1,
    0.2,
    0.3
  ]
}
```

### RAG: Build cited context from search results (`prepare_sources`)

Turn retrieved documents into bounded excerpts labeled S1, S2 and so on for an answer model. Pass the context to the model and retain sources for citation validation; this step does not generate an answer.

```json
{
  "documents": [
    {
      "document_id": "physical_id",
      "score": 0.9,
      "source": {
        "text": "Refunds are accepted within 30 days.",
        "document_id": "manual_42",
        "chunk_id": "part_1",
        "source_uri": "https://example.com/manual",
        "title": "Refund policy"
      }
    }
  ]
}
```

### RAG: Validate answer citation IDs (`finalize_answer`)

Check that answer markers such as [S1] match the cited IDs and supplied sources. Returns a fixed abstention when no evidence exists; checks citation syntax, not whether the answer follows from the text.

```json
{
  "answer": "Refund requests are accepted within 30 days. [S1]",
  "cited_source_ids": [
    "S1"
  ],
  "sources": [
    {
      "source_id": "S1",
      "text": "Refunds are accepted within 30 days.",
      "excerpt": "Refunds are accepted within 30 days."
    }
  ]
}
```

## Requirements and result handling

Select source fields explicitly for every read/search; wildcard projection is not supported. Supported query operators are `match_all`, `term`, `terms`, `range`, `match` and `bool`, one per object. Example: `{"bool":{"filter":[{"term":{"document_id":"manual_42"}}],"must":[{"match":{"text":"refund"}}]}}`. Do not paste a full Elasticsearch request or raw Solr query string. Retrieval results are in `result.documents`.

For RAG: prepare one fragment with text, stable document/fragment IDs and a supplied vector; save the returned `document_id` and `document`. Generate a question embedding, search the vector field, and pass the search documents into Build cited context. Send its `context` to your answer model, retain its `sources`, then validate the answer and its cited IDs. Include text and source metadata in the search projection. Citation validation checks ID consistency only and does not prove factual support. The illustrative three-component vectors only fit a three-dimensional field.

Workflow write actions are suppressed in test mode. Read/search actions still read the configured service. If a write fails after dispatch, inspect the service before repeating it; the write may already have applied.
