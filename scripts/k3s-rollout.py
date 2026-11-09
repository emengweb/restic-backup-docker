#!/usr/bin/env python3
"""Offline, secret-silent K3s controller backup, plan and guarded patch builder.

Never invokes kubectl, subprocesses, network clients or Secret reads. Input may
contain literal credentials; raw objects and generated patches ONLY go to new
root-owned private files. stdout contains fixed codes, counts and SHA-256 values.
"""
from __future__ import annotations

import argparse
import copy
import datetime as dt
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import stat
import sys
import uuid

MAX_INPUT = 32 * 1024 * 1024
KINDS = {"Deployment", "StatefulSet", "DaemonSet"}
PRIVATE_FILES = ("controller.json", "configmaps.json", "index.json")


class ToolError(Exception):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


def fail(code):
    raise ToolError(code)


def canonical(value):
    return (json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":")) + "\n").encode()


def digest(value):
    return hashlib.sha256(canonical(value)).hexdigest()


def no_duplicate(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            fail("duplicate_json_field")
        result[key] = value
    return result


def load_input(path):
    # '-' is recommended: Lead may pipe a read-only API response directly here.
    try:
        if path == "-":
            raw = sys.stdin.buffer.read(MAX_INPUT + 1)
        else:
            fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
            try:
                info = os.fstat(fd)
                if not stat.S_ISREG(info.st_mode):
                    fail("input_not_regular")
                with os.fdopen(fd, "rb", closefd=False) as stream:
                    raw = stream.read(MAX_INPUT + 1)
            finally:
                os.close(fd)
        if len(raw) > MAX_INPUT:
            fail("input_too_large")
        return json.loads(raw, object_pairs_hook=no_duplicate)
    except (OSError, ValueError, UnicodeError, RecursionError):
        fail("input_unreadable_or_invalid")


def pod_spec(controller):
    return controller.get("spec", {}).get("template", {}).get("spec", {})


def reference_inventory(controller):
    refs = {"configmaps": set(), "secrets": set()}

    def add(kind, name, key="*"):
        if not isinstance(name, str) or not name or not isinstance(key, str):
            fail("invalid_reference")
        refs[kind].add((name, key))

    pod = pod_spec(controller)
    containers = pod.get("initContainers", []) + pod.get("containers", [])
    for container in containers:
        for entry in container.get("env", []):
            source = entry.get("valueFrom", {})
            for selector, kind in (("configMapKeyRef", "configmaps"), ("secretKeyRef", "secrets")):
                if selector in source:
                    add(kind, source[selector].get("name"), source[selector].get("key"))
        for source in container.get("envFrom", []):
            for selector, kind in (("configMapRef", "configmaps"), ("secretRef", "secrets")):
                if selector in source:
                    add(kind, source[selector].get("name"))
    for volume in pod.get("volumes", []):
        if "configMap" in volume:
            add("configmaps", volume["configMap"].get("name"))
        if "secret" in volume:
            add("secrets", volume["secret"].get("secretName"))
        for source in volume.get("projected", {}).get("sources", []):
            if "configMap" in source:
                add("configmaps", source["configMap"].get("name"))
            if "secret" in source:
                add("secrets", source["secret"].get("name"))
    for item in pod.get("imagePullSecrets", []):
        add("secrets", item.get("name"))
    return {key: [{"name": name, "key": item} for name, item in sorted(value)]
            for key, value in refs.items()}


def unpack(value):
    if not isinstance(value, dict):
        fail("input_bundle_required")
    if "controller" in value:
        objects = [value["controller"]] + value.get("configmaps", [])
    elif value.get("kind") == "List":
        objects = value.get("items", [])
    else:
        objects = [value]
    if not isinstance(objects, list) or not all(isinstance(item, dict) for item in objects):
        fail("invalid_input_objects")
    controllers = [item for item in objects if item.get("kind") in KINDS]
    if len(controllers) != 1:
        fail("exactly_one_controller_required")
    if any(item.get("kind") not in KINDS | {"ConfigMap"} for item in objects):
        fail("secret_or_unsupported_object_refused")
    controller = controllers[0]
    metadata = controller.get("metadata", {})
    if (not all(isinstance(metadata.get(key), str) and metadata[key] for key in
                ("name", "namespace", "uid", "resourceVersion"))
            or type(metadata.get("generation")) is not int):
        fail("controller_identity_required")
    spec = pod_spec(controller)
    if not isinstance(spec.get("containers"), list) or not spec["containers"]:
        fail("pod_template_required")
    replicas = controller.get("spec", {}).get("replicas")
    if controller["kind"] != "DaemonSet" and (type(replicas) is not int or replicas < 0):
        fail("explicit_desired_replicas_required")
    refs = reference_inventory(controller)
    wanted = {item["name"] for item in refs["configmaps"]}
    configmaps, names = [], set()
    for item in objects:
        if item.get("kind") != "ConfigMap":
            continue
        meta = item.get("metadata", {})
        name = meta.get("name")
        if meta.get("namespace") != metadata["namespace"] or name not in wanted or name in names:
            fail("configmap_namespace_reference_or_duplicate")
        if not isinstance(meta.get("uid"), str) or not isinstance(meta.get("resourceVersion"), str):
            fail("configmap_identity_required")
        names.add(name)
        configmaps.append(item)
    return controller, sorted(configmaps, key=lambda item: item["metadata"]["name"]), refs


def legacy_commands(controller, sidecar_name=None, init_name=None):
    pod = pod_spec(controller)
    return {"sidecar": [{"command": item.get("command"), "args": item.get("args")}
                        for item in pod.get("containers", []) if sidecar_name is None or item.get("name") == sidecar_name],
            "init": [{"command": item.get("command"), "args": item.get("args")}
                     for item in pod.get("initContainers", []) if init_name is None or item.get("name") == init_name]}


def safe_summary(controller, configmaps, refs):
    meta = controller["metadata"]
    containers = pod_spec(controller).get("initContainers", []) + pod_spec(controller)["containers"]
    return {"controller_sha256": digest(controller), "configmaps_sha256": digest(configmaps),
            "identity_sha256": digest({key: meta[key] for key in
                                        ("namespace", "name", "uid", "resourceVersion", "generation")}),
            "kind": controller["kind"], "replicas": controller["spec"].get("replicas"),
            "container_count": len(containers), "legacy_commands_sha256": digest(legacy_commands(controller)),
            "secret_ref_count": len(refs["secrets"]),
            "configmap_count": len(configmaps), "references_sha256": digest(refs),
            "container_config_sha256": digest([{key: item.get(key) for key in
                                                 ("name", "image", "command", "args", "env", "envFrom", "volumeMounts")}
                                                for item in containers])}


def private_root(path, *, create=False):
    if os.geteuid() != 0:
        fail("root_required_for_private_backup")
    path = Path(path)
    if not path.is_absolute() or ".." in path.parts or len(path.parts) < 2:
        fail("absolute_private_path_required")
    fd = os.open("/", os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
    try:
        for index, part in enumerate(path.parts[1:]):
            try:
                child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC,
                                dir_fd=fd)
            except FileNotFoundError:
                if not create or index != len(path.parts) - 2:
                    fail("private_parent_must_exist")
                os.mkdir(part, 0o700, dir_fd=fd)
                os.fsync(fd)
                child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC,
                                dir_fd=fd)
            os.close(fd)
            fd = child
        info = os.fstat(fd)
        if info.st_uid != 0 or stat.S_IMODE(info.st_mode) != 0o700:
            fail("private_directory_must_be_root_0700")
        return fd
    except BaseException:
        os.close(fd)
        raise


def write_exclusive(directory, name, value):
    raw = canonical(value)
    temporary = ".pending-" + uuid.uuid4().hex
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
                 0o600, dir_fd=directory)
    try:
        os.fchmod(fd, 0o600)
        view = memoryview(raw)
        while view:
            count = os.write(fd, view)
            if count <= 0:
                fail("private_write_failed")
            view = view[count:]
        os.fsync(fd)
    finally:
        os.close(fd)
    # link is atomic and refuses overwrite, unlike os.replace.
    os.link(temporary, name, src_dir_fd=directory, dst_dir_fd=directory, follow_symlinks=False)
    os.unlink(temporary, dir_fd=directory)
    os.fsync(directory)
    return hashlib.sha256(raw).hexdigest()


def backup_bundle(controller, configmaps, refs, root):
    parent = private_root(root, create=True)
    directory = None
    backup_id = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ-") + uuid.uuid4().hex
    try:
        os.mkdir(backup_id, 0o700, dir_fd=parent)
        directory = os.open(backup_id, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent)
        os.fchmod(directory, 0o700)
        files = {"controller.json": write_exclusive(directory, "controller.json", controller),
                 "configmaps.json": write_exclusive(directory, "configmaps.json", configmaps)}
        summary = safe_summary(controller, configmaps, refs)
        metadata = controller["metadata"]
        index = {"schema_version": 1, "backup_id": backup_id,
                 "created_at": dt.datetime.now(dt.timezone.utc).isoformat(),
                 "complete": True, "files": files, "summary": summary,
                 "controller": {key: metadata[key] for key in
                                ("name", "namespace", "uid", "resourceVersion", "generation")},
                 "kind": controller["kind"], "replicas": controller["spec"].get("replicas"),
                 "secret_references_only": refs["secrets"], "references": refs}
        checksum = write_exclusive(directory, "index.json", index)
        os.fsync(directory)
        os.fsync(parent)
        return {"status": "backed_up", "backup_id": backup_id, "index_sha256": checksum, **summary}
    finally:
        if directory is not None:
            os.close(directory)
        os.close(parent)


def read_private(directory, name):
    fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK, dir_fd=directory)
    try:
        info = os.fstat(fd)
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != 0 or info.st_nlink != 1
                or stat.S_IMODE(info.st_mode) != 0o600 or info.st_size > MAX_INPUT):
            fail("untrusted_private_file")
        chunks, size = [], 0
        while True:
            block = os.read(fd, 65536)
            if not block:
                break
            chunks.append(block)
            size += len(block)
            if size > MAX_INPUT:
                fail("private_file_too_large")
        raw = b"".join(chunks)
        return json.loads(raw, object_pairs_hook=no_duplicate), hashlib.sha256(raw).hexdigest()
    finally:
        os.close(fd)


def verify_backup(controller, configmaps, refs, directory_path, expected_sha):
    if not re.fullmatch(r"[0-9a-f]{64}", expected_sha or ""):
        fail("backup_index_sha256_required")
    directory = private_root(directory_path)
    try:
        index, actual_sha = read_private(directory, "index.json")
        original, controller_sha = read_private(directory, "controller.json")
        configs, configs_sha = read_private(directory, "configmaps.json")
        if (actual_sha != expected_sha or index.get("complete") is not True
                or index.get("schema_version") != 1 or original != controller or configs != configmaps
                or index.get("files") != {"controller.json": controller_sha, "configmaps.json": configs_sha}
                or index.get("summary") != safe_summary(controller, configmaps, refs)):
            fail("backup_sha_or_current_object_mismatch")
        wanted = {entry["name"] for entry in refs["configmaps"]}
        if wanted != {item["metadata"]["name"] for item in configmaps}:
            fail("backup_missing_referenced_configmaps")
        return index
    finally:
        os.close(directory)


def clean_path(value):
    if not isinstance(value, str) or not value.startswith("/") or value in {"/", "//"}:
        fail("explicit_nonroot_data_path_required")
    path = PurePosixPath(value)
    if ".." in path.parts or "$" in value or "\x00" in value or str(path) != value:
        fail("unsafe_or_ambiguous_path")
    return path


def overlap(left, right):
    return left == right or left in right.parents or right in left.parents


def literal_env(container, configmaps, *, opaque_secret_reviewed=False):
    values, sources = {}, {}
    cm = {item["metadata"]["name"]: item.get("data", {}) for item in configmaps}

    def add(name, source, known=None):
        if name in sources:
            fail("duplicate_or_shadowed_environment")
        sources[name] = source
        if known is not None:
            values[name] = known

    for source in container.get("envFrom", []):
        if set(source) - {"prefix", "configMapRef", "secretRef"}:
            fail("unknown_envfrom_source")
        if "secretRef" in source:
            # Secret contents must NEVER be read just to decide collisions.
            # Explicit per-key refs are inspectable; opaque envFrom is not.
            if not opaque_secret_reviewed:
                fail("opaque_secret_envfrom_requires_manual_review")
            continue
        if "configMapRef" not in source:
            fail("unknown_envfrom_source")
        name = source["configMapRef"].get("name")
        if name not in cm:
            fail("missing_referenced_configmaps")
        prefix = source.get("prefix", "")
        for key, value in cm[name].items():
            add(prefix + key, "configmap", value)
    for entry in container.get("env", []):
        name = entry.get("name")
        if not isinstance(name, str) or not name:
            fail("invalid_environment_name")
        if "value" in entry:
            add(name, "literal", entry["value"])
        elif "configMapKeyRef" in entry.get("valueFrom", {}):
            ref = entry["valueFrom"]["configMapKeyRef"]
            value = cm.get(ref.get("name"), {}).get(ref.get("key"))
            if value is None:
                fail("missing_configmap_environment_key")
            add(name, "configmap", value)
        else:
            add(name, "reference")
    return values, sources


def _set_env(container, name, value, *, replacement=False):
    values = container.setdefault("env", [])
    existing = [entry for entry in values if entry.get("name") == name]
    if len(existing) > 1:
        fail("duplicate_environment")
    if existing:
        if existing[0] == {"name": name, "value": value}:
            return
        if not replacement:
            fail("managed_environment_conflict")
        values[values.index(existing[0])] = {"name": name, "value": value}
    else:
        values.append({"name": name, "value": value})


def data_volume_source(controller, pod, name):
    """Resolve StatefulSet implicit PVCs before template volumes, like the controller.

    Claim templates take precedence over same-name template volumes. Do not
    synthesize a volume, mutate claim templates, or reinterpret a shadowed
    hostPath as the source being restored.
    """
    claims = controller.get("spec", {}).get("volumeClaimTemplates", [])
    matching = [item for item in claims if item.get("metadata", {}).get("name") == name]
    if matching:
        if controller.get("kind") != "StatefulSet" or len(matching) != 1:
            fail("ambiguous_or_nonstateful_claim_template")
        claim = matching[0]
        if (not isinstance(claim.get("spec"), dict)
                or claim["spec"].get("volumeMode", "Filesystem") != "Filesystem"):
            fail("filesystem_claim_template_required")
        return "claim_template", claim
    volumes = [item for item in pod.get("volumes", []) if item.get("name") == name]
    if (len(volumes) != 1
            or set(volumes[0]) - {"name", "persistentVolumeClaim", "emptyDir", "hostPath"}
            or len(set(volumes[0]) - {"name"}) != 1):
        fail("explicit_single_supported_data_volume_required")
    return "host_path" if "hostPath" in volumes[0] else "template_volume", volumes[0]


def make_patch(controller, configmaps, refs, evidence, backup_sha):
    if not isinstance(evidence, dict) or evidence.get("schema_version") != 1:
        fail("approved_evidence_schema_required")
    metadata = controller["metadata"]
    for key, expected in (("controller_sha256", digest(controller)), ("uid", metadata["uid"]),
                          ("resource_version", metadata["resourceVersion"]), ("backup_index_sha256", backup_sha)):
        if evidence.get(key) != expected:
            fail("approval_identity_or_backup_mismatch")
    if (evidence.get("approved") is not True or evidence.get("restore_passed") is not True
            or evidence.get("signature_verified") is not True
            or not re.fullmatch(r"[0-9a-f]{64}", evidence.get("signature_evidence_sha256", ""))
            or not re.fullmatch(r"[0-9a-f]{64}", evidence.get("restore_evidence_sha256", ""))):
        fail("approval_signature_and_restore_proof_required")
    image = evidence.get("public_image", "")
    if (not re.fullmatch(r"ghcr\.io/[a-z0-9._/-]+@sha256:[0-9a-f]{64}", image)
            or any(part in {"", ".", ".."} for part in image.split("@")[0].split("/")[1:])):
        fail("approved_public_digest_required")
    if controller["kind"] not in {"Deployment", "StatefulSet"}:
        fail("controller_kind_requires_manual_rollout")
    if evidence.get("desired_replicas") != controller["spec"]["replicas"]:
        fail("approval_desired_replicas_mismatch")
    engine = evidence.get("engine")
    dataset = evidence.get("dataset_id", "")
    if (engine not in {"files", "postgres", "mariadb"}
            or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_./:-]{0,199}", dataset)):
        fail("explicit_dataset_engine_required")
    target = clean_path(evidence.get("recovery_data_path"))
    source = clean_path(evidence.get("source_data_path")) if engine == "files" else target
    work = clean_path(evidence.get("work_path", "/k3s-backup-work"))
    if overlap(target, work):
        fail("scratch_and_data_paths_overlap")
    pod = copy.deepcopy(pod_spec(controller))
    if pod.get("hostPID") or pod.get("hostIPC") or pod.get("hostNetwork"):
        fail("host_namespace_requires_manual_review")
    sidecars = [item for item in pod["containers"] if item.get("name") == evidence.get("sidecar_name")]
    inits = [item for item in pod.get("initContainers", []) if item.get("name") == evidence.get("init_name")]
    if len(sidecars) != 1 or len(inits) != 1:
        fail("explicit_existing_sidecar_and_init_required")
    sidecar, init = sidecars[0], inits[0]
    commands = legacy_commands(controller, sidecar["name"], init["name"])
    if any(item.get("command") or item.get("args") for items in commands.values() for item in items):
        if (evidence.get("legacy_commands_reviewed") is not True
                or evidence.get("legacy_commands_sha256") != digest(commands)):
            fail("legacy_commands_require_digest_bound_review")
    if sidecar.get("restartPolicy") or init.get("restartPolicy"):
        fail("native_sidecar_or_restart_policy_requires_review")
    for item in (sidecar, init):
        if any(key in item for key in ("lifecycle", "startupProbe", "livenessProbe", "readinessProbe")):
            fail("unknown_hook_or_probe_requires_manual_review")
        if item.get("securityContext", {}).get("privileged"):
            fail("privileged_container_refused")
        effective_nonroot = item.get("securityContext", {}).get("runAsNonRoot",
                                                                    pod.get("securityContext", {}).get("runAsNonRoot", False))
        if effective_nonroot is True and evidence.get("root_context_override_reviewed") is not True:
            fail("root_restore_security_context_requires_review")
    opaque_reviewed = (evidence.get("opaque_secret_envfrom_reviewed") is True
                       and evidence.get("unresolved_secret_env_conflicts_accepted") is True)
    side_values, side_sources = literal_env(sidecar, configmaps, opaque_secret_reviewed=opaque_reviewed)
    init_values, init_sources = literal_env(init, configmaps, opaque_secret_reviewed=opaque_reviewed)
    aliases = {"files": "files", "file": "files", "": "files", "postgres": "postgres", "postgresql": "postgres",
               "pg": "postgres", "mariadb": "mariadb", "mysql": "mariadb"}
    hook_names = {"PRE_BACKUP_COMMAND", "POST_BACKUP_COMMAND", "PRE_RESTORE_COMMAND", "POST_RESTORE_COMMAND",
                  "BACKUP_HOOK", "RESTORE_HOOK", "BACKUP_COMMAND", "RESTORE_COMMAND"}
    for values, sources, phase_data in ((side_values, side_sources, str(source)),
                                       (init_values, init_sources, str(target))):
        if hook_names & sources.keys():
            fail("unknown_environment_hook_requires_manual_review")
        if "DATA_PATH" in sources and values.get("DATA_PATH") != phase_data:
            fail("data_path_environment_conflict")
        if "RECOVERY_DATA_PATH" in sources and values.get("RECOVERY_DATA_PATH") != phase_data:
            fail("recovery_path_environment_conflict")
        if "BACKUP_WORK_PATH" in sources and values.get("BACKUP_WORK_PATH") != str(work):
            fail("work_path_environment_conflict")
        if "DATABASE_TYPE" in sources and aliases.get(values.get("DATABASE_TYPE", "").lower()) != engine:
            fail("engine_environment_conflict")
        if "DATASET_ID" in sources and values.get("DATASET_ID") != dataset:
            fail("dataset_environment_conflict")
        if values.get("BACKUP_MODE", "logical" if engine != "files" else "files") not in {"logical", "files"}:
            fail("physical_or_dual_requires_separate_review")
    if init.get("envFrom", []) != sidecar.get("envFrom", []):
        fail("init_sidecar_envfrom_mismatch")
    side_by_name = {item["name"]: item for item in sidecar.get("env", [])}
    init_by_name = {item["name"]: item for item in init.get("env", [])}
    phase_paths = {"DATA_PATH", "RECOVERY_DATA_PATH", "BACKUP_WORK_PATH"}
    for name in (init_by_name.keys() & side_by_name.keys()) - phase_paths:
        if init_by_name[name] != side_by_name[name]:
            fail("init_sidecar_environment_conflict")
    # Common credentials/scheduling refs merge verbatim. Path aliases belong to
    # their own phase: Config prefers RECOVERY_DATA_PATH even during backup.
    merged = [copy.deepcopy(entry) for entry in sidecar.get("env", []) if entry["name"] not in phase_paths]
    merged.extend(copy.deepcopy(entry) for name, entry in init_by_name.items()
                  if name not in side_by_name and name not in phase_paths)
    init["env"] = copy.deepcopy(merged) + [copy.deepcopy(entry) for name, entry in init_by_name.items() if name in phase_paths]
    sidecar["env"] = copy.deepcopy(merged) + [copy.deepcopy(entry) for name, entry in side_by_name.items() if name in phase_paths]
    data_name = evidence.get("data_volume")
    pod.setdefault("volumes", [])
    source_kind, _ = data_volume_source(controller, pod, data_name)
    if (source_kind == "claim_template" and any(item.get("name") == data_name for item in pod.get("volumes", []))
            and evidence.get("claim_template_shadowed_volume_reviewed") is not True):
        fail("claim_template_shadowed_volume_requires_review")
    if source_kind == "host_path" and evidence.get("hostpath_reviewed") is not True:
        fail("hostpath_requires_explicit_review")
    data_mounts = [item for item in init.get("volumeMounts", []) if item.get("name") == data_name]
    if (len(data_mounts) != 1 or data_mounts[0].get("mountPath") != str(target)
            or data_mounts[0].get("readOnly") or data_mounts[0].get("subPathExpr")
            or data_mounts[0].get("mountPropagation")):
        fail("recovery_path_must_match_existing_writable_init_mount")
    if len(init.get("volumeMounts", [])) != 1:
        fail("multiple_init_mounts_require_manual_review")
    app_mounts = [mount for container in pod["containers"] if container is not sidecar
                  for mount in container.get("volumeMounts", []) if mount.get("name") == data_name]
    if not app_mounts:
        fail("data_volume_not_mounted_by_application")
    init_sub = data_mounts[0].get("subPath", "")
    if any(mount.get("subPath", "") != init_sub or mount.get("subPathExpr") for mount in app_mounts):
        fail("application_and_restore_subpath_mismatch")
    side_mounts = [mount for mount in sidecar.get("volumeMounts", []) if mount.get("name") == data_name]
    if engine == "files":
        if (len(side_mounts) != 1 or side_mounts[0].get("mountPath") != str(source)
                or side_mounts[0].get("subPath", "") != init_sub
                or "subPathExpr" in side_mounts[0] or "mountPropagation" in side_mounts[0]):
            fail("file_source_mount_mapping_mismatch")
        side_mounts[0]["readOnly"] = True
    else:
        # Logical backup connects via DB endpoint; do NOT mount raw DB source.
        if any(mount.get("name") != data_name for mount in sidecar.get("volumeMounts", [])):
            fail("database_sidecar_unknown_mount_requires_manual_review")
        sidecar["volumeMounts"] = []
        for container in pod["containers"]:
            if container is sidecar:
                continue
            app_values, app_sources = literal_env(container, configmaps, opaque_secret_reviewed=opaque_reviewed)
            if engine == "postgres" and "PGDATA" in app_sources:
                fail("pgdata_environment_requires_explicit_relayout")
        if engine == "postgres" and ("PGDATA" in init_sources or "PGDATA" in side_sources):
            fail("pgdata_environment_requires_explicit_relayout")
    work_name = evidence.get("work_volume", "k3s-backup-work")
    if not isinstance(work_name, str) or not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,62}", work_name):
        fail("invalid_scratch_volume_name")
    if (any(item.get("name") == work_name for item in pod.get("volumes", []))
            or any(item.get("metadata", {}).get("name") == work_name
                   for item in controller.get("spec", {}).get("volumeClaimTemplates", []))):
        fail("scratch_volume_name_collision")
    limit = evidence.get("scratch_size_limit")
    if not isinstance(limit, str) or not re.fullmatch(r"[1-9][0-9]*(Mi|Gi|Ti)", limit):
        fail("explicit_scratch_size_limit_required")
    pod["volumes"].append({"name": work_name, "emptyDir": {"sizeLimit": limit}})
    max_bytes, min_free = evidence.get("backup_max_bytes"), evidence.get("backup_min_free_bytes")
    if type(max_bytes) is not int or max_bytes <= 0 or type(min_free) is not int or min_free <= 0:
        fail("explicit_dataset_capacity_limits_required")
    for item, command in ((init, "restore-init"), (sidecar, "schedule")):
        item["image"] = image
        item["imagePullPolicy"] = "IfNotPresent"
        item["command"] = ["python3", "-m", "k3s_backup", command]
        item.pop("args", None)
        item.setdefault("volumeMounts", []).append({"name": work_name, "mountPath": str(work)})
        for mount in item["volumeMounts"]:
            if mount["name"] != work_name and overlap(clean_path(mount["mountPath"]), work):
                fail("scratch_mount_overlaps_existing_mount")
        requested_data = str(target if item is init else source)
        item_values, item_sources, item_by_name = ((init_values, init_sources, init_by_name) if item is init
                                                  else (side_values, side_sources, side_by_name))
        for name, value in (("DATABASE_TYPE", engine), ("DATASET_ID", dataset),
                            ("DATA_PATH", requested_data), ("RECOVERY_DATA_PATH", requested_data),
                            ("BACKUP_WORK_PATH", str(work)),
                            ("BACKUP_MAX_BYTES", str(max_bytes)), ("BACKUP_MIN_FREE_BYTES", str(min_free))):
            if name in item_sources and name not in item_by_name:
                if item_values.get(name) != value:
                    fail("managed_configmap_environment_conflict")
            else:
                _set_env(item, name, value)
        if engine != "files":
            major = evidence.get("target_major", "")
            if not isinstance(major, str) or not re.fullmatch(r"[0-9]{1,2}(\.[0-9]{1,2})?", major):
                fail("explicit_target_database_major_required")
            _set_env(item, "RESTORE_TARGET_MAJOR", major)
        resources = evidence.get("resources")
        if not isinstance(resources, dict) or not resources.get("limits", {}).get("memory") or not resources.get("limits", {}).get("cpu"):
            fail("explicit_resource_limits_required")
        item["resources"] = copy.deepcopy(resources)
        security = item.setdefault("securityContext", {})
        security.update({"runAsUser": 0, "runAsGroup": 0, "runAsNonRoot": False,
                         "allowPrivilegeEscalation": False})
        security["capabilities"] = {"drop": ["ALL"], "add": ["CHOWN", "DAC_OVERRIDE", "FOWNER", "SETUID", "SETGID"]}
        item["securityContext"] = security
    patch = [{"op": "test", "path": "/metadata/uid", "value": metadata["uid"]},
             {"op": "test", "path": "/metadata/resourceVersion", "value": metadata["resourceVersion"]},
             {"op": "test", "path": "/spec/replicas", "value": controller["spec"]["replicas"]},
             {"op": "test", "path": "/spec/template", "value": controller["spec"]["template"]}]
    template = copy.deepcopy(controller["spec"]["template"])
    template["spec"] = pod
    patch.append({"op": "replace", "path": "/spec/template", "value": template})
    if controller["spec"]["replicas"] == 0:
        patch.append({"op": "replace", "path": "/spec/replicas", "value": 0})
    return patch


def emit_patch(controller, configmaps, refs, args):
    if not args.backup_dir or not args.approved_evidence:
        fail("completed_backup_and_approved_evidence_required")
    verify_backup(controller, configmaps, refs, args.backup_dir, args.backup_index_sha256)
    evidence = load_input(args.approved_evidence)
    patch = make_patch(controller, configmaps, refs, evidence, args.backup_index_sha256)
    directory = private_root(args.backup_dir)
    try:
        filename = "patch-" + uuid.uuid4().hex + ".json"
        checksum = write_exclusive(directory, filename, patch)
        return {"status": "PATCH_GENERATED_NOT_APPLIED", "applies_cluster_changes": False,
                "patch_file": filename, "patch_sha256": checksum, "operations": len(patch),
                "evidence_sha256": digest(evidence), "replicas_preserved": controller["spec"]["replicas"],
                "image_digest": evidence["public_image"].rsplit("@", 1)[1]}
    finally:
        os.close(directory)


def inspect_plan(controller, configmaps, proposal):
    """Evaluate a mapping without faking approval/restore or creating a patch."""
    reasons = []
    if not isinstance(proposal, dict):
        fail("plan_mapping_required")
    if proposal.get("engine") not in {"files", "postgres", "mariadb"}:
        reasons.append("unsupported_or_missing_engine")
    if not isinstance(proposal.get("dataset_id"), str) or not proposal["dataset_id"]:
        reasons.append("stable_dataset_required")
    pod = pod_spec(controller)
    source_kind = None
    try:
        source_kind, _ = data_volume_source(controller, pod, proposal.get("data_volume"))
        if (source_kind == "claim_template" and any(item.get("name") == proposal.get("data_volume")
                                                   for item in pod.get("volumes", []))):
            reasons.append("claim_template_shadowed_volume_requires_review")
    except ToolError as error:
        reasons.append(error.code)
    sidecars = [item for item in pod["containers"] if item.get("name") == proposal.get("sidecar_name")]
    inits = [item for item in pod.get("initContainers", []) if item.get("name") == proposal.get("init_name")]
    if len(sidecars) != 1 or len(inits) != 1:
        reasons.append("existing_init_sidecar_mapping_required")
    else:
        for item in (sidecars[0], inits[0]):
            if item.get("lifecycle"):
                reasons.append("unknown_lifecycle_hook")
            try:
                literal_env(item, configmaps)
            except ToolError as error:
                reasons.append(error.code)
        init_mount = [mount for mount in inits[0].get("volumeMounts", [])
                      if mount.get("name") == proposal.get("data_volume")]
        side_mount = [mount for mount in sidecars[0].get("volumeMounts", [])
                      if mount.get("name") == proposal.get("data_volume")]
        if len(init_mount) != 1 or init_mount[0].get("mountPath") != proposal.get("recovery_data_path") or init_mount[0].get("readOnly"):
            reasons.append("writable_recovery_mount_mapping_required")
        if proposal.get("engine") == "files" and (len(side_mount) != 1
                or side_mount[0].get("mountPath") != proposal.get("source_data_path")
                or "subPathExpr" in side_mount[0] or "mountPropagation" in side_mount[0]):
            reasons.append("file_source_mount_mapping_required")
        if len(inits[0].get("volumeMounts", [])) != 1:
            reasons.append("multiple_init_mounts_require_review")
    return {"status": "NO_GO" if reasons else "MAPPING_REVIEWED_NOT_APPROVED",
            "reasons": sorted(set(reasons)), "proposal_sha256": digest(proposal),
            "data_volume_source": source_kind,
            "legacy_commands_sha256": digest(legacy_commands(controller, proposal.get("sidecar_name"), proposal.get("init_name"))),
            "replicas_preserved": controller["spec"].get("replicas"),
            "requires_backup_signature_restore_evidence": True,
            "applies_cluster_changes": False}


def plan(controller, configmaps, refs):
    present = {item["metadata"]["name"] for item in configmaps}
    missing = {item["name"] for item in refs["configmaps"]} - present
    reasons = ["missing_referenced_configmaps"] if missing else []
    if controller["kind"] == "DaemonSet":
        reasons.append("daemonset_requires_separate_approval")
    return {"status": "NO_GO" if reasons else "PLAN_ONLY", "applies_cluster_changes": False,
            "approval_required": True, "reasons": reasons,
            "missing_configmap_count": len(missing), **safe_summary(controller, configmaps, refs)}


def parser():
    result = argparse.ArgumentParser(description="Offline private backup and guarded rollout planning; never applies")
    result.add_argument("command", choices=("plan", "backup", "patch-template"), nargs="?", default="plan")
    result.add_argument("--input", default="-", help="Controller or controller/configmaps bundle JSON; '-' reads stdin")
    result.add_argument("--backup-root", help="Root-owned mode0700 backup parent; final component may be created")
    result.add_argument("--backup-dir", help="Exact completed backup directory to verify before patch generation")
    result.add_argument("--backup-index-sha256")
    result.add_argument("--approved-evidence")
    result.add_argument("--plan-input", help="Optional reviewed non-secret dataset/volume plan, never authorizes a patch")
    return result


def main(argv=None):
    args = parser().parse_args(argv)
    try:
        controller, configmaps, refs = unpack(load_input(args.input))
        report = plan(controller, configmaps, refs)
        if args.plan_input:
            report["template_plan"] = inspect_plan(controller, configmaps, load_input(args.plan_input))
        if args.command == "backup" or args.backup_root:
            if not args.backup_root:
                fail("backup_root_required")
            report["backup"] = backup_bundle(controller, configmaps, refs, args.backup_root)
        if args.command == "backup":
            report["status"] = "BACKED_UP" if not report["reasons"] else "BACKED_UP_NO_GO"
        if args.command == "patch-template":
            report = emit_patch(controller, configmaps, refs, args)
        print(json.dumps(report, sort_keys=True))
        return 0 if not report.get("reasons", []) else 3
    except ToolError as error:
        print(json.dumps({"status": "NO_GO", "code": error.code, "applies_cluster_changes": False}))
        return 2
    except Exception:
        # Never print traceback, raw JSON, errno filenames, literals or argv.
        print(json.dumps({"status": "NO_GO", "code": "invalid_input_or_private_io",
                          "applies_cluster_changes": False}))
        return 2


if __name__ == "__main__":
    sys.exit(main())
