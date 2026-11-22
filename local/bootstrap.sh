#!/usr/bin/env bash
# One-shot installer run by the bootstrap service of compose.yaml. Re-runnable, every wait bounded.
# Secrets arrive only as env; none is printed, traced or passed as an argument.
set +x
set -euo pipefail
umask 077

cd /repo
# shellcheck source=versions.conf
. local/versions.conf

STATE=/state
export KUBECONFIG="$STATE/kubeconfig.yaml"
HTTP_PORT="${AGENTS_LOCAL_HTTP_PORT:-8080}"

log() { printf '[bootstrap] %s\n' "$*"; }

# wait_for SECONDS DESCRIPTION CMD...: poll every 3 s until CMD succeeds, else fail.
wait_for() {
  local limit="$1" what="$2" waited=0 err=""
  shift 2
  # Only kubectl/API waits are wrapped, so the last stderr line carries no secret.
  until err=$("$@" 2>&1 > /dev/null); do
    if [ "$waited" -ge "$limit" ]; then
      log "timed out after ${limit}s waiting for $what"
      [ -z "$err" ] || log "last error: $(printf '%s\n' "$err" | tail -n 1)"
      exit 1
    fi
    sleep 3
    waited=$((waited + 3))
  done
}

log "installing tools"
mise install --yes python helm helmfile kubectl > /dev/null
x() { mise exec -- "$@"; }

log "preparing kubeconfig"
mkdir -p "$STATE"
sed 's#server: https://[^ ]*#server: https://k3s:6443#' /output/kubeconfig.yaml > "$STATE/kubeconfig.yaml.tmp"
mv "$STATE/kubeconfig.yaml.tmp" "$KUBECONFIG"

log "waiting for k3s"
wait_for 300 "k3s readyz" x kubectl get --raw /readyz
wait_for 300 "traefik CRDs" x kubectl get crd middlewares.traefik.io

log "registry login and operator"
printf '%s' "$HARBOR_PASSWORD" | x helm registry login harbor.szymonrichert.pl --username "$HARBOR_USERNAME" --password-stdin
x helm repo add cnpg https://cloudnative-pg.github.io/charts --force-update
x helm upgrade --install cnpg cnpg/cloudnative-pg --version "$CNPG_OPERATOR_CHART_VERSION" \
  -n cnpg-system --create-namespace --wait --timeout 5m

log "namespace and generated state"
x kubectl create namespace agents --dry-run=client -o yaml | x kubectl apply -f -
x python local/tokens.py --state "$STATE" --profiles "${AGENTS_LOCAL_PROFILES:-}" --http-port "$HTTP_PORT"

log "storage and policies"
# k3s restores its own local-path-config on restart, so re-apply every run and restart the
# provisioner so it rereads the shared-filesystem setting.
x kubectl apply -f local/raw/
wait_for 300 "local-path-provisioner" x kubectl -n kube-system get deploy/local-path-provisioner
x kubectl -n kube-system rollout restart deploy/local-path-provisioner
x kubectl -n kube-system rollout status deploy/local-path-provisioner --timeout=180s

x kubectl apply -f "$STATE/secrets.yaml" -o name

log "helmfile sync"
x helmfile -f helmfile.yaml.gotmpl -e local sync --suppress-secrets

log "seeding"
x python local/orch.py --url http://k3s --host-header localhost seed --docker "${AGENTS_LOCAL_DIND:-true}"
x python local/orch.py --url http://k3s --host-header localhost hives "${AGENTS_LOCAL_HIVES:-1}" --wait

printf 'agents local ready: http://localhost:%s\n' "$HTTP_PORT"
