#!/usr/bin/env bash
# Creates the SOPS-encrypted secrets files the releases need.
# The orchestrator file has the chart values shape (claude.token, masterKey.value).
# Values only ever travel between processes (pipes, env of a single command);
# nothing is echoed, traced or written to disk unencrypted.
# A target that already exists is never touched, so re-running never rotates.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

ORCH_FILE="values/agents-orchestrator/default.secrets.yaml"
DB_FILE="values/agents-orchestrator-db/default.secrets.yaml"
WH_FILE="values/agents-webhook/default.secrets.yaml"
WH_TOKEN_FILE="values/agents-orchestrator/service-token-agents-webhook.secrets.yaml"
SCH_FILE="values/agents-schedule/default.secrets.yaml"
SCH_TOKEN_FILE="values/agents-orchestrator/service-token-agents-schedule.secrets.yaml"
TOKEN_KEY="${BOOTSTRAP_TOKEN_KEY:-CLAUDE_CODE_OAUTH_TOKEN}"
# Namespaces searched in order; the first holding the key name wins (tatara, magic-trainer, mtg-engine).
read -r -a NAMESPACES <<<"${BOOTSTRAP_NAMESPACES:-tatara magic-trainer mtg-engine}"
INFRA_HELMFILE="${INFRA_HELMFILE:-$HOME/Documents/infrastructure/code/helmfile}"
DB_SOURCE="${DB_SOURCE:-$INFRA_HELMFILE/helmfiles/media/values/jellystat-database/default.secrets.yaml}"

die() {
  echo "ERROR: $*" >&2
  exit 1
}

# encrypt_to <target>: plaintext JSON on stdin, SOPS yaml written to <target>.
# --filename-override makes sops apply the creation rule for the final path.
encrypt_to() {
  local target="$1" tmp
  mkdir -p "$(dirname "$target")"
  tmp="$(mktemp "$target.XXXXXX")"
  if sops --encrypt --filename-override "$target" \
    --input-type json --output-type yaml /dev/stdin >"$tmp"; then
    mv "$tmp" "$target"
  else
    rm -f "$tmp"
    die "encryption of $target failed"
  fi
}

find_token_secret() {
  local ns name
  for ns in "${NAMESPACES[@]}"; do
    name="$(kubectl -n "$ns" get secrets -o json |
      jq -r --arg k "$TOKEN_KEY" '.items[] | select(.data | has($k)) | .metadata.name' |
      head -n 1)" || name=""
    if [[ -n "$name" ]]; then
      FOUND_NS="$ns"
      FOUND_SECRET="$name"
      return 0
    fi
  done
  return 1
}

bootstrap_orchestrator() {
  [[ -e "$ORCH_FILE" ]] && return 0
  local FOUND_NS FOUND_SECRET oauth_value key_value
  find_token_secret || die "no secret with key $TOKEN_KEY in namespaces: ${NAMESPACES[*]}"
  oauth_value="$(kubectl -n "$FOUND_NS" get secret "$FOUND_SECRET" \
    -o jsonpath="{.data['$TOKEN_KEY']}" | base64 -d)"
  [[ -n "$oauth_value" ]] || die "secret $FOUND_NS/$FOUND_SECRET holds an empty $TOKEN_KEY"
  key_value="$(openssl rand -hex 32)"
  OAUTH_VALUE="$oauth_value" KEY_VALUE="$key_value" \
    jq -n '{claude: {token: $ENV.OAUTH_VALUE}, masterKey: {value: $ENV.KEY_VALUE}}' |
    encrypt_to "$ORCH_FILE"
}

bootstrap_database() {
  [[ -e "$DB_FILE" ]] && return 0
  [[ -f "$DB_SOURCE" ]] || die "backup credentials source not found: $DB_SOURCE"
  sops --decrypt "$DB_SOURCE" |
    yq -o=json '{"backups": {"s3": {"accessKey": .backups.s3.accessKey, "secretKey": .backups.s3.secretKey}}}' |
    encrypt_to "$DB_FILE"
}

# bootstrap_service_token <service> <service file> <orchestrator hash file>:
# both files or neither. One service token (its sha256 goes to the orchestrator).
# Never rotates, never half-writes.
bootstrap_service_token() {
  local service="$1" svc_file="$2" hash_file="$3" svc digest
  if [[ -e "$svc_file" && -e "$hash_file" ]]; then return 0; fi
  if [[ -e "$svc_file" || -e "$hash_file" ]]; then
    die "exactly one of $svc_file and $hash_file exists; refusing to rotate or half-write"
  fi
  svc="$(openssl rand -hex 32)"
  digest="$(printf '%s' "$svc" | openssl dgst -sha256 -r | cut -d' ' -f1)"
  SVC="$svc" jq -n '{orchestrator: {token: $ENV.SVC}}' |
    encrypt_to "$svc_file"
  if ! DIGEST="$digest" SERVICE="$service" \
    jq -n '{serviceApi: {tokenHashes: {($ENV.SERVICE): $ENV.DIGEST}}}' |
    encrypt_to "$hash_file"; then
    rm -f "$svc_file"
    die "writing $hash_file failed; removed $svc_file"
  fi
}

bootstrap_orchestrator
bootstrap_database
bootstrap_service_token agents-webhook "$WH_FILE" "$WH_TOKEN_FILE"
bootstrap_service_token agents-schedule "$SCH_FILE" "$SCH_TOKEN_FILE"
echo "secrets bootstrap done (existing files left untouched)" >&2
