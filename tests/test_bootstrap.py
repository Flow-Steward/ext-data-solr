"""The shipped host entrypoint must perform a real Solr action."""

from pathlib import Path
import importlib.util


def test_solr_host_entrypoint_implements_catalog():
    root = Path(__file__).resolve().parents[1]
    path = root / "main.py"
    assert path.is_file(), "Solr host entrypoint is not implemented"
    spec = importlib.util.spec_from_file_location("main", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert callable(getattr(module, "handle", None)), "Solr catalog action is not implemented"
    result = module.handle(
        {
            "contract_version": "extension_host_v2",
            "mode": "query",
            "runtime_context": {
                "account_id": None,
                "project_id": None,
                "workflow_id": None,
                "job_id": None,
                "step_id": None,
                "step_execution_id": None,
                "group_id": None,
                "for_each_id": None,
                "test_mode": False,
            },
            "query": {"query_id": "supported_connectors"},
        }
    )
    assert result["ok"], result
    assert result["result"]["connectors"][0]["connector_id"] == "solr"
