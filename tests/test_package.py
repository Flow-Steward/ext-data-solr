"""Build and inspect actual deterministic pure Python installer archives."""

from __future__ import annotations
import hashlib
import importlib.util
import json
from pathlib import Path
import zipfile


def test_deterministic_architecture_labelled_archives(tmp_path):
    root = Path(__file__).resolve().parents[1]
    spec = importlib.util.spec_from_file_location(
        "package_extension", root / "scripts/package_extension.py"
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    hashes = []
    for target in ["linux-amd64", "linux-arm64"]:
        a = module.package_extension(root, target, tmp_path / "one")
        b = module.package_extension(root, target, tmp_path / "two")
        assert a.read_bytes() == b.read_bytes()
        assert a.stat().st_size <= 50 * 1024 * 1024
        hashes.append(hashlib.sha256(a.read_bytes()).hexdigest())
        with zipfile.ZipFile(a) as archive:
            names = archive.namelist()
            assert all(n.startswith("flowsteward_solr/") for n in names)
            assert json.loads(archive.read("flowsteward_solr/.fs-package.yaml")) == {
                "runtime_target": target
            }
            assert sum(i.file_size for i in archive.infolist()) <= 150 * 1024 * 1024
            # Public catalog discovery consumes extension.yaml, not the descriptor.
            manifest = json.loads(archive.read("flowsteward_solr/extension.yaml"))
            descriptor = json.loads(archive.read("flowsteward_solr/extension-descriptor.yaml"))
            assert "dataset_connector" in manifest["features"]
            host_ids = {
                "test_connection",
                "introspect_schema",
                "validate_query",
                "preview_query",
                "execute_template",
                "infer_output_schema",
            }
            actions = json.loads(archive.read("flowsteward_solr/ui/actions/actions.yaml"))[
                "actions"
            ]
            declared = {a["action_id"]: a for a in actions}
            assert len(declared) == len(actions)
            assert host_ids <= declared.keys()
            assert host_ids.isdisjoint(
                {effect["operation_id"] for effect in manifest["external_effects"]}
            )
            for host_id in host_ids:
                host_action = declared[host_id]
                assert host_action["workflow_visible"] is False
                assert host_action["mutates_platform"] is False
                assert host_action["handler"] == {"mode": "extension"}
                assert {p["name"] for p in host_action["parameters"]} == {
                    "connection_config",
                    "secret_refs",
                    "dataset",
                    "query_template",
                    "params",
                    "limits",
                }
            assert "result.document_id" not in declared["upsert_documents"]["result_fields"]
            operations = json.loads(
                archive.read("flowsteward_solr/contracts/operation_manifest.yaml")
            )["operations"]
            batch = next(op for op in operations if op["operation_id"] == "upsert_documents")
            assert "document_id" not in {o["name"] for o in batch["outputs"]}
            assert "result.document_id" not in batch["result_fields"]
            public_contract = manifest["runtime"]["extension_contract_v2"]
            native_contract = descriptor["runtime"]["extension_contract_v2"]
            assert public_contract["dataset_connectors"] == native_contract["dataset_connectors"]
            assert (
                public_contract["resource_query_manifest"]
                == native_contract["resource_query_manifest"]
            )
            connector = public_contract["dataset_connectors"][0]
            assert connector["connector_id"] == "solr"
            assert connector["query_language"] == "json" and connector["access_mode"] == "read_only"
            assert {q["query_id"] for q in public_contract["resource_query_manifest"]} == {
                "runtime_health",
                "supported_connectors",
            }
            assert "flowsteward_solr/artifacts/workflows/solr_ask.yaml" in names
            assert not any(
                "/.git/" in n or "/dev-wheels/" in n or "/dist/" in n or "/__pycache__/" in n
                for n in names
            )
            assert not any(archive.read(n).startswith(b"\x7fELF") for n in names)
    assert hashes[0] != hashes[1]


def test_packaged_typed_dataset_query_dispatches_through_actual_core(tmp_path):
    import os
    import subprocess

    root = Path(__file__).resolve().parents[1]
    spec = importlib.util.spec_from_file_location(
        "dataset_dispatch_package", root / "scripts/package_extension.py"
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    import platform

    target = {"aarch64": "linux-arm64", "arm64": "linux-arm64", "x86_64": "linux-amd64"}[
        platform.machine().lower()
    ]
    archive = module.package_extension(root, target, tmp_path)
    result = subprocess.run(
        ["/usr/local/bin/python", str(root / "scripts/validate_dataset_dispatch.py"), str(archive)],
        env={**os.environ, "PYTHONPATH": "/app", "FS_STACK_ENVIRONMENT": "development"},
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
