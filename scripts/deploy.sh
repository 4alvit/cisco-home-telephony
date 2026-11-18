#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
site=.local/site.json
mode=local
with_tftp=0
profiles_only=0
tftp_only=0
phone_addresses=
firewall_reviewed=0
usage_error() {
  echo "$1" >&2
  exit 2
}
while [[ $# -gt 0 ]]; do
  case "$1" in
    --site|--phone-addresses)
      [[ $# -ge 2 && -n "$2" && "$2" != --* ]] || usage_error "$1 requires a file path."
      if [[ "$1" == --site ]]; then site=$2; else phone_addresses=$2; fi
      shift 2
      ;;
    --apply|--server-dry-run)
      [[ "$mode" == local ]] || usage_error 'Choose one deployment mode: --apply or --server-dry-run.'
      if [[ "$1" == --apply ]]; then mode=apply; else mode=server; fi
      shift
      ;;
    --with-tftp) with_tftp=1; shift ;;
    --profiles-only) profiles_only=1; shift ;;
    --tftp-only) tftp_only=1; shift ;;
    --firewall-reviewed) firewall_reviewed=1; shift ;;
    *) echo "Usage: $0 [--site FILE] [--server-dry-run | --apply --firewall-reviewed] [--with-tftp | --tftp-only | --profiles-only [--phone-addresses FILE]]" >&2; exit 2 ;;
  esac
done
if (( with_tftp + profiles_only + tftp_only > 1 )); then
  echo 'Choose only one TFTP mode.' >&2; exit 2
fi
if [[ -n "$phone_addresses" && "$profiles_only" != 1 ]]; then
  echo '--phone-addresses requires --profiles-only.' >&2; exit 2
fi
if [[ "$mode" == apply && "$firewall_reviewed" != 1 ]]; then
  usage_error 'Before apply, follow host firewall/socket checks and pass --firewall-reviewed. hostNetwork bypasses normal pod isolation.'
fi
args=(--site "$site")
if [[ "$with_tftp" == 1 ]]; then args+=(--with-tftp); fi
if [[ "$profiles_only" == 1 ]]; then args+=(--profiles-only); fi
if [[ "$tftp_only" == 1 ]]; then args+=(--tftp-only); fi
if [[ -n "$phone_addresses" ]]; then args+=(--phone-addresses "$phone_addresses"); fi
python3 scripts/preflight.py "${args[@]}"
python3 scripts/render_k3s.py "${args[@]}"
if [[ "$mode" == local ]]; then
  echo 'Local dry run complete. No cluster calls. Review private manifests; docs describe --server-dry-run / --apply.'
  exit 0
fi
python3 scripts/preflight.py "${args[@]}" --cluster
context=$(python3 -c 'import json,sys;print(json.load(open(sys.argv[1]))["kube_context"])' "$site")
namespace=$(python3 -c 'import json,sys;print(json.load(open(sys.argv[1]))["namespace"])' "$site")
rendered=$(python3 -c 'import pathlib,sys;print(pathlib.Path(sys.argv[1]).resolve().parent / "rendered")' "$site")
kube=(kubectl --context "$context" --request-timeout=30s)
if [[ "$mode" == server ]]; then
  # For a fresh namespace, Kubernetes cannot dry-run namespaced children until the namespace exists.
  if ! "${kube[@]}" get namespace "$namespace" >/dev/null 2>&1; then
    echo 'Server dry run needs an existing namespace. Local render passed. Review namespace labels, then create the namespace explicitly per docs.' >&2
    exit 2
  fi
  if [[ "$profiles_only" == 0 && "$tftp_only" == 0 ]]; then
    "${kube[@]}" apply --server-side --field-manager=cisco-home-telephony --dry-run=server -f "$rendered/core.yaml"
  fi
  if [[ "$with_tftp" == 1 || "$tftp_only" == 1 ]]; then "${kube[@]}" apply --server-side --field-manager=cisco-home-telephony --dry-run=server -f "$rendered/tftp.yaml"; fi
  if [[ "$profiles_only" == 1 ]]; then "${kube[@]}" apply --server-side --field-manager=cisco-home-telephony --dry-run=server -f "$rendered/tftp-profiles.yaml"; fi
  exit 0
fi
if [[ "$profiles_only" == 0 && "$tftp_only" == 0 ]]; then
  "${kube[@]}" apply --server-side --field-manager=cisco-home-telephony -f "$rendered/core.yaml"
  "${kube[@]}" -n "$namespace" rollout status deployment/asterisk --timeout=180s
fi
if [[ "$with_tftp" == 1 || "$tftp_only" == 1 ]]; then
  python3 scripts/tftp_service.py apply --mode firmware --manifest "$rendered/tftp.yaml" --context "$context" --namespace "$namespace"
fi
if [[ "$profiles_only" == 1 ]]; then
  python3 scripts/tftp_service.py apply --mode profiles --manifest "$rendered/tftp-profiles.yaml" --context "$context" --namespace "$namespace"
fi
