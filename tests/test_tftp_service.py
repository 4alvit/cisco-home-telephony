import copy
import importlib.util
import json
from pathlib import Path
import subprocess
import tempfile
import types
import unittest


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("tftp_service", ROOT / "scripts/tftp_service.py")
service = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(service)
NS = "telephony"


def meta(name, uid=None):
    result = {"name": name, "namespace": NS}
    if uid is not None:
        result.update(uid=uid, resourceVersion="10", generation=1)
    return result


def podspec(restart="Always"):
    return {"hostNetwork": True, "restartPolicy": restart, "containers": [{
        "name": "tftp", "env": [{"name": "CISCO_TFTP_LOCK", "value": "/run/cisco-tftp/owner.lock"}],
        "readinessProbe": {"exec": {"command": ["/usr/local/bin/cisco-tftp", "--check"]}},
        "volumeMounts": [{"name": "ownership", "mountPath": "/run/cisco-tftp"},
                         {"name": "served", "mountPath": "/srv/tftp", "readOnly": True}]}],
        "volumes": [{"name": "ownership", "hostPath": {
            "path": "/var/lib/cisco-tftp-control", "type": "DirectoryOrCreate"}},
                    {"name": "served", "emptyDir": {}}]}


def profile(*, live=True, replicas=1):
    value = {"apiVersion": "apps/v1", "kind": "Deployment",
             "metadata": meta(service.PROFILE, "profile-uid" if live else None),
             "spec": {"replicas": replicas, "strategy": {"type": "Recreate"},
                      "selector": {"matchLabels": {"app": service.PROFILE}},
                      "template": {"metadata": {"labels": {"app": service.PROFILE}}, "spec": podspec()}}}
    if live:
        value["status"] = {"observedGeneration": 1, "replicas": replicas, "updatedReplicas": replicas,
                           "readyReplicas": replicas, "availableReplicas": replicas,
                           "conditions": [{"type": "Available", "status": "True"}]}
    return value


def firmware(*, live=True, terminal=True, uid="old-job"):
    value = {"apiVersion": "batch/v1", "kind": "Job",
             "metadata": meta(service.FIRMWARE, uid if live else None),
             "spec": {"activeDeadlineSeconds": 3600, "backoffLimit": 0,
                      "template": {"metadata": {"labels": {"app": service.FIRMWARE}},
                                   "spec": podspec("Never")}}}
    if live:
        value["status"] = ({"succeeded": 1, "conditions": [{"type": "Complete", "status": "True"}]}
                           if terminal else {"active": 1})
    return value


def owned_pod(label, uid, phase="Running", is_ready=True):
    value = {"apiVersion": "v1", "kind": "Pod", "metadata": meta(label + "-pod", label + "-pod-uid"),
             "status": {"phase": phase, "conditions": [{"type": "Ready", "status": "True" if is_ready else "False"}]}}
    owner_kind, name = ("Job", service.FIRMWARE) if label == service.FIRMWARE else ("ReplicaSet", "profiles-rs")
    value["metadata"]["ownerReferences"] = [{"kind": owner_kind, "uid": uid, "name": name, "controller": True}]
    return value


class Cluster:
    """Small stateful API double; mutations change future observations."""
    namespace = NS

    def __init__(self):
        self.profile = profile()
        self.job = None
        self.firmware_pods = []
        self.profile_pods = [owned_pod(service.PROFILE, "rs-uid")]
        self.events = []
        self.hook = lambda event: None
        self.instant_ready = True
        self.keep_profile_pods = False
        self.now = 0

    def sleep(self, seconds):
        self.now += seconds

    def get(self, kind, name, **kwargs):
        self.events.append(("get", kind))
        self.hook(("get", kind))
        return copy.deepcopy(self.job if kind == "job" else self.profile)

    def items(self, kind, label, **kwargs):
        self.events.append(("items", kind, label))
        self.hook(("items", kind, label))
        if kind == "replicasets":
            return [{"apiVersion": "apps/v1", "kind": "ReplicaSet", "metadata": {
                **meta("profiles-rs", "rs-uid"), "ownerReferences": [{"kind": "Deployment",
                "name": service.PROFILE, "uid": self.profile["metadata"]["uid"], "controller": True}]}}]
        return copy.deepcopy(self.firmware_pods if label == service.FIRMWARE else self.profile_pods)

    def call(self, *args, payload=None, **kwargs):
        event = (args[0], args, json.loads(payload) if payload else None)
        self.events.append(event)
        self.hook(event)
        if args[0] == "scale":
            old = self.profile["spec"]["replicas"]
            if "--current-replicas=" + str(old) not in args:
                raise service.Refused("scale_conflict")
            if "--resource-version=" + self.profile["metadata"]["resourceVersion"] not in args:
                raise service.Refused("version_conflict")
            replicas = int(next(value.split("=")[1] for value in args if value.startswith("--replicas=")))
            self.profile["spec"]["replicas"] = replicas
            self.profile["metadata"]["resourceVersion"] = str(int(self.profile["metadata"]["resourceVersion"]) + 1)
            if not self.keep_profile_pods:
                self.profile_pods = [owned_pod(service.PROFILE, "rs-uid")] if replicas else []
        elif args[0] == "delete":
            options = json.loads(payload)
            if options["preconditions"] != {key: self.job["metadata"][key] for key in ("uid", "resourceVersion")}:
                raise service.Refused("delete_conflict")
            self.job, self.firmware_pods = None, []
        elif args[0] == "create":
            if self.job is not None:
                raise service.Refused("already_exists")
            self.job = firmware(terminal=False, uid="new-job")
            self.firmware_pods = [owned_pod(service.FIRMWARE, "new-job", is_ready=self.instant_ready)]
            return json.dumps(self.job)
        elif args[0] == "apply":
            for item in json.loads(payload)["items"]:
                if item["kind"] == "Deployment":
                    self.profile = profile()
                    self.profile["spec"] = item["spec"]
                    self.profile_pods = [owned_pod(service.PROFILE, "rs-uid")]
        return ""

    def mutations(self):
        return [event for event in self.events if event[0] in ("scale", "delete", "create", "apply")]

    def apply(self, mode):
        owner = profile(live=False) if mode == "profiles" else firmware(live=False)
        return service.Handover(self, clock=lambda: self.now, sleep=self.sleep).apply(mode, [], owner)


class HandoverTests(unittest.TestCase):
    def test_active_pending_malformed_and_deleting_jobs_never_mutate_either_mode(self):
        cases = [firmware(terminal=False), firmware()]
        cases[1]["status"] = {}
        for change in (
            lambda v: v.pop("status"),
            lambda v: v["status"].update(active=1),
            lambda v: v["status"].update(active=True),
            lambda v: v["status"].update(conditions=[{"type": "Complete", "status": True}]),
            lambda v: v["status"].update(conditions=[{"type": "FutureCondition", "status": "True"}]),
            lambda v: v["status"].update(uncountedTerminatedPods={"failed": ["unsettled"]}),
            lambda v: v["metadata"].pop("uid"),
            lambda v: v["metadata"].update(deletionTimestamp="now"),
            lambda v: v["spec"].pop("template"),
        ):
            item = firmware()
            change(item)
            cases.append(item)
        for mode in ("profiles", "firmware"):
            for value in cases:
                with self.subTest(mode=mode, job=value):
                    cluster = Cluster()
                    cluster.job = value
                    with self.assertRaises(service.Refused):
                        cluster.apply(mode)
                    self.assertEqual(cluster.mutations(), [])

    def test_any_nonterminal_or_unknown_firmware_pod_blocks_even_without_job(self):
        for phase in ("Pending", "Running", "Unknown", "NewPhase", None):
            for mode in ("profiles", "firmware"):
                with self.subTest(phase=phase, mode=mode):
                    cluster = Cluster()
                    cluster.firmware_pods = [owned_pod(service.FIRMWARE, "orphan", phase)]
                    with self.assertRaises(service.Refused):
                        cluster.apply(mode)
                    self.assertEqual(cluster.mutations(), [])

    def test_profile_update_leaves_terminal_firmware_job_untouched(self):
        cluster = Cluster()
        cluster.job = firmware()
        cluster.apply("profiles")
        self.assertEqual([event[0] for event in cluster.mutations()], ["apply"])
        self.assertEqual(cluster.job["metadata"]["uid"], "old-job")
        self.assertTrue(any(event[0] == "rollout" for event in cluster.events))

    def test_terminal_firmware_pods_still_block_profile_resume_until_gone(self):
        cluster = Cluster()
        cluster.job = firmware()
        cluster.firmware_pods = [owned_pod(service.FIRMWARE, "old-job", "Succeeded", False)]
        with self.assertRaisesRegex(service.Refused, "maintenance_pods_remain"):
            cluster.apply("profiles")
        self.assertEqual(cluster.mutations(), [])

    def test_terminal_handover_orders_drain_delete_create_ready_without_restore(self):
        cluster = Cluster()
        cluster.job = firmware()
        cluster.firmware_pods = [owned_pod(service.FIRMWARE, "old-job", "Succeeded", False)]
        cluster.apply("firmware")
        mutations = cluster.mutations()
        self.assertEqual([e[0] for e in mutations], ["scale", "delete", "create"])
        self.assertIn("--replicas=0", mutations[0][1])
        self.assertEqual(mutations[1][2]["preconditions"], {"uid": "old-job", "resourceVersion": "10"})
        self.assertEqual(mutations[1][2]["propagationPolicy"], "Foreground")
        self.assertEqual(cluster.job["metadata"]["uid"], "new-job")
        self.assertEqual(cluster.profile["spec"]["replicas"], 0)

    def test_profile_pods_must_be_gone_before_firmware_and_wait_is_bounded(self):
        cluster = Cluster()
        cluster.keep_profile_pods = True
        with self.assertRaisesRegex(service.Refused, "timeout"):
            cluster.apply("firmware")
        self.assertEqual(cluster.now, service.WAIT_SECONDS)
        self.assertEqual([e[0] for e in cluster.mutations()], ["scale", "scale"])
        self.assertEqual(cluster.profile["spec"]["replicas"], 1)

    def test_replacement_job_before_delete_is_protected_by_uid_precondition(self):
        cluster = Cluster()
        cluster.job = firmware()
        def race(event):
            if event[0] == "delete":
                cluster.job = firmware(terminal=False, uid="concurrent-job")
        cluster.hook = race
        with self.assertRaisesRegex(service.Refused, "delete_conflict"):
            cluster.apply("firmware")
        self.assertEqual(cluster.job["metadata"]["uid"], "concurrent-job")
        self.assertFalse(any(e[0] == "create" for e in cluster.events))
        self.assertEqual(cluster.profile["spec"]["replicas"], 0)

    def test_job_appearing_before_create_is_never_applied_over(self):
        cluster = Cluster()
        def race(event):
            if event[0] == "create":
                cluster.job = firmware(terminal=False, uid="concurrent-job")
        cluster.hook = race
        with self.assertRaisesRegex(service.Refused, "already_exists"):
            cluster.apply("firmware")
        self.assertEqual(cluster.job["metadata"]["uid"], "concurrent-job")
        self.assertFalse(any(e[0] == "delete" for e in cluster.events))
        self.assertEqual(cluster.profile["spec"]["replicas"], 0)

    def test_not_ready_firmware_times_out_without_killing_job_or_restoring_profile(self):
        cluster = Cluster()
        cluster.instant_ready = False
        with self.assertRaisesRegex(service.Refused, "timeout"):
            cluster.apply("firmware")
        self.assertEqual(cluster.job["metadata"]["uid"], "new-job")
        self.assertFalse(any(e[0] == "delete" for e in cluster.events))
        self.assertEqual(cluster.profile["spec"]["replicas"], 0)

    def test_readiness_from_wrong_job_owner_is_refused(self):
        cluster = Cluster()
        def race(event):
            if event == ("items", "pods", service.FIRMWARE) and cluster.job is not None:
                cluster.firmware_pods = [owned_pod(service.FIRMWARE, "unrelated")]
        cluster.hook = race
        with self.assertRaisesRegex(service.Refused, "unexpected_owner"):
            cluster.apply("firmware")
        self.assertEqual(cluster.job["metadata"]["uid"], "new-job")

    def test_do_not_restore_replaced_profile_deployment(self):
        cluster = Cluster()
        def race(event):
            if event == ("items", "pods", service.PROFILE):
                cluster.profile["metadata"]["uid"] = "replacement"
                raise service.Refused("handover_failed")
        cluster.hook = race
        with self.assertRaisesRegex(service.Refused, "handover_failed"):
            cluster.apply("firmware")
        self.assertEqual([e[0] for e in cluster.mutations()], ["scale"])

    def test_prior_zero_replica_is_preserved(self):
        cluster = Cluster()
        cluster.profile = profile(replicas=0)
        cluster.profile_pods = []
        cluster.apply("firmware")
        self.assertEqual([e[0] for e in cluster.mutations()], ["create"])
        self.assertEqual(cluster.profile["spec"]["replicas"], 0)

    def test_predispatch_error_restores_only_with_fresh_quiet_job_and_no_pods(self):
        for obstruction in (None, "pod", "api", "template"):
            with self.subTest(obstruction=obstruction):
                cluster = Cluster()
                failed = False
                def fail(event):
                    nonlocal failed
                    if event == ("items", "pods", service.PROFILE):
                        failed = True
                        if obstruction == "pod":
                            cluster.firmware_pods = [owned_pod(service.FIRMWARE, "old", "Succeeded")]
                        if obstruction == "template":
                            cluster.profile["spec"]["template"]["metadata"]["labels"]["changed"] = "yes"
                        raise service.Refused("handover_failed")
                    if failed and obstruction == "api" and event == ("get", "job"):
                        raise service.Refused("api_unavailable")
                cluster.hook = fail
                with self.assertRaisesRegex(service.Refused, "handover_failed"):
                    cluster.apply("firmware")
                self.assertEqual(cluster.profile["spec"]["replicas"], 1 if obstruction is None else 0)

    def test_ambiguous_create_with_currently_absent_job_never_restores_profiles(self):
        cluster = Cluster()
        def fail(event):
            if event[0] == "create":
                raise service.Refused("create_timeout")
        cluster.hook = fail
        with self.assertRaisesRegex(service.Refused, "create_timeout"):
            cluster.apply("firmware")
        self.assertIsNone(cluster.job)
        self.assertEqual(cluster.profile["spec"]["replicas"], 0)
        self.assertEqual([e[0] for e in cluster.mutations()], ["scale", "create"])

    def test_explicit_profiles_resume_waits_until_firmware_is_terminal(self):
        cluster = Cluster()
        cluster.apply("firmware")
        with self.assertRaises(service.Refused):
            cluster.apply("profiles")
        self.assertEqual(cluster.profile["spec"]["replicas"], 0)
        cluster.job["status"] = {"conditions": [{"type": "Complete", "status": "True"}], "succeeded": 1}
        cluster.firmware_pods[0]["status"] = {"phase": "Succeeded"}
        with self.assertRaisesRegex(service.Refused, "maintenance_pods_remain"):
            cluster.apply("profiles")
        cluster.firmware_pods = []
        cluster.apply("profiles")
        self.assertEqual(cluster.profile["spec"]["replicas"], 1)

    def test_active_job_rerun_does_not_guess_restore_after_controller_loss(self):
        cluster = Cluster()
        cluster.job = firmware(terminal=False)
        cluster.profile = profile(replicas=0)
        cluster.profile_pods = []
        with self.assertRaises(service.Refused):
            cluster.apply("firmware")
        self.assertEqual(cluster.mutations(), [])

    def test_profile_rollout_must_match_current_uid_and_pod_owner(self):
        for change in ("uid", "pod"):
            with self.subTest(change=change):
                cluster = Cluster()
                def race(event):
                    if event[0] == "rollout":
                        if change == "uid":
                            cluster.profile["metadata"]["uid"] = "replacement"
                        else:
                            cluster.profile_pods = [owned_pod(service.PROFILE, "foreign-rs")]
                cluster.hook = race
                with self.assertRaises(service.Refused):
                    cluster.apply("profiles")


class InputTests(unittest.TestCase):
    def load(self, owner, mode="profiles"):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "manifest; $(not-a-command).yaml"
            path.write_text(json.dumps({"apiVersion": "v1", "kind": "List", "items": [owner]}))
            return service.manifest(path, mode, NS)

    def test_renderer_owner_contract(self):
        self.load(profile(live=False))
        self.load(firmware(live=False), "firmware")

    def test_actual_rendered_manifests_are_accepted_without_changes(self):
        from test_k3s import fixture, render
        from test_profile_tftp import files, inventory
        for mode, value in (("profiles", render.render_profile_tftp(fixture(), files(), inventory())),
                            ("firmware", render.render_tftp(fixture(), files()))):
            with self.subTest(mode=mode), tempfile.TemporaryDirectory() as folder:
                path = Path(folder) / "rendered.yaml"
                path.write_text(json.dumps(value))
                secrets, owner = service.manifest(path, mode, NS)
                self.assertEqual(secrets + [owner], value["items"])

    def test_missing_lock_wrong_volume_writable_root_or_fake_readiness_refused(self):
        for change in (
            lambda s: s["containers"][0].update(env=[]),
            lambda s: s["volumes"][0]["hostPath"].update(path="/tmp/other-lock"),
            lambda s: s["containers"][0]["volumeMounts"][1].update(readOnly=False),
            lambda s: s["containers"][0]["readinessProbe"].update(exec={"command": ["true"]}),
            lambda s: s.update(hostPID=True),
            lambda s: s.update(shareProcessNamespace=True),
            lambda s: s["containers"][0].update(command=["/usr/sbin/in.tftpd"]),
            lambda s: s["containers"][0]["volumeMounts"][0].update(subPathExpr="other-lock"),
        ):
            for mode in ("profiles", "firmware"):
                with self.subTest(mode=mode, change=change):
                    owner = profile(live=False) if mode == "profiles" else firmware(live=False)
                    change(owner["spec"]["template"]["spec"])
                    with self.assertRaises(service.Refused):
                        self.load(owner, mode)

    def test_invalid_existing_profiles_never_scaled(self):
        cluster = Cluster()
        cluster.profile["spec"]["template"]["spec"]["containers"][0]["env"] = []
        with self.assertRaises(service.Refused):
            cluster.apply("firmware")
        self.assertEqual(cluster.mutations(), [])

    def test_duplicate_json_keys_rejected(self):
        with self.assertRaises(service.Refused):
            service.decode('{"kind":"List","kind":"Job"}')

    def test_context_is_inert_argv_and_kube_calls_have_timeout(self):
        calls = []
        def run(argv, **kwargs):
            calls.append((argv, kwargs))
            return types.SimpleNamespace(returncode=0, stdout="", stderr="")
        context = "ctx; $(touch /tmp/never)"
        kube = service.Kube(context, NS, runner=run)
        kube.call("apply", "-f", "-", payload="private manifest")
        argv, kwargs = calls[0]
        self.assertEqual(argv, ["kubectl", "--context", context, "--namespace", NS,
                                "--request-timeout=30s", "apply", "-f", "-"])
        self.assertNotIn("shell", kwargs)
        self.assertEqual(kwargs["input"], "private manifest")
        self.assertEqual(kwargs["timeout"], 35)

    def test_kubectl_failure_never_echoes_private_stderr(self):
        def run(*args, **kwargs):
            return types.SimpleNamespace(returncode=1, stdout="private", stderr="secret-token")
        with self.assertRaisesRegex(service.Refused, "^kubectl_failed_no_automatic_retry$"):
            service.Kube("ctx", NS, runner=run).call("get", "jobs")
        def timeout(*args, **kwargs):
            raise subprocess.TimeoutExpired(args[0], 35, output="secret")
        with self.assertRaisesRegex(service.Refused, "^kubectl_failed_or_timed_out_no_automatic_retry$"):
            service.Kube("ctx", NS, runner=timeout).call("get", "jobs")

    def test_kubectl_generic_and_typed_lists_and_unknown_schema(self):
        for kind, version, name in (("pods", "v1", "List"), ("pods", "v1", "PodList"),
                                    ("replicasets", "v1", "List"), ("replicasets", "apps/v1", "ReplicaSetList")):
            def run(*args, **kwargs):
                return types.SimpleNamespace(returncode=0, stdout=json.dumps(
                    {"apiVersion": version, "kind": name, "items": []}), stderr="")
            self.assertEqual(service.Kube("ctx", NS, runner=run).items(kind, service.PROFILE), [])
        for value in ({"items": []}, {"apiVersion": "v1", "kind": "List", "items": None}):
            def run(*args, **kwargs):
                return types.SimpleNamespace(returncode=0, stdout=json.dumps(value), stderr="")
            with self.assertRaises(service.Refused):
                service.Kube("ctx", NS, runner=run).items("pods", service.FIRMWARE)


if __name__ == "__main__":
    unittest.main()
