"""Synthetic fixtures only; no cluster commands, credentials or private inputs."""
import contextlib
import copy
import importlib.util
import io
import json
import os
from pathlib import Path
import stat
import tempfile
import unittest
from unittest import mock

SPEC = importlib.util.spec_from_file_location("k3s_rollout", Path(__file__).parents[1] / "scripts" / "k3s-rollout.py")
rollout = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(rollout)


def fixture(replicas=0):
    return {"apiVersion": "apps/v1", "kind": "Deployment",
            "metadata": {"name": "synthetic", "namespace": "synthetic", "uid": "synthetic-uid",
                         "resourceVersion": "42", "generation": 3},
            "spec": {"replicas": replicas, "template": {"spec": {
                "containers": [{"name": "app", "image": "synthetic/app:1", "volumeMounts": [{"name": "data", "mountPath": "/appdata"}]},
                               {"name": "backup", "image": "legacy/restic:1", "env": [
                                   {"name": "RESTIC_PASSWORD", "value": "SYNTHETIC_PRIVATE_SENTINEL"},
                                   {"name": "DATABASE_TYPE", "value": "files"},
                                   {"name": "BACKUP_CRON", "value": "0 */6 * * *"},
                                   {"name": "TOKEN", "valueFrom": {"secretKeyRef": {"name": "synthetic-secret", "key": "password"}}}],
                                "envFrom": [{"configMapRef": {"name": "synthetic-config"}}],
                                "volumeMounts": [{"name": "data", "mountPath": "/data"}]}],
                "initContainers": [{"name": "restore", "image": "legacy/restic:1", "envFrom": [{"configMapRef": {"name": "synthetic-config"}}], "volumeMounts": [{"name": "data", "mountPath": "/data"}]}],
                "volumes": [{"name": "data", "persistentVolumeClaim": {"claimName": "synthetic"}}]}}}}


def bundle(replicas=0):
    return {"controller": fixture(replicas), "configmaps": [{"apiVersion": "v1", "kind": "ConfigMap",
            "metadata": {"name": "synthetic-config", "namespace": "synthetic", "uid": "cm-uid", "resourceVersion": "4"},
            "data": {"private": "SYNTHETIC_CONFIG_SENTINEL"}}]}


class BackupTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="k3s-rollout-unit-")
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.root = self.base / "private"
        self.controller, self.configmaps, self.refs = rollout.unpack(bundle())

    def error(self, code, callback):
        with self.assertRaises(rollout.ToolError) as raised:
            callback()
        self.assertEqual(raised.exception.code, code)

    def backup(self):
        return rollout.backup_bundle(self.controller, self.configmaps, self.refs, self.root)

    def test_backup_complete_private_original_and_hashes(self):
        report = self.backup()
        target = self.root / report["backup_id"]
        for path in (self.root, target):
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o700)
            self.assertEqual(path.stat().st_uid, 0)
        for name in rollout.PRIVATE_FILES:
            self.assertEqual(stat.S_IMODE((target / name).stat().st_mode), 0o600)
        original = json.loads((target / "controller.json").read_bytes())
        self.assertEqual(original, self.controller)
        index = json.loads((target / "index.json").read_bytes())
        self.assertEqual(index["replicas"], 0)
        self.assertEqual(index["controller"]["resourceVersion"], "42")
        self.assertEqual(index["files"]["controller.json"], rollout.digest(self.controller))
        self.assertEqual(index["secret_references_only"], [{"name": "synthetic-secret", "key": "password"}])
        public = json.dumps(report)
        self.assertNotIn("SYNTHETIC_PRIVATE_SENTINEL", public)
        self.assertNotIn("SYNTHETIC_CONFIG_SENTINEL", public)

    def test_unique_backups_never_overwrite(self):
        first, second = self.backup(), self.backup()
        self.assertNotEqual(first["backup_id"], second["backup_id"])
        self.assertTrue((self.root / first["backup_id"] / "index.json").exists())

    def test_mode_or_symlink_private_root_rejected(self):
        self.root.mkdir(mode=0o755)
        self.root.chmod(0o755)
        self.error("private_directory_must_be_root_0700", self.backup)
        self.root.chmod(0o700)
        alias = self.base / "alias"
        alias.symlink_to(self.root, target_is_directory=True)
        with self.assertRaises(OSError):
            rollout.backup_bundle(self.controller, self.configmaps, self.refs, alias)

    def test_nonroot_cannot_create_private_backup(self):
        with mock.patch.object(rollout.os, "geteuid", return_value=65534):
            self.error("root_required_for_private_backup", self.backup)
        self.assertFalse(self.root.exists())

    def test_secret_payload_and_cross_namespace_configmap_refused(self):
        candidate = bundle()
        candidate["configmaps"].append({"kind": "Secret", "data": {"x": "do-not-read"}})
        self.error("secret_or_unsupported_object_refused", lambda: rollout.unpack(candidate))
        candidate = bundle()
        candidate["configmaps"][0]["metadata"]["namespace"] = "other"
        self.error("configmap_namespace_reference_or_duplicate", lambda: rollout.unpack(candidate))

    def test_missing_configmap_is_recorded_no_go_without_secret(self):
        controller, configmaps, refs = rollout.unpack(fixture())
        report = rollout.plan(controller, configmaps, refs)
        self.assertEqual(report["status"], "NO_GO")
        self.assertEqual(report["missing_configmap_count"], 1)
        self.assertNotIn("SYNTHETIC_PRIVATE_SENTINEL", json.dumps(report))

    def test_cli_backup_silent_for_literals_commands_and_config(self):
        source = self.base / "synthetic-input.json"
        source.write_bytes(rollout.canonical(bundle()))
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            result = rollout.main(["backup", "--input", str(source), "--backup-root", str(self.root)])
        self.assertEqual(result, 0)
        self.assertNotIn("SYNTHETIC_PRIVATE_SENTINEL", output.getvalue())
        self.assertNotIn("SYNTHETIC_CONFIG_SENTINEL", output.getvalue())
        self.assertEqual(json.loads(output.getvalue())["status"], "BACKED_UP")

    def test_duplicate_json_fields_rejected_safely(self):
        source = self.base / "duplicate.json"
        source.write_bytes(b'{"kind":"Secret","kind":"Deployment"}')
        self.error("duplicate_json_field", lambda: rollout.load_input(source))

    def test_existing_atomic_filename_never_overwrites(self):
        self.root.mkdir(mode=0o700)
        fd = rollout.private_root(self.root)
        try:
            rollout.write_exclusive(fd, "same.json", {"first": True})
            with self.assertRaises(FileExistsError):
                rollout.write_exclusive(fd, "same.json", {"second": True})
            self.assertEqual(json.loads((self.root / "same.json").read_bytes()), {"first": True})
        finally:
            os.close(fd)

    def test_multiple_controllers_and_missing_identity_are_refused(self):
        candidate = {"kind": "List", "items": [fixture(), fixture()]}
        self.error("exactly_one_controller_required", lambda: rollout.unpack(candidate))
        candidate = fixture()
        del candidate["metadata"]["resourceVersion"]
        self.error("controller_identity_required", lambda: rollout.unpack(candidate))


class PatchTests(BackupTests):
    def evidence(self, sha):
        record = {"schema_version": 1, "approved": True, "restore_passed": True,
                "signature_verified": True, "signature_evidence_sha256": "1" * 64,
                "restore_evidence_sha256": "2" * 64,
                "controller_sha256": rollout.digest(self.controller), "uid": "synthetic-uid",
                "resource_version": "42", "backup_index_sha256": sha, "desired_replicas": 0,
                "public_image": "ghcr.io/synthetic/backup@sha256:" + "3" * 64,
                "dataset_id": "synthetic-files", "engine": "files", "sidecar_name": "backup",
                "init_name": "restore", "data_volume": "data", "source_data_path": "/data",
                "recovery_data_path": "/data", "work_path": "/backup", "work_volume": "restic-work",
                "scratch_size_limit": "64Mi", "backup_max_bytes": 1024 * 1024,
                "backup_min_free_bytes": 1024 * 1024,
                "resources": {"limits": {"cpu": "500m", "memory": "512Mi"}}}
        return record

    def stateful_claim_fixture(self):
        controller = copy.deepcopy(self.controller)
        controller["kind"] = "StatefulSet"
        controller["spec"]["volumeClaimTemplates"] = [{"metadata": {"name": "data"},
            "spec": {"accessModes": ["ReadWriteOnce"], "volumeMode": "Filesystem",
                     "resources": {"requests": {"storage": "4Gi"}}}}]
        controller["spec"]["template"]["spec"]["volumes"] = [
            {"name": "data-o", "hostPath": {"path": "/synthetic-unused"}}]
        evidence = self.evidence("a" * 64)
        evidence.update(controller_sha256=rollout.digest(controller), scratch_size_limit="2Gi",
                        backup_max_bytes=1024**3, backup_min_free_bytes=24 * 1024**3,
                        resources={"limits": {"cpu": "500m", "memory": "4Gi"}})
        return controller, evidence

    def test_root_context_inheritance_conflict_requires_review_and_scoped_override(self):
        for location in ("pod", "container", "both"):
            with self.subTest(location=location):
                controller = copy.deepcopy(self.controller)
                original_pod = controller["spec"]["template"]["spec"]
                if location in {"pod", "both"}:
                    original_pod["securityContext"] = {"runAsNonRoot": True, "runAsUser": 1234, "fsGroup": 1234}
                if location in {"container", "both"}:
                    for item in (original_pod["containers"][1], original_pod["initContainers"][0]):
                        item["securityContext"] = {"runAsNonRoot": True, "readOnlyRootFilesystem": True}
                evidence = self.evidence("a" * 64)
                evidence["controller_sha256"] = rollout.digest(controller)
                self.error("root_restore_security_context_requires_review", lambda: rollout.make_patch(
                    controller, self.configmaps, self.refs, evidence, "a" * 64))
                evidence["root_context_override_reviewed"] = True
                patch = rollout.make_patch(controller, self.configmaps, self.refs, evidence, "a" * 64)
                modified = next(item["value"]["spec"] for item in patch if item["op"] == "replace" and item["path"] == "/spec/template")
                self.assertEqual(modified.get("securityContext"), original_pod.get("securityContext"))
                self.assertEqual(modified["containers"][0], original_pod["containers"][0])
                for item in (modified["containers"][1], modified["initContainers"][0]):
                    self.assertEqual(item["securityContext"]["runAsUser"], 0)
                    self.assertIs(item["securityContext"]["runAsNonRoot"], False)
                if location in {"container", "both"}:
                    self.assertTrue(modified["containers"][1]["securityContext"]["readOnlyRootFilesystem"])

    def test_explicit_false_container_nonroot_overrides_true_pod_without_new_approval(self):
        controller = copy.deepcopy(self.controller)
        original = controller["spec"]["template"]["spec"]
        original["securityContext"] = {"runAsNonRoot": True}
        for item in (original["containers"][1], original["initContainers"][0]):
            item["securityContext"] = {"runAsNonRoot": False}
        evidence = self.evidence("a" * 64)
        evidence["controller_sha256"] = rollout.digest(controller)
        patch = rollout.make_patch(controller, self.configmaps, self.refs, evidence, "a" * 64)
        modified = next(item["value"]["spec"] for item in patch if item["op"] == "replace" and item["path"] == "/spec/template")
        self.assertEqual(modified["securityContext"], {"runAsNonRoot": True})
        self.assertIs(modified["containers"][1]["securityContext"]["runAsNonRoot"], False)

    def test_files_source_target_aliases_roundtrip_through_runtime_config(self):
        from k3s_backup.config import Config
        for existing in (False, True):
            with self.subTest(existing=existing):
                controller = copy.deepcopy(self.controller)
                pod = controller["spec"]["template"]["spec"]
                pod["containers"][1]["volumeMounts"][0]["mountPath"] = "/source-files"
                if existing:
                    pod["containers"][1]["env"].extend([{"name": "DATA_PATH", "value": "/source-files"},
                                                         {"name": "RECOVERY_DATA_PATH", "value": "/source-files"}])
                    pod["initContainers"][0]["env"] = [{"name": "DATA_PATH", "value": "/data"},
                                                        {"name": "RECOVERY_DATA_PATH", "value": "/data"}]
                evidence = self.evidence("a" * 64)
                evidence.update(source_data_path="/source-files", controller_sha256=rollout.digest(controller))
                patch = rollout.make_patch(controller, self.configmaps, self.refs, evidence, "a" * 64)
                modified = next(item["value"]["spec"] for item in patch if item["op"] == "replace" and item["path"] == "/spec/template")
                for item, expected in ((modified["containers"][1], "/source-files"), (modified["initContainers"][0], "/data")):
                    env = {entry["name"]: entry["value"] for entry in item["env"] if "value" in entry}
                    env["RESTIC_REPOSITORY"] = "/synthetic-repository-not-read"
                    config = Config.from_env(env)
                    self.assertEqual(config.data_path, Path(expected))
                    self.assertEqual(env["DATA_PATH"], env["RECOVERY_DATA_PATH"])
                self.assertEqual(controller["spec"]["template"]["spec"], pod)

    def test_conflicting_existing_file_recovery_alias_is_not_silently_replaced(self):
        controller = copy.deepcopy(self.controller)
        controller["spec"]["template"]["spec"]["containers"][1]["volumeMounts"][0]["mountPath"] = "/source-files"
        controller["spec"]["template"]["spec"]["containers"][1]["env"].append({"name": "RECOVERY_DATA_PATH", "value": "/data"})
        evidence = self.evidence("a" * 64)
        evidence.update(source_data_path="/source-files", controller_sha256=rollout.digest(controller))
        self.error("recovery_path_environment_conflict", lambda: rollout.make_patch(controller, self.configmaps, self.refs, evidence, "a" * 64))
        self.assertEqual(controller["spec"]["template"]["spec"]["containers"][1]["env"][-1]["value"], "/data")

    def test_file_sidecar_dynamic_subpath_and_propagation_refused(self):
        for field, value in (("subPathExpr", "$(TENANT)"), ("subPathExpr", ""),
                             ("mountPropagation", "HostToContainer"), ("mountPropagation", "None")):
            with self.subTest(field=field, value=value):
                controller = copy.deepcopy(self.controller)
                controller["spec"]["template"]["spec"]["containers"][1]["volumeMounts"][0][field] = value
                evidence = self.evidence("a" * 64)
                evidence["controller_sha256"] = rollout.digest(controller)
                self.error("file_source_mount_mapping_mismatch", lambda: rollout.make_patch(controller, self.configmaps, self.refs, evidence, "a" * 64))

    def test_stateful_implicit_claim_works_without_explicit_data_volume(self):
        controller, evidence = self.stateful_claim_fixture()
        before = copy.deepcopy(controller)
        report = rollout.inspect_plan(controller, self.configmaps, evidence)
        self.assertEqual(report["data_volume_source"], "claim_template")
        self.assertEqual(report["status"], "MAPPING_REVIEWED_NOT_APPROVED")
        patch = rollout.make_patch(controller, self.configmaps, self.refs, evidence, "a" * 64)
        pod = next(item["value"]["spec"] for item in patch if item["op"] == "replace" and item["path"] == "/spec/template")
        self.assertEqual(pod["volumes"][0], {"name": "data-o", "hostPath": {"path": "/synthetic-unused"}})
        self.assertFalse(any(volume["name"] == "data" for volume in pod["volumes"]))
        self.assertEqual(pod["volumes"][-1]["emptyDir"]["sizeLimit"], "2Gi")
        self.assertEqual(controller, before)
        self.assertFalse(any(item["path"].startswith("/spec/volumeClaimTemplates") for item in patch))
        self.assertEqual(patch[-1], {"op": "replace", "path": "/spec/replicas", "value": 0})

    def test_stateful_implicit_claim_without_any_pod_volumes(self):
        controller, evidence = self.stateful_claim_fixture()
        del controller["spec"]["template"]["spec"]["volumes"]
        evidence["controller_sha256"] = rollout.digest(controller)
        patch = rollout.make_patch(controller, self.configmaps, self.refs, evidence, "a" * 64)
        pod = next(item["value"]["spec"] for item in patch if item["op"] == "replace" and item["path"] == "/spec/template")
        self.assertEqual(pod["volumes"], [{"name": "restic-work", "emptyDir": {"sizeLimit": "2Gi"}}])

    def test_same_named_hostpath_shadowed_by_claim_requires_review_not_hostpath_relayout(self):
        controller, evidence = self.stateful_claim_fixture()
        shadowed = {"name": "data", "hostPath": {"path": "/synthetic-unused-shadow"}}
        controller["spec"]["template"]["spec"]["volumes"].append(shadowed)
        evidence["controller_sha256"] = rollout.digest(controller)
        self.error("claim_template_shadowed_volume_requires_review", lambda: rollout.make_patch(
            controller, self.configmaps, self.refs, evidence, "a" * 64))
        evidence["claim_template_shadowed_volume_reviewed"] = True
        patch = rollout.make_patch(controller, self.configmaps, self.refs, evidence, "a" * 64)
        pod = next(item["value"]["spec"] for item in patch if item["op"] == "replace" and item["path"] == "/spec/template")
        self.assertIn(shadowed, pod["volumes"])
        self.assertNotIn("hostpath_reviewed", evidence)
        self.assertEqual(rollout.data_volume_source(controller, pod, "data")[0], "claim_template")

    def test_claim_name_mismatch_duplicate_block_mode_and_deployment_are_refused(self):
        for case, code in (("mismatch", "explicit_single_supported_data_volume_required"),
                           ("duplicate", "ambiguous_or_nonstateful_claim_template"),
                           ("block", "filesystem_claim_template_required"),
                           ("deployment", "ambiguous_or_nonstateful_claim_template")):
            with self.subTest(case=case):
                controller, evidence = self.stateful_claim_fixture()
                if case == "mismatch":
                    controller["spec"]["volumeClaimTemplates"][0]["metadata"]["name"] = "other"
                elif case == "duplicate":
                    controller["spec"]["volumeClaimTemplates"] *= 2
                elif case == "block":
                    controller["spec"]["volumeClaimTemplates"][0]["spec"]["volumeMode"] = "Block"
                else:
                    controller["kind"] = "Deployment"
                evidence["controller_sha256"] = rollout.digest(controller)
                self.error(code, lambda: rollout.make_patch(controller, self.configmaps, self.refs, evidence, "a" * 64))

    def test_scratch_cannot_shadow_implicit_claim(self):
        controller, evidence = self.stateful_claim_fixture()
        controller["spec"]["volumeClaimTemplates"].append({"metadata": {"name": "restic-work"}, "spec": {}})
        evidence["controller_sha256"] = rollout.digest(controller)
        self.error("scratch_volume_name_collision", lambda: rollout.make_patch(controller, self.configmaps, self.refs, evidence, "a" * 64))

    def test_verified_backup_gate_detects_changed_resource_and_original(self):
        report = self.backup()
        directory = self.root / report["backup_id"]
        rollout.verify_backup(self.controller, self.configmaps, self.refs, directory, report["index_sha256"])
        changed = copy.deepcopy(self.controller)
        changed["metadata"]["resourceVersion"] = "43"
        self.error("backup_sha_or_current_object_mismatch", lambda: rollout.verify_backup(
            changed, self.configmaps, self.refs, directory, report["index_sha256"]))
        self.error("backup_sha_or_current_object_mismatch", lambda: rollout.verify_backup(
            self.controller, self.configmaps, self.refs, directory, "f" * 64))
        (directory / "controller.json").write_bytes(b"{}")
        self.error("backup_sha_or_current_object_mismatch", lambda: rollout.verify_backup(
            self.controller, self.configmaps, self.refs, directory, report["index_sha256"]))

    def test_offline_patch_guards_and_zero_replica_preservation(self):
        report = self.backup()
        evidence = self.evidence(report["index_sha256"])
        patch = rollout.make_patch(self.controller, self.configmaps, self.refs, evidence, report["index_sha256"])
        self.assertEqual(patch[0], {"op": "test", "path": "/metadata/uid", "value": "synthetic-uid"})
        self.assertEqual(patch[1], {"op": "test", "path": "/metadata/resourceVersion", "value": "42"})
        self.assertIn({"op": "test", "path": "/spec/replicas", "value": 0}, patch)
        self.assertEqual(patch[-1], {"op": "replace", "path": "/spec/replicas", "value": 0})
        pod = next(item["value"]["spec"] for item in patch if item["path"] == "/spec/template" and item["op"] == "replace")
        sidecar = pod["containers"][1]
        init = pod["initContainers"][0]
        self.assertEqual(sidecar["command"], ["python3", "-m", "k3s_backup", "schedule"])
        self.assertEqual(init["command"], ["python3", "-m", "k3s_backup", "restore-init"])
        self.assertTrue(sidecar["volumeMounts"][0]["readOnly"])
        self.assertEqual(sidecar["volumeMounts"][-1], init["volumeMounts"][-1])
        self.assertEqual(pod["volumes"][-1], {"name": "restic-work", "emptyDir": {"sizeLimit": "64Mi"}})
        for container in (init, sidecar):
            env = {entry["name"]: entry for entry in container["env"]}
            self.assertEqual(env["RESTIC_PASSWORD"]["value"], "SYNTHETIC_PRIVATE_SENTINEL")
            self.assertEqual(env["BACKUP_CRON"]["value"], "0 */6 * * *")
            self.assertEqual(env["TOKEN"]["valueFrom"], self.controller["spec"]["template"]["spec"]["containers"][1]["env"][-1]["valueFrom"])
        self.assertEqual(pod["containers"][0], self.controller["spec"]["template"]["spec"]["containers"][0])

    def test_unapproved_unsigned_failed_restore_and_foreign_identity_rejected(self):
        report = self.backup()
        for key, value, code in (("approved", False, "approval_signature_and_restore_proof_required"),
                                  ("restore_passed", False, "approval_signature_and_restore_proof_required"),
                                  ("signature_verified", False, "approval_signature_and_restore_proof_required"),
                                  ("uid", "other", "approval_identity_or_backup_mismatch"),
                                  ("public_image", "synthetic:latest", "approved_public_digest_required"),
                                  ("desired_replicas", 1, "approval_desired_replicas_mismatch")):
            with self.subTest(key=key):
                evidence = self.evidence(report["index_sha256"])
                evidence[key] = value
                self.error(code, lambda: rollout.make_patch(self.controller, self.configmaps, self.refs, evidence, report["index_sha256"]))

    def test_mount_hook_and_scratch_conflicts_refused(self):
        sha = "a" * 64
        cases = (("hook", "unknown_hook_or_probe_requires_manual_review"),
                 ("path", "recovery_path_must_match_existing_writable_init_mount"),
                 ("scratch", "scratch_and_data_paths_overlap"),
                 ("subpath", "application_and_restore_subpath_mismatch"))
        for kind, code in cases:
            with self.subTest(kind=kind):
                controller = copy.deepcopy(self.controller)
                evidence = self.evidence(sha)
                if kind == "hook":
                    controller["spec"]["template"]["spec"]["initContainers"][0]["lifecycle"] = {"postStart": {"exec": {"command": ["unsafe"]}}}
                elif kind == "path":
                    evidence["recovery_data_path"] = "/db-source"
                elif kind == "scratch":
                    evidence["work_path"] = "/data/work"
                else:
                    controller["spec"]["template"]["spec"]["containers"][0]["volumeMounts"][0]["subPath"] = "other"
                evidence["controller_sha256"] = rollout.digest(controller)
                self.error(code, lambda: rollout.make_patch(controller, self.configmaps, self.refs, evidence, sha))

    def test_secret_envfrom_preserved_only_after_explicit_opaque_review(self):
        controller = copy.deepcopy(self.controller)
        for container in (controller["spec"]["template"]["spec"]["containers"][1], controller["spec"]["template"]["spec"]["initContainers"][0]):
            container["envFrom"] = [{"secretRef": {"name": "synthetic-secret"}}]
        evidence = self.evidence("a" * 64)
        evidence["controller_sha256"] = rollout.digest(controller)
        self.error("opaque_secret_envfrom_requires_manual_review", lambda: rollout.make_patch(controller, [], self.refs, evidence, "a" * 64))
        evidence.update(opaque_secret_envfrom_reviewed=True, unresolved_secret_env_conflicts_accepted=True)
        patch = rollout.make_patch(controller, [], self.refs, evidence, "a" * 64)
        pod = next(item["value"]["spec"] for item in patch if item["op"] == "replace" and item["path"] == "/spec/template")
        self.assertEqual(pod["containers"][1]["envFrom"], [{"secretRef": {"name": "synthetic-secret"}}])

    def test_legacy_commands_require_exact_digest_review(self):
        controller = copy.deepcopy(self.controller)
        controller["spec"]["template"]["spec"]["initContainers"][0]["command"] = ["sh", "-c", "SYNTHETIC_PRIVATE_COMMAND"]
        evidence = self.evidence("a" * 64)
        evidence["controller_sha256"] = rollout.digest(controller)
        self.error("legacy_commands_require_digest_bound_review", lambda: rollout.make_patch(controller, self.configmaps, self.refs, evidence, "a" * 64))
        evidence["legacy_commands_reviewed"] = True
        evidence["legacy_commands_sha256"] = rollout.digest(rollout.legacy_commands(controller, "backup", "restore"))
        patch = rollout.make_patch(controller, self.configmaps, self.refs, evidence, "a" * 64)
        self.assertTrue(patch)

    def test_unknown_hooks_and_conflicting_data_environment_refused(self):
        for name, value, code in (("PRE_BACKUP_COMMAND", "custom", "unknown_environment_hook_requires_manual_review"),
                                  ("DATA_PATH", "/different", "data_path_environment_conflict"),
                                  ("BACKUP_WORK_PATH", "/data/inside", "work_path_environment_conflict")):
            with self.subTest(name=name):
                controller = copy.deepcopy(self.controller)
                controller["spec"]["template"]["spec"]["containers"][1]["env"].append({"name": name, "value": value})
                evidence = self.evidence("a" * 64)
                evidence["controller_sha256"] = rollout.digest(controller)
                self.error(code, lambda: rollout.make_patch(controller, self.configmaps, self.refs, evidence, "a" * 64))

    def test_unsupported_engine_and_pgdata_layout_are_not_relabelled(self):
        evidence = self.evidence("a" * 64)
        evidence["engine"] = "mongodb"
        self.error("explicit_dataset_engine_required", lambda: rollout.make_patch(self.controller, self.configmaps, self.refs, evidence, "a" * 64))
        controller = copy.deepcopy(self.controller)
        controller["spec"]["template"]["spec"]["containers"][1]["env"][1]["value"] = "postgres"
        controller["spec"]["template"]["spec"]["containers"][0]["env"] = [{"name": "PGDATA", "value": "/appdata/pgdata"}]
        evidence = self.evidence("a" * 64)
        evidence.update(engine="postgres", target_major="17", controller_sha256=rollout.digest(controller))
        self.error("pgdata_environment_requires_explicit_relayout", lambda: rollout.make_patch(controller, self.configmaps, self.refs, evidence, "a" * 64))

    def test_plan_mapping_does_not_require_or_fabricate_restore_evidence(self):
        evidence = self.evidence("a" * 64)
        for name in ("approved", "restore_passed", "signature_verified"):
            evidence.pop(name)
        report = rollout.inspect_plan(self.controller, self.configmaps, evidence)
        self.assertEqual(report["status"], "MAPPING_REVIEWED_NOT_APPROVED")
        self.assertTrue(report["requires_backup_signature_restore_evidence"])
        self.assertFalse(report["applies_cluster_changes"])

    def test_nonzero_desired_replicas_never_scaled_by_template(self):
        controller = copy.deepcopy(self.controller)
        controller["spec"]["replicas"] = 2
        evidence = self.evidence("a" * 64)
        evidence.update(desired_replicas=2, controller_sha256=rollout.digest(controller))
        patch = rollout.make_patch(controller, self.configmaps, self.refs, evidence, "a" * 64)
        self.assertEqual([item for item in patch if item["path"] == "/spec/replicas"],
                         [{"op": "test", "path": "/spec/replicas", "value": 2}])

    def test_cli_patch_writes_private_file_never_literal_stdout(self):
        report = self.backup()
        source = self.base / "input.json"
        proof = self.base / "proof.json"
        source.write_bytes(rollout.canonical(bundle()))
        proof.write_bytes(rollout.canonical(self.evidence(report["index_sha256"])))
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            code = rollout.main(["patch-template", "--input", str(source), "--backup-dir", str(self.root / report["backup_id"]),
                                 "--backup-index-sha256", report["index_sha256"], "--approved-evidence", str(proof)])
        self.assertEqual(code, 0)
        result = json.loads(output.getvalue())
        self.assertEqual(result["status"], "PATCH_GENERATED_NOT_APPLIED")
        self.assertNotIn("SYNTHETIC_PRIVATE_SENTINEL", output.getvalue())
        patch_path = self.root / report["backup_id"] / result["patch_file"]
        self.assertEqual(stat.S_IMODE(patch_path.stat().st_mode), 0o600)
        self.assertIn("SYNTHETIC_PRIVATE_SENTINEL", patch_path.read_text())


if __name__ == "__main__":
    unittest.main()
