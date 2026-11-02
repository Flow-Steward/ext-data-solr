"""Bound retrieval excerpts and validate citation syntax, not entailment.

Retrieved text is untrusted evidence. ``grounded`` records citation consistency
only; it does not assert that an answer logically follows from its excerpts.
"""

from __future__ import annotations

import copy
import math
import re
from typing import Any

from documents import MAX_DOCUMENT_BYTES, bounded_string, safe_metadata, safe_source_uri

ABSTENTION_ANSWER = "I do not have enough retrieved evidence to answer this question."
SOURCE_ID = re.compile(r"S[1-9][0-9]*\Z")
MARKER = re.compile(r"\[(S[^\]\s]*)\]")


def build_context(matches: list, *, max_characters: int = 24000) -> dict:
    """Label exact excerpt prefixes within a total context character budget."""
    if not isinstance(matches, list) or len(matches) > 1000:
        raise ValueError("matches must be a bounded list")
    if type(max_characters) is not int or not 1 <= max_characters <= 1000000:
        raise ValueError("max_characters must be an integer from 1 to 1000000")
    context = ""
    sources: list[dict] = []
    truncated = False
    for match in matches:
        if not isinstance(match, dict) or not isinstance(match.get("payload"), dict):
            raise ValueError("match must contain a payload object")
        payload = match["payload"]
        text = bounded_string(payload.get("text"), "text", MAX_DOCUMENT_BYTES)
        if not text.strip():
            continue
        source_id = f"S{len(sources) + 1}"
        prefix = ("\n\n" if sources else "") + f"[{source_id}]\n"
        available = max_characters - len(context) - len(prefix)
        if available <= 0:
            truncated = True
            break
        excerpt = text[:available]
        score = match.get("score")
        if score is not None and (
            isinstance(score, bool)
            or not isinstance(score, (int, float))
            or not math.isfinite(score)
        ):
            raise ValueError("match score must be finite")
        source = {
            "source_id": source_id,
            "document_id": bounded_string(payload.get("document_id", ""), "document_id", 256),
            "chunk_id": bounded_string(
                payload.get("chunk_id", str(match.get("id", ""))), "chunk_id", 256
            ),
            "source_uri": safe_source_uri(payload.get("source_uri", "")),
            "title": bounded_string(payload.get("title", ""), "title", 1024),
            "metadata": safe_metadata(payload.get("metadata")),
            "score": score,
            "excerpt": excerpt,
            "text": excerpt,
        }
        sources.append(source)
        context += prefix + excerpt
        if len(excerpt) < len(text):
            truncated = True
            break
    return {
        "context": context,
        "sources": sources,
        "match_count": len(sources),
        "truncated": truncated,
        "answerable": bool(sources),
    }


def _abstention() -> dict:
    return {
        "answer": ABSTENTION_ANSWER,
        "sources": [],
        "citation_count": 0,
        "grounded": False,
        "abstained": True,
    }


def finalize_answer(
    answer: str, cited_source_ids: list, sources: list, *, abstained: bool = False
) -> dict:
    """Require nonempty, known citation IDs to equal the answer's marker set.

    This is deterministic syntax validation. It cannot verify semantic support.
    Empty retrieval always yields the same abstention without model content.
    """
    if not isinstance(sources, list) or len(sources) > 1000:
        raise ValueError("sources must be a bounded list")
    if type(abstained) is not bool:
        raise ValueError("abstained must be boolean")
    if not sources:
        return _abstention()
    bounded_string(answer, "answer", MAX_DOCUMENT_BYTES, required=not abstained)
    if not isinstance(cited_source_ids, list) or any(
        not isinstance(item, str) or SOURCE_ID.fullmatch(item) is None for item in cited_source_ids
    ):
        raise ValueError("cited_source_ids must be a list of source IDs")
    marker_ids = set(MARKER.findall(answer))
    if abstained:
        if cited_source_ids or marker_ids:
            raise ValueError("an abstention must not contain citations")
        return _abstention()
    source_map: dict[str, Any] = {}
    for source in sources:
        if not isinstance(source, dict):
            raise ValueError("source must be an object")
        identifier = source.get("source_id")
        if not isinstance(identifier, str) or SOURCE_ID.fullmatch(identifier) is None:
            raise ValueError("source_id is invalid")
        if identifier in source_map:
            raise ValueError("source IDs must be unique")
        source_map[identifier] = source
    identifiers = set(cited_source_ids)
    if not identifiers or identifiers != marker_ids or not identifiers <= source_map.keys():
        raise ValueError("answer markers must match nonempty known cited_source_ids exactly")
    selected = [copy.deepcopy(source) for source in sources if source["source_id"] in identifiers]
    return {
        "answer": answer,
        "sources": selected,
        "citation_count": len(selected),
        "grounded": True,
        "abstained": False,
    }
