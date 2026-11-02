"""Admit an actual bundle ZIP and dispatch a canonical typed Dataset host action."""

from pathlib import Path
from types import SimpleNamespace
from typing import Any
import sys
from core.application.extensions.archive_extraction_service import (
    ExtensionArchiveExtractionService,
    safe_extension_archive_file_mode,
)
from core.application.extensions.archive_validation_service import ExtensionArchiveValidationService
from core.application.extensions.command_core import ExtensionCommandCore
from core.application.extensions.lifecycle import validate_extension_descriptor_file
from core.infrastructure.extension_runtime.extension_runtime import run_extension_action


def main(archive_path):
    # Admission uses only the command validator; lifecycle callbacks are unused.
    unused: Any = None
    context: Any = SimpleNamespace()
    projection: Any = SimpleNamespace(
        extension_version_from_payload=lambda payload: payload["version"]
    )
    command = ExtensionCommandCore(
        context=unused, install_release=unused, cleanup_release_artifacts=lambda _: None
    )
    context.command_service = lambda _: command
    validator = ExtensionArchiveValidationService(
        context=context,
        projection=projection,
    )
    upload = validator.validate_upload(
        filename=archive_path.name,
        content=archive_path.read_bytes(),
        max_archive_bytes=50 * 1024 * 1024,
    )
    extractor = ExtensionArchiveExtractionService(file_mode=safe_extension_archive_file_mode)
    with extractor.extract(upload=upload, max_uncompressed_bytes=150 * 1024 * 1024) as extracted:
        descriptor_path = next(extracted.rglob("extension-descriptor.yaml"))
        descriptor = validate_extension_descriptor_file(descriptor_path)
        assert descriptor["valid"], descriptor["errors"]
        bundle = validator.validate_extracted_bundle(extracted_root=extracted, bl=None, scope=None)
        payload = {
            "connection_config": {"origin": "https://solr.example", "collections": ["chunks"]},
            "secret_refs": {"bearer_token": "dummy"},
            "dataset": {
                "dataset_id": "dispatch-dataset",
                "resource_scope_jsonb": {
                    "resources": [{"resource_id": "chunks", "columns": ["value"]}]
                },
            },
            "query_template": {
                "query_template_id": "dispatch-query",
                "dataset_id": "dispatch-dataset",
                "query_language": "json",
                "query_definition_jsonb": {
                    "resource_id": "chunks",
                    "operation": "search",
                    "arguments": {
                        "query": {"term": {"value": {"$param": "value"}}},
                        "source_fields": ["value"],
                    },
                },
                "param_schema_jsonb": {"properties": {"value": {"type": "boolean"}}},
            },
            "params": {"value": False},
            "limits": {"max_preview_rows": 10, "max_runtime_rows": 10, "timeout_seconds": 5},
        }
        invocation = run_extension_action(
            bundle.compiled,
            action_id="validate_query",
            input_payload=payload,
            timeout_seconds=5,
            runtime_context={"extension_install_id": "dispatch-install"},
        )
        assert invocation["status"] == "succeeded", invocation
        response = invocation["response"]
        assert response["ok"] is True, response
        assert response["result"] == {
            "valid": True,
            "read_only": True,
            "validation_complete": True,
            "referenced_resources": ["chunks"],
        }
        # Other native actions need a provider to succeed. Invalid connection
        # values prove Core admits each action and reaches the provider's own
        # validation, while remaining unable to make any database request.
        for action_id in (
            "test_connection",
            "introspect_schema",
            "preview_query",
            "execute_template",
            "infer_output_schema",
        ):
            rejected = run_extension_action(
                bundle.compiled,
                action_id=action_id,
                input_payload={"connection_config": {}, "secret_refs": {}},
                timeout_seconds=5,
                runtime_context={"extension_install_id": "dispatch-install"},
            )
            assert rejected["response"].get("error_code") == "invalid_connection", rejected
    print("Core packaged typed validate_query succeeded; all six Dataset host actions admitted")


if __name__ == "__main__":
    main(Path(sys.argv[1]))
