# Apache Solr RAG artifacts

Import the workflows, agent and skill declared in `extension-descriptor.yaml`. Choose each connected step's project Apache Solr connection, and configure the project's answer model provider. The workflows ingest one explicitly embedded chunk, partially update one exact physical ID, delete one exact physical ID, and retrieve/answer with citation checking.

See the main README for a reproducible synthetic-vector example and explicit multi-chunk lifecycle requirements. Embeddings are supplied by the caller. No Qdrant workflow is reused unchanged, and no automatic embedding provider is claimed. The grounded answer agent inherits the project provider and has no tools. The finalizer checks citation consistency, not semantic entailment.
