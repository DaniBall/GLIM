"""Pruebas unitarias aisladas para migrate_gitlab_group.

No realizan conexiones de red ni ejecutan Git: todos los clientes de GitLab se
sustituyen por dobles mínimos que registran las llamadas efectuadas.
"""

from __future__ import annotations

import base64
import json
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import glim as migration

REQUIRED_ARGS = [
    "--source-url",
    "https://gitlab-source.example.com",
    "--target-url",
    "https://gitlab-target.example.com",
    "--source-group",
    "company/source",
    "--target-group",
    "company/target",
]


def parse_args(*extra: str):
    """Construye argumentos válidos usando el parser real del programa."""
    return migration.build_parser().parse_args([*REQUIRED_ARGS, *extra])


def build_migrator(*extra: str) -> migration.Migrator:
    """Crea un orquestador sin ejecutar preflight ni llamadas externas."""
    return migration.Migrator(parse_args(*extra), "source-token", "target-token")


class ParserDefaultsTests(unittest.TestCase):
    def test_defaults_are_non_destructive(self):
        args = parse_args()

        self.assertFalse(args.execute)
        self.assertEqual(args.existing, "skip")
        self.assertFalse(args.overwrite_ci_variables)
        self.assertFalse(args.resume)
        self.assertFalse(args.allow_http)
        self.assertTrue(args.include_subgroups)
        self.assertTrue(args.with_lfs)
        self.assertTrue(args.with_wikis)
        self.assertFalse(args.include_archived)
        self.assertEqual(args.timeout, 60)
        self.assertEqual(args.git_timeout, 1800)
        self.assertEqual(args.min_free_space_mb, 1024)

    def test_boolean_options_can_be_disabled_explicitly(self):
        args = parse_args("--no-include-subgroups", "--no-with-lfs", "--no-with-wikis")

        self.assertFalse(args.include_subgroups)
        self.assertFalse(args.with_lfs)
        self.assertFalse(args.with_wikis)


class RedactionTests(unittest.TestCase):
    def test_redact_replaces_every_known_secret(self):
        value = "Authorization: alpha; variable=beta; alpha repeated"

        redacted = migration.redact(value, ["alpha", "beta", ""])

        self.assertEqual(redacted.count("[REDACTED_SECRET]"), 3)
        self.assertNotIn("alpha", redacted)
        self.assertNotIn("beta", redacted)


class CheckpointTests(unittest.TestCase):
    def test_checkpoint_v2_records_scope_and_options_without_tokens(self):
        """Comprueba el contenido durable y que nunca incluya credenciales."""
        migrator = build_migrator(
            "--execute",
            "--checkpoint",
            "state.json",
            "--include-archived",
            "--no-with-lfs",
            "--existing",
            "update",
            "--overwrite-ci-variables",
        )
        with (
            patch.object(Path, "write_text") as write_text,
            patch.object(migration.os, "replace"),
        ):
            migrator.checkpoint_phase(
                "company/source/service",
                "repository",
                {"id": 42, "path_with_namespace": "company/target/service"},
                created_by_migrator=True,
                migration_mode="update",
            )

        raw = write_text.call_args.args[0]
        payload = json.loads(raw)

        self.assertEqual(payload["version"], 2)
        self.assertEqual(payload["identity"]["source_group"], "company/source")
        self.assertEqual(payload["identity"]["target_group"], "company/target")
        self.assertEqual(
            payload["identity"]["options"],
            {
                "include_subgroups": True,
                "include_archived": True,
                "with_lfs": False,
                "with_wikis": True,
                "existing": "update",
                "overwrite_ci_variables": True,
                "no_ci_variables": False,
            },
        )
        self.assertEqual(
            payload["projects"]["company/source/service"]["completed_phases"],
            ["repository"],
        )
        self.assertNotIn("source-token", raw)
        self.assertNotIn("target-token", raw)
        self.assertNotIn("GITLAB_SOURCE_TOKEN", raw)
        self.assertNotIn("GITLAB_TARGET_TOKEN", raw)

    def test_no_ci_variables_is_part_of_checkpoint_identity(self):
        """Conserva la exclusión de variables al validar una reanudación."""
        migrator = build_migrator("--no-ci-variables")

        options = migrator.checkpoint_identity()["options"]

        self.assertTrue(options["no_ci_variables"])

    def test_resume_rejects_checkpoint_with_different_options(self):
        original = build_migrator("--execute", "--checkpoint", "state.json")
        saved = {
            "version": 2,
            "identity": original.checkpoint_identity(),
            "projects": {},
        }
        resumed = build_migrator(
            "--execute",
            "--resume",
            "--include-archived",
            "--checkpoint",
            "state.json",
        )
        resumed.checkpoint_path = Mock()
        resumed.checkpoint_path.exists.return_value = True
        resumed.checkpoint_path.read_text.return_value = json.dumps(saved)

        with self.assertRaisesRegex(migration.MigrationError, "otra migración"):
            resumed.load_checkpoint()


class ResultStateTests(unittest.TestCase):
    def test_mark_partial_preserves_failed_and_report_counts_partial(self):
        migrator = build_migrator()
        partial = migration.ItemResult("project", "source/a", "target/a", "ok")
        failed = migration.ItemResult("project", "source/b", "target/b", "failed")

        migrator.mark_partial(partial, "CI incompleto")
        migrator.mark_partial(failed, "fallo adicional")
        migrator.report.items.extend([partial, failed])
        summary = migrator.report.as_dict()["summary"]

        self.assertEqual(partial.status, "partial")
        self.assertEqual(partial.errors, ["CI incompleto"])
        self.assertEqual(failed.status, "failed")
        self.assertEqual(summary["partial"], 1)
        self.assertEqual(summary["failed"], 1)


class CiVariableTests(unittest.TestCase):
    def test_hidden_variable_is_created_as_masked_and_hidden(self):
        migrator = build_migrator()
        migrator.target.paginate = Mock(return_value=[])
        migrator.target.request = Mock(return_value={})
        result = migration.ItemResult("project", "source/a", "target/a", "ok")

        counters = migrator.copy_variables(
            [
                {
                    "key": "DEPLOY_TOKEN",
                    "value": "super-secret-value",
                    "hidden": True,
                    "masked": True,
                    "protected": True,
                    "environment_scope": "production",
                }
            ],
            "/projects/42/variables",
            result,
        )

        self.assertEqual(counters, (1, 0, 0))
        _, call_path = migrator.target.request.call_args.args
        payload = migrator.target.request.call_args.kwargs["data"]
        self.assertEqual(call_path, "/projects/42/variables")
        self.assertTrue(payload["masked_and_hidden"])
        self.assertNotIn("hidden", payload)
        self.assertEqual(result.status, "ok")


class PipelineScheduleTests(unittest.TestCase):
    def test_schedule_is_created_inactive_with_inputs_and_variables(self):
        migrator = build_migrator()
        source_schedule = {
            "id": 7,
            "description": "nightly",
            "ref": "main",
            "cron": "0 2 * * *",
            "cron_timezone": "Europe/Madrid",
        }
        source_detail = {
            **source_schedule,
            "inputs": [{"name": "environment", "value": "staging"}],
            "variables": [{"key": "MODE", "value": "safe", "variable_type": "env_var"}],
        }
        migrator.source.paginate = Mock(return_value=[source_schedule])
        migrator.source.request = Mock(return_value=source_detail)
        migrator.target.paginate = Mock(return_value=[])

        calls = []

        def target_request(method, path, **kwargs):
            calls.append((method, path, kwargs))
            if method == "POST" and path.endswith("/pipeline_schedules"):
                return {"id": 99}
            if method == "GET" and path.endswith("/pipeline_schedules/99"):
                return {
                    "id": 99,
                    "active": False,
                    "cron_timezone": "Europe/Madrid",
                    "inputs": source_detail["inputs"],
                    "variables": [],
                }
            if method == "POST" and path.endswith("/pipeline_schedules/99/variables"):
                return {}
            raise AssertionError(f"Llamada inesperada: {method} {path}")

        migrator.target.request = Mock(side_effect=target_request)
        result = migration.ItemResult("project", "source/a", "target/a", "ok")

        migrator.copy_pipeline_schedules({"id": 1}, {"id": 2}, result)

        creation = next(
            call
            for call in calls
            if call[0] == "POST" and call[1].endswith("/pipeline_schedules")
        )
        self.assertFalse(creation[2]["data"]["active"])
        self.assertEqual(creation[2]["data"]["inputs"], source_detail["inputs"])
        self.assertTrue(
            any(call[1].endswith("/pipeline_schedules/99/variables") for call in calls)
        )
        self.assertEqual(result.status, "ok")
        self.assertIn("1 schedules reconciliadas e inactivas", result.details)


class RunnerValidationTests(unittest.TestCase):
    def test_ci_becomes_partial_when_no_online_runner_matches_job_tags(self):
        migrator = build_migrator()
        migrator.target.get_project = Mock(
            return_value={
                "id": 2,
                "default_branch": "main",
                "ci_config_path": ".gitlab-ci.yml",
            }
        )
        encoded_ci = base64.b64encode(b"build:\n  script: echo ok\n").decode("ascii")

        def target_request(method, path, **kwargs):
            if method == "GET" and "/repository/files/" in path:
                return {"content": encoded_ci}
            if method == "POST" and path.endswith("/ci/lint"):
                return {
                    "valid": True,
                    "jobs": [{"name": "deploy", "tag_list": ["docker"]}],
                }
            raise AssertionError(f"Llamada inesperada: {method} {path}")

        migrator.target.request = Mock(side_effect=target_request)
        migrator.target.paginate = Mock(
            return_value=[
                {"paused": False, "tag_list": ["shell"], "run_untagged": True}
            ]
        )
        result = migration.ItemResult("project", "source/a", "target/a", "ok")

        migrator.validate_ci({"id": 2}, result)

        self.assertEqual(result.status, "partial")
        self.assertTrue(
            any("Jobs sin runner online compatible" in error for error in result.errors)
        )
        migrator.target.paginate.assert_called_once_with(
            "/projects/2/runners", query={"status": "online"}
        )


if __name__ == "__main__":
    unittest.main()
