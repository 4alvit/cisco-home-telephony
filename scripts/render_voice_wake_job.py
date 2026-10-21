#!/usr/bin/env python3
"""Render an inert, one-shot wake pilot using an immutable optional voice image.

The caller supplies an existing private model PVC and the exact host directory
used by the gateway's VoiceControl. Rendering never accesses a cluster or phone.
"""
import argparse
import copy
import importlib.util
import json
import math
import os
from pathlib import Path, PurePosixPath
import re
import stat
import sys

ROOT = Path(__file__).resolve().parents[1]


def module(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    value = importlib.util.module_from_spec(spec)
    sys.modules[name] = value
    spec.loader.exec_module(value)
    return value


manual = module("_wake_job_manual", ROOT / "scripts/render_voice_job.py")
models = module("_wake_job_models", ROOT / "containers/voice-wake/stage_models.py")
control = module("_wake_job_control", ROOT / "containers/asterisk/phone_voice_control.py")
require = manual._require
canonical = manual._canonical
RUNTIME = "/run/voice"
CONFIG = RUNTIME + "/.local/config"
STATE = RUNTIME + "/.local/state/voice-control"
# A child that ignores Stop can consume the complete kill/report deadlines
# before the wrapper enters its separate uncertain-cleanup drain.
TERMINATION_GRACE_SECONDS = math.ceil(
    control.STOP_GRACE_SECONDS + control.REPORT_GRACE_SECONDS
    + control.UNCERTAIN_CLEANUP_SECONDS + 18)

# Only this initializer runs as root. It sees a new emptyDir, no host state,
# Secret or model PVC. CHOWN is its only extra capability.
PREPARE = """import os
from pathlib import Path
os.umask(0o077)
root = Path('/runtime/private')
root.mkdir(mode=0o700)
paths = [root, root/'.local', root/'.local/config', root/'.local/models',
         root/'.local/state', root/'.local/state/voice-control']
for path in paths:
    if path != root:
        path.mkdir(mode=0o700)
for path in reversed(paths):
    os.chown(path, 10001, 10001)
"""

STAGE = """import json, sys
from pathlib import Path
sys.path[:0] = ['/opt/voice', '/opt/voice-wake']
from stage_private import stage_private
from stage_models import stage_models
def unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError('duplicate_private_bundle_key')
        result[key] = value
    return result
root = Path('/runtime/private/.local')
stage_private('/run/voice-inputs', root/'config', '/run/voice-inputs/staging.json')
bundle = json.loads((root/'config/bundle.json').read_text(), object_pairs_hook=unique_object)
stage_models('/run/wake-models', root/'models', bundle)
print('Private configuration and three pinned models staged.')
"""


def read_private(value, root):
    path = manual._private_path(value, root)
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, "rb") as stream:
        info = os.fstat(stream.fileno())
        require(stat.S_ISREG(info.st_mode) and info.st_uid == os.getuid()
                and not info.st_mode & 0o077 and info.st_size <= manual.MAX_CONFIG_BYTES,
                "owner_only_private_file_required")
        data = stream.read(manual.MAX_CONFIG_BYTES + 1)
    require(len(data) <= manual.MAX_CONFIG_BYTES, "private_file_too_large")
    return data


def unique_object(pairs):
    result = {}
    for key, value in pairs:
        require(key not in result, "duplicate_private_json_key")
        result[key] = value
    return result


def render(site, transport_config, gateway_config, voice_config, token, bundle, *,
           voice_image, models_claim, state_host_path, display_port=None,
           name="phone-wake-pilot"):
    site, transport, gateway, voice = copy.deepcopy(
        (site, transport_config, gateway_config, voice_config))
    manual.validate_voice_config(voice)
    require(voice["wake"]["mode"] == "onnx", "onnx_profile_required")
    phone = manual._validate(site, transport, gateway, voice, token, display_port,
                             wake_mode="onnx")
    # Retain preparation/finish time beyond the fixed media lease, with the
    # previous 120-second floor. SIGTERM cleanup grace remains independent.
    active_deadline = max(120, math.ceil(transport["duration_seconds"]) + 90)
    bundle = models._validate_bundle(bundle)
    require(isinstance(name, str) and re.fullmatch(r"[a-z][a-z0-9-]{0,29}[a-z0-9]|[a-z]", name),
            "invalid_job_name")
    require(isinstance(models_claim, str) and len(models_claim) <= 253
            and all(re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", part)
                    for part in models_claim.split(".")), "invalid_models_claim")
    require(isinstance(voice_image, str)
            and re.fullmatch(r"[a-z0-9][a-z0-9._:/-]*@sha256:[0-9a-f]{64}", voice_image),
            "immutable_wake_image_required")
    require(isinstance(state_host_path, str), "shared_voice_state_required")
    state_path = PurePosixPath(state_host_path)
    require(state_path.is_absolute() and len(state_path.parts) >= 4
            and state_path.name == "voice-control" and ".." not in state_path.parts
            and str(state_path) == state_host_path and "\x00" not in state_host_path,
            "exact_shared_voice_state_required")

    reduced = {
        "persistent_enabled": True, "bind_ipv4": gateway["bind_ipv4"],
        "api_port": gateway["api_port"], "api_clients": [site["node_ipv4"]],
        "api_token": "unused-wake-pilot-http-api-token-00000000",
        "ami": {k: gateway["ami"][k] for k in ("host", "port", "username", "secret")},
        "dial_allowlist": [], "ring_allowlist": [],
        "phones": [{k: phone[k] for k in
                    ("id", "lines", "ipv4", "device", "port", "auth_path", "username", "password")}],
    }
    transport["gateway_config"] = CONFIG + "/gateway.json"
    voice["ha"]["token_file"] = CONFIG + "/ha-token.txt"
    voice["wake"]["bundle_file"] = RUNTIME + "/.local/models/bundle.json"
    files = {
        "transport.json": canonical(transport), "gateway.json": canonical(reduced),
        "voice.json": canonical(voice), "ha-token.txt": token.strip() + "\n",
        "bundle.json": canonical(bundle),
    }
    files["staging.json"] = canonical({"version": 1, "files": sorted(files)})
    require(sum(len(k.encode()) + len(v.encode()) for k, v in files.items())
            <= manual.MAX_CONFIG_BYTES, "private_config_too_large")
    namespace = site["namespace"]
    secret_name, secret = manual.k3s.secret(namespace, name + "-private", files)
    revision = manual._sha({
        "files": files, "image": voice_image, "claim": models_claim,
        "state": state_host_path, "site": site, "display": display_port,
        "name": name, "prepare": PREPARE, "stage": STAGE,
        "active_deadline_seconds": active_deadline,
    })[:16]
    labels = {"app": "phone-wake-pilot", "app.kubernetes.io/part-of": "cisco-home-telephony",
              "wake-pilot-revision": revision}
    secret["metadata"]["labels"] = labels
    security = {"allowPrivilegeEscalation": False, "readOnlyRootFilesystem": True,
                "capabilities": {"drop": ["ALL"]}}
    resources = {
        "requests": {"cpu": "20m", "memory": "128Mi", "ephemeral-storage": "128Mi"},
        "limits": {"cpu": "1000m", "memory": "512Mi", "ephemeral-storage": "256Mi"},
    }
    pod = manual.k3s.pod_base(site)
    pod.update(restartPolicy="Never", enableServiceLinks=False,
               terminationGracePeriodSeconds=TERMINATION_GRACE_SECONDS)
    # Do not set fsGroup: kubelet must not recursively alter an existing model
    # PVC or the gateway's state. Provision their UID permissions separately.
    pod["securityContext"].update(runAsNonRoot=True, runAsUser=10001, runAsGroup=10001)
    command = [
        "python3", "/opt/voice-wake/run_reserved.py",
        "--transport-config", CONFIG + "/transport.json",
        "--voice-config", CONFIG + "/voice.json", "--workspace", RUNTIME,
        "--state-directory", STATE, "--ffmpeg", "/usr/bin/ffmpeg",
        "--attempt-id", revision,
        "--run", "--attended", "--exclusive-media", "--enable-microphone",
    ]
    if display_port is not None:
        command += ["--display-port", str(display_port)]
    pod["initContainers"] = [
        {
            "name": "prepare-private-runtime", "image": voice_image,
            "command": ["python3", "-c", PREPARE],
            "securityContext": {**security, "runAsNonRoot": False, "runAsUser": 0,
                                "runAsGroup": 0, "capabilities": {"drop": ["ALL"], "add": ["CHOWN"]}},
            "resources": resources,
            "volumeMounts": [{"name": "runtime", "mountPath": "/runtime"}],
        },
        {
            "name": "stage-pinned-models", "image": voice_image,
            "command": ["python3", "-c", STAGE], "securityContext": security,
            "resources": resources,
            "volumeMounts": [{"name": "runtime", "mountPath": "/runtime"},
                             {"name": "inputs", "mountPath": "/run/voice-inputs", "readOnly": True},
                             {"name": "models", "mountPath": "/run/wake-models", "readOnly": True}],
        },
    ]
    pod["containers"] = [{
        "name": "wake-pilot", "image": voice_image, "imagePullPolicy": "IfNotPresent",
        "workingDir": "/workspace", "command": command,
        "env": [{"name": "PYTHONDONTWRITEBYTECODE", "value": "1"},
                {"name": "PYTHONUNBUFFERED", "value": "1"}, {"name": "TMPDIR", "value": "/tmp"}],
        "securityContext": security, "resources": resources,
        "volumeMounts": [{"name": "runtime", "mountPath": RUNTIME, "subPath": "private", "readOnly": True},
                         {"name": "state", "mountPath": STATE},
                         {"name": "tmp", "mountPath": "/tmp"}],
    }]
    pod["volumes"] = [
        {"name": "runtime", "emptyDir": {"sizeLimit": "40Mi"}},
        {"name": "inputs", "secret": {"secretName": secret_name, "defaultMode": 0o444}},
        {"name": "models", "persistentVolumeClaim": {"claimName": models_claim, "readOnly": True}},
        # Directory, never DirectoryOrCreate: a typo must not create a separate
        # lock namespace and allow overlapping capture with the gateway.
        {"name": "state", "hostPath": {"path": state_host_path, "type": "Directory"}},
        {"name": "tmp", "emptyDir": {"sizeLimit": "64Mi"}},
    ]
    job = {
        "apiVersion": "batch/v1", "kind": "Job",
        "metadata": {"name": name + "-" + revision, "namespace": namespace, "labels": labels},
        "spec": {"suspend": True, "parallelism": 1, "completions": 1, "backoffLimit": 0,
                 "podReplacementPolicy": "Failed",
                 "activeDeadlineSeconds": active_deadline, "ttlSecondsAfterFinished": 300,
                 "template": {"metadata": {"labels": labels}, "spec": pod}},
    }
    return {"apiVersion": "v1", "kind": "List", "items": [secret, job]}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    for flag in ("site", "transport-config", "voice-config", "bundle", "voice-image",
                 "models-claim", "state-host-path", "output"):
        parser.add_argument("--" + flag, required=True)
    parser.add_argument("--workspace", default=str(ROOT))
    parser.add_argument("--display-port", type=int)
    parser.add_argument("--name", default="phone-wake-pilot")
    args = parser.parse_args(argv)
    try:
        root = Path(args.workspace).resolve()

        def read(value):
            return json.loads(read_private(value, root), object_pairs_hook=unique_object)

        config = read(args.transport_config)
        gateway = read(config["gateway_config"])
        voice = read(args.voice_config)
        token = read_private(voice["ha"]["token_file"], root).decode("ascii")
        result = render(read(args.site), config, gateway, voice, token, read(args.bundle),
                        voice_image=args.voice_image, models_claim=args.models_claim,
                        state_host_path=args.state_host_path, display_port=args.display_port, name=args.name)
        manual.write_private_manifest(result, args.output, workspace=root)
    except Exception:
        print("Wake Job render failed: check private inputs, immutable image, model PVC, "
              "shared voice state and exclusive output path.", file=sys.stderr)
        return 2
    print("Private suspended wake Job rendered. No cluster changes or microphone activation.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
