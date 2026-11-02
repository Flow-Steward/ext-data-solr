"""Explicit user-embedded chunks and deterministic source/citation handling."""

from __future__ import annotations
import hashlib
import json
import math
from citations import build_context, finalize_answer
from documents import bounded_string, safe_source_uri
from validation import require

ACTIONS = {"prepare_chunk", "prepare_sources", "finalize_answer"}


def handle(name, args):
    if name == "prepare_chunk":
        require(
            set(args) == {"document_id", "chunk_id", "text", "source_uri", "title", "vector"},
            "Exact chunk metadata and supplied vector required.",
        )
        document_id = bounded_string(args["document_id"], "document_id", 256, required=True)
        chunk_id = bounded_string(args["chunk_id"], "chunk_id", 256, required=True)
        text = bounded_string(args["text"], "text", 65536, required=True)
        title = bounded_string(args["title"], "title", 1024)
        uri = safe_source_uri(args["source_uri"])
        vector = args["vector"]
        require(
            isinstance(vector, list)
            and 1 <= len(vector) <= 4096
            and all(type(x) in {int, float} and math.isfinite(x) for x in vector),
            "Supply a finite vector from the indexing embedding model.",
        )
        physical_id = hashlib.sha256(
            json.dumps([document_id, chunk_id], ensure_ascii=True, separators=(",", ":")).encode()
        ).hexdigest()
        return {
            "document_id": physical_id,
            "document": {
                "document_id": document_id,
                "chunk_id": chunk_id,
                "text": text,
                "source_uri": uri,
                "title": title,
                "vector": vector,
            },
        }
    if name == "prepare_sources":
        require(set(args) == {"documents"}, "Only retrieval documents accepted.")
        docs = args["documents"]
        require(isinstance(docs, list) and len(docs) <= 100, "At most 100 retrieval documents.")
        matches = []
        for item in docs:
            require(
                isinstance(item, dict) and isinstance(item.get("source"), dict),
                "Invalid retrieval source.",
            )
            matches.append(
                {
                    "id": item.get("document_id", ""),
                    "score": item.get("score"),
                    "payload": item["source"],
                }
            )
        return build_context(matches, max_characters=24000)
    require(
        set(args) <= {"answer", "cited_source_ids", "sources", "abstained"},
        "Unknown citation input.",
    )
    return finalize_answer(**args)
