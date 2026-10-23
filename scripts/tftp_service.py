#!/usr/bin/env python3
"""Hand over managed TFTP owners without interrupting a firmware Job.

Input is the renderer's JSON (valid YAML), not an arbitrary kubectl manifest.
There is no automatic retry of mutations. A killed controller can leave the
profile Deployment scaled down; inspect the Job before explicitly resuming it.
The node's shared runtime flock is the final exclusion against concurrent CLIs.
"""
import argparse
import json
from pathlib import Path
import re
import subprocess
import sys
import time


PROFILE = "cisco-tftp-profiles"
FIRMWARE = "cisco-tftp"
MAX_JSON = 4 * 1024 * 1024
WAIT_SECONDS = 180


class Refused(RuntimeError):
    pass


def require(value, reason):
    if not value:
        raise Refused(reason)


def unique_object(pairs):
    value = {}
    for key, item in pairs:
        require(key not in value, "duplicate_json_key")
        value[key] = item
    return value


def decode(raw):
    require(len(raw) <= MAX_JSON, "response_too_large")
    try:
        return json.loads(raw, object_pairs_hook=unique_object)
    except (ValueError, UnicodeError, RecursionError):
        raise Refused("invalid_json") from None


def integer(value):
    return type(value) is int and value >= 0


def metadata(value, kind, namespace, name=None, *, live=True):
    require(type(value) is dict and value.get("kind") == kind, "unexpected_resource")
    version = {"Job": "batch/v1", "Deployment": "apps/v1", "ReplicaSet": "apps/v1",
               "Pod": "v1", "Secret": "v1"}[kind]
    require(value.get("apiVersion") == version, "unexpected_api_version")
    meta = value.get("metadata")
    require(type(meta) is dict and meta.get("namespace") == namespace
            and type(meta.get("name")) is str and meta["name"]
            and (name is None or meta["name"] == name), "unexpected_resource_identity")
    if live:
        require(all(type(meta.get(key)) is str and meta[key] for key in ("uid", "resourceVersion")),
                "missing_resource_identity")
    return meta


def controller(meta, kind, uid, name=None):
    owners = meta.get("ownerReferences")
    require(type(owners) is list and all(type(item) is dict for item in owners), "invalid_owner")
    active = [item for item in owners if item.get("controller") is True]
    require(len(active) == 1 and active[0].get("kind") == kind
            and active[0].get("uid") == uid
            and (name is None or active[0].get("name") == name), "unexpected_owner")


def conditions(status):
    rows = status.get("conditions", [])
    require(type(rows) is list, "invalid_conditions")
    result = {}
    for row in rows:
        require(type(row) is dict and type(row.get("type")) is str and row["type"]
                and row.get("status") in ("True", "False", "Unknown")
                and row["type"] not in result, "invalid_conditions")
        result[row["type"]] = row["status"]
    return result


def managed_pod(spec):
    require(type(spec) is dict and spec.get("hostNetwork") is True
            and spec.get("hostPID", False) is False
            and spec.get("shareProcessNamespace", False) is False, "unmanaged_tftp_pod")
    containers, volumes = spec.get("containers"), spec.get("volumes")
    require(type(containers) is list and len(containers) == 1 and type(containers[0]) is dict
            and containers[0].get("name") == "tftp" and type(volumes) is list
            and all(type(v) is dict for v in volumes), "unmanaged_tftp_pod")
    daemon = containers[0]
    require(not daemon.get("command"), "runtime_entrypoint_overridden")
    env, mounts = daemon.get("env", []), daemon.get("volumeMounts", [])
    require(type(env) is list and all(type(v) is dict for v in env)
            and [v for v in env if v.get("name") == "CISCO_TFTP_LOCK"] ==
            [{"name": "CISCO_TFTP_LOCK", "value": "/run/cisco-tftp/owner.lock"}], "missing_runtime_lock")
    require(type(mounts) is list and all(type(v) is dict for v in mounts), "invalid_tftp_mounts")
    lock = [v for v in mounts if v.get("mountPath") == "/run/cisco-tftp"]
    require(len(lock) == 1 and not lock[0].get("readOnly") and not lock[0].get("subPath")
            and not lock[0].get("subPathExpr"),
            "invalid_runtime_lock_mount")
    require(not any(type(v.get("mountPath")) is str and
                    (v["mountPath"].startswith("/run/cisco-tftp/") or v["mountPath"] in ("/", "/run"))
                    for v in mounts), "runtime_lock_mount_shadowed")
    owned = [v for v in volumes if v.get("name") == lock[0].get("name")]
    require(len(owned) == 1 and owned[0].get("hostPath") ==
            {"path": "/var/lib/cisco-tftp-control", "type": "DirectoryOrCreate"},
            "invalid_runtime_lock_volume")
    roots = [v for v in mounts if v.get("mountPath") == "/srv/tftp"]
    require(len(roots) == 1 and roots[0].get("readOnly") is True, "writable_tftp_root")
    require(type(daemon.get("readinessProbe")) is dict
            and daemon["readinessProbe"].get("exec") ==
            {"command": ["/usr/local/bin/cisco-tftp", "--check"]}, "missing_runtime_readiness")


def deployment(value, namespace, *, live=True):
    meta = metadata(value, "Deployment", namespace, PROFILE, live=live)
    require(not meta.get("deletionTimestamp") and not meta.get("ownerReferences"), "owned_or_deleting_profiles")
    spec = value.get("spec")
    require(type(spec) is dict and type(spec.get("replicas")) is int
            and spec["replicas"] in (0, 1) and spec.get("strategy", {}).get("type") == "Recreate"
            and spec.get("selector") == {"matchLabels": {"app": PROFILE}}, "invalid_profile_deployment")
    template = spec.get("template")
    require(type(template) is dict and template.get("metadata", {}).get("labels", {}).get("app") == PROFILE,
            "invalid_profile_selector")
    managed_pod(template.get("spec"))
    if live:
        require(integer(meta.get("generation")) and type(value.get("status", {})) is dict,
                "invalid_profile_status")
        conditions(value.get("status", {}))
        for key in ("observedGeneration", "replicas", "updatedReplicas", "readyReplicas",
                    "availableReplicas", "unavailableReplicas", "terminatingReplicas"):
            require(integer(value.get("status", {}).get(key, 0)), "invalid_profile_status")
    return meta


def job(value, namespace, *, terminal=False):
    meta = metadata(value, "Job", namespace, FIRMWARE)
    require(not meta.get("deletionTimestamp") and not meta.get("ownerReferences"), "owned_or_deleting_firmware")
    spec, status = value.get("spec"), value.get("status", {})
    require(type(spec) is dict and type(status) is dict, "invalid_firmware_job")
    require(type(spec.get("activeDeadlineSeconds")) is int
            and 0 < spec["activeDeadlineSeconds"] <= 3600 and spec.get("backoffLimit") == 0
            and type(spec.get("backoffLimit")) is int
            and type(spec.get("parallelism", 1)) is int and spec.get("parallelism", 1) == 1
            and type(spec.get("completions", 1)) is int and spec.get("completions", 1) == 1,
            "unbounded_firmware_job")
    template = spec.get("template")
    require(type(template) is dict and template.get("metadata", {}).get("labels", {}).get("app") == FIRMWARE
            and type(template.get("spec")) is dict and template["spec"].get("restartPolicy") == "Never",
            "invalid_firmware_template")
    for key in ("active", "ready", "succeeded", "failed", "terminating"):
        require(integer(status.get(key, 0)), "invalid_firmware_status")
    flags = conditions(status)
    require(set(flags) <= {"Complete", "Failed", "Suspended", "FailureTarget", "SuccessCriteriaMet"},
            "unknown_firmware_condition")
    done = [name for name in ("Complete", "Failed") if flags.get(name) == "True"]
    require(len(done) <= 1, "contradictory_firmware_status")
    if terminal:
        require(len(done) == 1 and not status.get("active", 0) and not status.get("ready", 0)
                and not status.get("terminating", 0), "firmware_not_terminal")
        uncounted = status.get("uncountedTerminatedPods", {})
        require(type(uncounted) is dict and set(uncounted) <= {"succeeded", "failed"}
                and all(type(v) is list and not v for v in uncounted.values()), "firmware_not_settled")
    return meta


def pod(value, namespace):
    meta = metadata(value, "Pod", namespace)
    status = value.get("status")
    require(type(status) is dict and status.get("phase") in
            ("Pending", "Running", "Succeeded", "Failed", "Unknown"), "invalid_pod_status")
    conditions(status)
    return meta, status["phase"]


def ready(value):
    return (not value["metadata"].get("deletionTimestamp")
            and value["status"]["phase"] == "Running"
            and conditions(value["status"]).get("Ready") == "True")


def manifest(path, mode, namespace):
    with Path(path).open("rb") as source:
        value = decode(source.read(MAX_JSON + 1))
    require(type(value) is dict and value.get("apiVersion") == "v1" and value.get("kind") == "List"
            and type(value.get("items")) is list, "expected_renderer_list")
    owners, secrets, names = [], [], set()
    for item in value["items"]:
        require(type(item) is dict, "invalid_manifest_item")
        kind = item.get("kind")
        require(kind in ("Secret", "Deployment" if mode == "profiles" else "Job"), "unexpected_manifest_kind")
        meta = metadata(item, kind, namespace, live=False)
        require(not set(meta).intersection(("uid", "resourceVersion", "deletionTimestamp", "ownerReferences")),
                "live_metadata_in_manifest")
        identity = (kind, meta["name"])
        require(identity not in names, "duplicate_manifest_resource")
        names.add(identity)
        if kind == "Secret":
            require(item.get("immutable") is True, "mutable_tftp_secret")
            secrets.append(item)
        else:
            owners.append(item)
    require(len(owners) == 1, "expected_one_tftp_owner")
    owner = owners[0]
    if mode == "profiles":
        deployment(owner, namespace, live=False)
        require(owner["spec"]["replicas"] == 1, "profiles_must_have_one_replica")
    else:
        require(owner["metadata"]["name"] == FIRMWARE, "unexpected_firmware_name")
        probe = json.loads(json.dumps(owner))
        probe["metadata"].update(uid="manifest", resourceVersion="manifest")
        probe["status"] = {}
        job(probe, namespace)
        require(owner["spec"].get("template", {}).get("metadata", {}).get("labels", {}).get("app") == FIRMWARE,
                "invalid_firmware_selector")
        managed_pod(owner["spec"].get("template", {}).get("spec"))
    return secrets, owner


class Kube:
    def __init__(self, context, namespace, *, runner=subprocess.run, clock=time.monotonic):
        require(type(context) is str and context and "\x00" not in context, "invalid_context")
        require(re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", namespace), "invalid_namespace")
        self.base = ["kubectl", "--context", context, "--namespace", namespace, "--request-timeout=30s"]
        self.namespace, self.runner, self.clock = namespace, runner, clock

    def call(self, *args, payload=None, deadline=None, wait_timeout=35):
        seconds = wait_timeout if deadline is None else min(wait_timeout, deadline - self.clock())
        require(seconds > 0, "handover_timeout")
        try:
            result = self.runner([*self.base, *args], input=payload, capture_output=True,
                                 text=True, timeout=seconds, check=False)
        except (subprocess.TimeoutExpired, OSError):
            raise Refused("kubectl_failed_or_timed_out_no_automatic_retry") from None
        require(result.returncode == 0, "kubectl_failed_no_automatic_retry")
        require(len(result.stdout.encode()) <= MAX_JSON, "response_too_large")
        return result.stdout

    def get(self, kind, name, *, deadline=None):
        raw = self.call("get", kind, name, "--ignore-not-found", "-o", "json", deadline=deadline)
        return decode(raw) if raw.strip() else None

    def items(self, kind, label, *, deadline=None):
        value = decode(self.call("get", kind, "-l", "app=" + label, "-o", "json", deadline=deadline))
        expected = ("v1", "PodList") if kind == "pods" else ("apps/v1", "ReplicaSetList")
        require(type(value) is dict and (value.get("apiVersion"), value.get("kind")) in
                (("v1", "List"), expected)
                and type(value.get("items")) is list, "invalid_resource_list")
        return value["items"]


class Handover:
    def __init__(self, kube, *, clock=time.monotonic, sleep=time.sleep):
        self.kube, self.ns, self.clock, self.sleep = kube, kube.namespace, clock, sleep

    def quiet_firmware(self, deadline=None):
        previous = self.kube.get("job", FIRMWARE, deadline=deadline)
        if previous is not None:
            job(previous, self.ns, terminal=True)
        for item in self.kube.items("pods", FIRMWARE, deadline=deadline):
            meta, phase = pod(item, self.ns)
            require(phase in ("Succeeded", "Failed"), "firmware_pod_not_terminal")
            if previous is not None:
                controller(meta, "Job", previous["metadata"]["uid"], FIRMWARE)
        current = self.kube.get("job", FIRMWARE, deadline=deadline)
        require((current is None) == (previous is None), "firmware_changed")
        if current is not None:
            job(current, self.ns, terminal=True)
            require(all(current["metadata"][key] == previous["metadata"][key]
                        for key in ("uid", "resourceVersion")), "firmware_changed")
        return current

    def wait(self, check):
        deadline = self.clock() + WAIT_SECONDS
        while not check(deadline):
            require(self.clock() < deadline, "handover_timeout")
            self.sleep(min(1, max(0, deadline - self.clock())))

    def profiles(self):
        value = self.kube.get("deployment", PROFILE)
        if value is not None:
            deployment(value, self.ns)
        return value

    def scale(self, value, replicas):
        meta = value["metadata"]
        self.kube.call("scale", "deployment/" + PROFILE, "--replicas=" + str(replicas),
                       "--current-replicas=" + str(value["spec"]["replicas"]),
                       "--resource-version=" + meta["resourceVersion"])

    def profile_gone(self, uid, deadline):
        value = self.kube.get("deployment", PROFILE, deadline=deadline)
        require(value is not None, "profiles_disappeared")
        meta = deployment(value, self.ns)
        require(meta["uid"] == uid and value["spec"]["replicas"] == 0, "profiles_changed")
        items = self.kube.items("pods", PROFILE, deadline=deadline)
        for item in items:
            pod(item, self.ns)
        return not items

    def firmware_ready(self, uid, deadline):
        value = self.kube.get("job", FIRMWARE, deadline=deadline)
        require(value is not None and job(value, self.ns)["uid"] == uid, "firmware_owner_changed")
        flags = conditions(value.get("status", {}))
        require(not any(flags.get(key) == "True" for key in ("Complete", "Failed", "FailureTarget")),
                "firmware_ended_before_ready")
        items = self.kube.items("pods", FIRMWARE, deadline=deadline)
        for item in items:
            meta, phase = pod(item, self.ns)
            controller(meta, "Job", uid, FIRMWARE)
            require(phase not in ("Failed", "Succeeded", "Unknown"), "firmware_pod_failed")
        require(len(items) <= 1, "multiple_firmware_pods")
        return len(items) == 1 and ready(items[0])

    def profiles_ready(self, uid):
        value = self.profiles()
        require(value is not None and value["metadata"]["uid"] == uid, "profiles_owner_changed")
        status, meta = value["status"], value["metadata"]
        require(value["spec"]["replicas"] == 1 and integer(status.get("observedGeneration"))
                and status["observedGeneration"] >= meta["generation"]
                and all(status.get(key) == 1 and type(status.get(key)) is int
                        for key in ("replicas", "updatedReplicas", "readyReplicas", "availableReplicas"))
                and status.get("unavailableReplicas", 0) == 0
                and conditions(status).get("Available") == "True", "profiles_not_ready")
        replicasets = {}
        for item in self.kube.items("replicasets", PROFILE):
            child = metadata(item, "ReplicaSet", self.ns)
            controller(child, "Deployment", uid, PROFILE)
            replicasets[child["uid"]] = child["name"]
        items = self.kube.items("pods", PROFILE)
        require(len(items) == 1, "profiles_not_ready")
        child, _ = pod(items[0], self.ns)
        owners = child.get("ownerReferences", [])
        require(type(owners) is list and len(owners) == 1 and type(owners[0]) is dict
                and owners[0].get("uid") in replicasets, "unexpected_profile_pod_owner")
        controller(child, "ReplicaSet", owners[0]["uid"], replicasets[owners[0]["uid"]])
        require(ready(items[0]), "profiles_not_ready")

    def restore_if_quiet(self, prior):
        """Recover a pre-dispatch failure only after fresh maintenance absence.

        An active/unknown Job, an API failure, or any remaining maintenance Pod
        leaves daily profiles suspended. Never turn a failed observation into a
        second owner, and never replace the original handover error.
        """
        try:
            self.quiet_firmware()
            require(not self.kube.items("pods", FIRMWARE), "maintenance_pods_remain")
            current = self.profiles()
            require(current is not None and current["metadata"]["uid"] == prior["metadata"]["uid"],
                    "profiles_restore_owner_changed")
            require(current["spec"]["template"] == prior["spec"]["template"], "profiles_restore_template_changed")
            if current["spec"]["replicas"] == 0:
                self.scale(current, 1)
        except (Refused, OSError, TypeError, KeyError, AttributeError):
            return False
        return True

    def apply(self, mode, secrets, owner):
        self.quiet_firmware()
        if mode == "profiles":
            require(not self.kube.items("pods", FIRMWARE), "maintenance_pods_remain")
        prior = self.profiles()
        if mode == "profiles":
            self.kube.call("apply", "--server-side", "--field-manager=cisco-home-telephony", "-f", "-",
                           payload=json.dumps({"apiVersion": "v1", "kind": "List", "items": secrets + [owner]}))
            current = self.profiles()
            require(current is not None and (prior is None or current["metadata"]["uid"] == prior["metadata"]["uid"]),
                    "profiles_owner_changed")
            uid = current["metadata"]["uid"]
            # kubectl's watch is separately bounded; follow it with UID/owner checks.
            self.kube.call("rollout", "status", "deployment/" + PROFILE, "--timeout=180s",
                           deadline=self.clock() + WAIT_SECONDS + 5, wait_timeout=WAIT_SECONDS + 5)
            self.profiles_ready(uid)
            self.quiet_firmware()
            require(not self.kube.items("pods", FIRMWARE), "maintenance_pods_remain")
            return
        restore = prior is not None and prior["spec"]["replicas"] == 1
        firmware_dispatched = False
        try:
            if prior is not None:
                if restore:
                    self.scale(prior, 0)
                self.wait(lambda deadline: self.profile_gone(prior["metadata"]["uid"], deadline))
            else:
                require(not self.kube.items("pods", PROFILE), "orphan_profile_pods")
            previous = self.quiet_firmware()
            if previous is not None:
                meta = previous["metadata"]
                options = {"apiVersion": "v1", "kind": "DeleteOptions", "propagationPolicy": "Foreground",
                           "preconditions": {key: meta[key] for key in ("uid", "resourceVersion")}}
                self.kube.call("delete", "--raw", "/apis/batch/v1/namespaces/" + self.ns + "/jobs/" + FIRMWARE,
                               "-f", "-", payload=json.dumps(options))

                def deleted(deadline):
                    current = self.kube.get("job", FIRMWARE, deadline=deadline)
                    if current is not None:
                        require(current.get("metadata", {}).get("uid") == meta["uid"], "firmware_owner_changed")
                        return False
                    items = self.kube.items("pods", FIRMWARE, deadline=deadline)
                    for item in items:
                        child, phase = pod(item, self.ns)
                        controller(child, "Job", meta["uid"], FIRMWARE)
                        require(phase in ("Succeeded", "Failed"), "firmware_pod_not_terminal")
                    return not items
                self.wait(deleted)
            self.quiet_firmware()
            if secrets:
                self.kube.call("apply", "--server-side", "--field-manager=cisco-home-telephony", "-f", "-",
                               payload=json.dumps({"apiVersion": "v1", "kind": "List", "items": secrets}))
            # Sticky before I/O: a timed-out create can commit after a subsequent
            # empty GET. Such an observation must never authorize restoration.
            firmware_dispatched = True
            created = decode(self.kube.call("create", "-f", "-", "-o", "json", payload=json.dumps(owner)))
            uid = job(created, self.ns)["uid"]
            self.wait(lambda deadline: self.firmware_ready(uid, deadline))
        finally:
            if restore and not firmware_dispatched:
                # Never guess after process loss. This invocation alone retained
                # the original owner and desired replicas before scaling down.
                self.restore_if_quiet(prior)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    apply = commands.add_parser("apply")
    apply.add_argument("--mode", choices=("profiles", "firmware"), required=True)
    apply.add_argument("--manifest", type=Path, required=True)
    apply.add_argument("--context", required=True)
    apply.add_argument("--namespace", required=True)
    args = parser.parse_args(argv)
    try:
        kube = Kube(args.context, args.namespace)
        secrets, owner = manifest(args.manifest, args.mode, args.namespace)
        Handover(kube).apply(args.mode, secrets, owner)
    except (Refused, OSError, TypeError, KeyError, AttributeError) as error:
        # Kubernetes output and manifest content can contain private material.
        reason = str(error) if isinstance(error, Refused) else "invalid_input_or_local_io"
        print("TFTP handover refused or incomplete (" + reason + "); inspect owner state before retrying.", file=sys.stderr)
        return 1
    if args.mode == "firmware":
        print("Firmware TFTP owner ready. Daily profiles remain suspended; explicitly apply profiles after maintenance ends.")
    else:
        print("Profile TFTP owner ready. No firmware Job was interrupted.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
