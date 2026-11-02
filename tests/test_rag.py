"""RAG source identity and citation consistency through host actions."""

import main
from test_runtime import envelope


def local(name, **args):
    p = envelope(name)
    p["action"]["input"] = args
    return main.handle(p)


def test_stable_chunk_identity_and_source_metadata():
    a = local(
        "prepare_chunk",
        document_id="manual",
        chunk_id="intro",
        text="Old",
        source_uri="https://example.org/manual",
        title="Manual",
        vector=[1, 0, 0],
    )
    b = local(
        "prepare_chunk",
        document_id="manual",
        chunk_id="intro",
        text="New",
        source_uri="https://example.org/manual",
        title="Manual",
        vector=[0, 1, 0],
    )
    assert a["ok"] and b["ok"], a
    assert a["result"]["document_id"] == b["result"]["document_id"]
    assert b["result"]["document"] == {
        "document_id": "manual",
        "chunk_id": "intro",
        "text": "New",
        "source_uri": "https://example.org/manual",
        "title": "Manual",
        "vector": [0, 1, 0],
    }


def test_retrieval_grounded_answer_and_empty_abstention():
    docs = [
        {
            "document_id": "physical",
            "score": 1,
            "source": {
                "document_id": "manual",
                "chunk_id": "intro",
                "text": "Earth is round.",
                "source_uri": "https://example.org/manual",
                "title": "Manual",
            },
        }
    ]
    r = local("prepare_sources", documents=docs)
    assert r["ok"], r
    assert r["result"]["context"] == "[S1]\nEarth is round."
    f = local(
        "finalize_answer",
        answer="Round [S1]",
        cited_source_ids=["S1"],
        sources=r["result"]["sources"],
    )
    assert f["ok"] and f["result"]["grounded"]
    assert f["result"]["sources"][0]["source_uri"] == "https://example.org/manual"
    assert not local(
        "finalize_answer",
        answer="Round [S2]",
        cited_source_ids=["S2"],
        sources=r["result"]["sources"],
    )["ok"]
    f = local("finalize_answer", answer="", cited_source_ids=[], sources=[])
    assert f["ok"] and f["result"]["abstained"]


def test_insufficient_evidence_passes_core_output_contract_and_citation_bridge():
    import json
    import os
    from pathlib import Path
    import subprocess

    root = Path(__file__).resolve().parents[1]
    response = subprocess.run(
        [
            "/usr/local/bin/python",
            str(root / "scripts/validate_workflows.py"),
            str(root),
            "--abstention-only",
        ],
        env={**os.environ, "PYTHONPATH": "/app"},
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert response.returncode == 0, response.stdout + response.stderr
    assert json.loads(response.stdout) == {
        "answer": "I do not have enough retrieved evidence to answer this question.",
        "sources": [],
        "citation_count": 0,
        "grounded": False,
        "abstained": True,
    }
