"""Validate authored artifacts against the actual Core import/runtime contracts."""

from typing import Any
from types import SimpleNamespace
import json
from pathlib import Path
import sys
import yaml
from core.application.workflows.workflow_definition_validation import validate_workflow_definition
from core.application.workflows.workflow_portability_import import _workflow_definition_issues
from core.application.orchestration.connector_mapping import (
    _render_input_payload,
    _apply_output_mapping,
)
from core.application.orchestration.control_flow_step_helpers import _set_variable_path
from core.application.orchestration.agent_messages import build_agent_message
from core.application.registry.step_output_contract import resolve_step_output_contract


def main(root):
    ops = {
        o["operation_id"]: o
        for o in json.loads((root / "contracts/operation_manifest.yaml").read_text())["operations"]
    }
    for path in sorted((root / "artifacts/workflows").glob("*.yaml")):
        w = yaml.safe_load(path.read_text())
        issues = validate_workflow_definition(
            {**w, "execution_graph": w["settings"]["execution_graph"]}
        )
        issues += _workflow_definition_issues(w, workflow_path=path.name)
        assert not issues, (path.name, issues)
        for s in w["steps"]:
            if s["step_kind"] == "connector":
                a = s["override"]["connector"]
                assert a["extension_id"] == "flowsteward.solr"
                op = ops[a["action_id"]]
                assert set(s["input_mapping_jsonb"]) <= {i["name"] for i in op["inputs"]}
                assert set(s["output_mapping_jsonb"]) <= {i["name"] for i in op["outputs"]}
        print("Core definition/import contract OK:", path.name)
    w = json.loads((root / "artifacts/workflows/solr_ask.yaml").read_text())
    search = next(s for s in w["steps"] if s["step_id"] == "retrieve")
    payload, missing = _render_input_payload(
        search["input_mapping_jsonb"],
        {"inputs": {"collection": "chunks", "query_vector": [1.0, 0.0, 0.0]}},
    )
    assert not missing and payload == {
        "collection": "chunks",
        "field": "vector",
        "query_vector": [1.0, 0.0, 0.0],
        "k": 5,
        "source_fields": ["document_id", "chunk_id", "text", "source_uri", "title"],
    }
    response: dict[str, Any] = {
        "invocation": {
            "response": {
                "result": {"documents": [{"document_id": "stable", "source": {"text": "evidence"}}]}
            }
        }
    }
    _apply_output_mapping(step_payload=search, dispatch_result=response, state={})
    assert response["state_patch"]["documents"][0]["document_id"] == "stable"
    state = {
        "agent_messages": [
            build_agent_message(
                from_agent="solr_grounded_answerer",
                to_agent="orchestrator",
                skill="solr_answer_from_sources",
                status="pass",
                correlation_id="contract",
                artifacts={
                    "answer": "Supported [S1]",
                    "cited_source_ids": ["S1"],
                    "abstained": False,
                },
                published_at="2026-11-02T00:00:00Z",
                step_id="answer_from_sources",
            )
        ]
    }
    publish = next(s for s in w["steps"] if s["step_id"] == "publish_answer")
    updates: dict[str, Any] = {}
    for target, value in publish["override"]["set_variable"]["assignments"].items():
        _set_variable_path(updates, state, target, value)
    assert {k: updates[k] for k in ("answer", "cited_source_ids", "abstained")} == {
        "answer": "Supported [S1]",
        "cited_source_ids": ["S1"],
        "abstained": False,
    }
    print("Core connector mapping and project-provider agent citation bridge OK")


def validate_abstention(root):
    """Exercise the actual selected-skill contract and publish/finalize path."""
    skill = yaml.safe_load((root / "artifacts/skills/solr_answer_from_sources.yaml").read_text())
    contract = resolve_step_output_contract(
        object(),
        agent_id="solr_grounded_answerer",
        skill_id=skill["skill_id"],
        account_id="contract-account",
        skill_manifest=SimpleNamespace(
            expected_outputs=skill["expected_outputs"], output_schema={}
        ),
        agent_manifest=SimpleNamespace(outputs=[]),
    )
    artifacts = {"answer": "", "cited_source_ids": [], "abstained": True}
    errors = contract.validate(artifacts)
    assert not errors, errors
    state = {
        "agent_messages": [
            build_agent_message(
                from_agent="solr_grounded_answerer",
                to_agent="orchestrator",
                skill=skill["skill_id"],
                status="pass",
                correlation_id="abstention-contract",
                artifacts=artifacts,
                published_at="2026-11-02T00:00:00Z",
                step_id="answer_from_sources",
            )
        ]
    }
    workflow = json.loads((root / "artifacts/workflows/solr_ask.yaml").read_text())
    publish = next(s for s in workflow["steps"] if s["step_id"] == "publish_answer")
    updates: dict[str, Any] = {}
    for target, value in publish["override"]["set_variable"]["assignments"].items():
        _set_variable_path(updates, state, target, value)
    # Retrieval is nonempty but insufficient; this must follow agent success,
    # rather than relying on the workflow's separate empty-retrieval branch.
    updates["retrieved_sources"] = [{"source_id": "S1", "text": "Unrelated evidence."}]
    finalizer = next(s for s in workflow["steps"] if s["step_id"] == "finalize_answer")
    payload, missing = _render_input_payload(finalizer["input_mapping_jsonb"], updates)
    assert not missing, missing
    assert payload == {
        "answer": "",
        "cited_source_ids": [],
        "sources": [{"source_id": "S1", "text": "Unrelated evidence."}],
        "abstained": True,
    }
    sys.path.insert(0, str(root))
    from rag import handle

    result = handle("finalize_answer", payload)
    assert result == {
        "answer": "I do not have enough retrieved evidence to answer this question.",
        "sources": [],
        "citation_count": 0,
        "grounded": False,
        "abstained": True,
    }
    return result


if __name__ == "__main__":
    if "--abstention-only" in sys.argv:
        print(json.dumps(validate_abstention(Path(sys.argv[1]))))
    else:
        main(Path(sys.argv[1]))
        validate_abstention(Path(sys.argv[1]))
