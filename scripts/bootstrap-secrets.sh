#!/usr/bin/env bash
# Creates the SOPS-encrypted secrets files the releases need.
# The orchestrator file has the chart values shape (claude.token, masterKey.value).
# Values only ever travel between processes (pipes, env of a single command);
# nothing is echoed, traced or written to disk unencrypted.
# A target that already exists is never touched, so re-running never rotates.
set -euo pipefail
# Off even when the caller turned xtrace on, so no value reaches a trace.
set +x

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

ORCH_FILE="values/agents-orchestrator/default.secrets.yaml"
DB_FILE="values/agents-orchestrator-db/default.secrets.yaml"
WH_FILE="values/agents-webhook/default.secrets.yaml"
WH_TOKEN_FILE="values/agents-orchestrator/service-token-agents-webhook.secrets.yaml"
SCH_FILE="values/agents-schedule/default.secrets.yaml"
SCH_TOKEN_FILE="values/agents-orchestrator/service-token-agents-schedule.secrets.yaml"
MX_FILE="values/agents-matrix-connector/default.secrets.yaml"
MX_TOKEN_FILE="values/agents-orchestrator/service-token-agents-matrix-connector.secrets.yaml"
CHAT_FILE="values/agents-orchestrator/chat-token.secrets.yaml"
CHAT_HASH_FILE="values/agents-matrix-connector/agent-token.secrets.yaml"
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

# bootstrap_matrix_connector: both files or neither. Needs MATRIX_CONNECTOR_TOKEN_FILE
# (path to a file holding the Matrix access token) only when it has to write.
# One service token (its sha256 goes to the orchestrator). Never rotates, never half-writes.
bootstrap_matrix_connector() {
  local svc digest matrix
  if [[ -e "$MX_FILE" && -e "$MX_TOKEN_FILE" ]]; then return 0; fi
  if [[ -e "$MX_FILE" || -e "$MX_TOKEN_FILE" ]]; then
    die "exactly one of $MX_FILE and $MX_TOKEN_FILE exists; refusing to rotate or half-write"
  fi
  [[ -n "${MATRIX_CONNECTOR_TOKEN_FILE:-}" && -s "$MATRIX_CONNECTOR_TOKEN_FILE" ]] ||
    die "MATRIX_CONNECTOR_TOKEN_FILE must name a non-empty file holding the Matrix access token"
  matrix="$(tr -d '\r\n' <"$MATRIX_CONNECTOR_TOKEN_FILE")"
  [[ -n "$matrix" ]] || die "MATRIX_CONNECTOR_TOKEN_FILE holds an empty token"
  svc="$(openssl rand -hex 32)"
  digest="$(printf '%s' "$svc" | openssl dgst -sha256 -r | cut -d' ' -f1)"
  SVC="$svc" MATRIX="$matrix" \
    jq -n '{orchestrator: {token: $ENV.SVC}, matrix: {accessToken: $ENV.MATRIX}}' |
    encrypt_to "$MX_FILE"
  if ! DIGEST="$digest" \
    jq -n '{serviceApi: {tokenHashes: {"agents-matrix-connector": $ENV.DIGEST}}}' |
    encrypt_to "$MX_TOKEN_FILE"; then
    rm -f "$MX_FILE"
    die "writing $MX_TOKEN_FILE failed; removed $MX_FILE"
  fi
}

# bootstrap_chat_token: both files or neither. The chat token the orchestrator sends to the
# connector's agent listener; the connector gets only its sha256. Never rotates, never half-writes.
bootstrap_chat_token() {
  local chat digest
  if [[ -e "$CHAT_FILE" && -e "$CHAT_HASH_FILE" ]]; then return 0; fi
  if [[ -e "$CHAT_FILE" || -e "$CHAT_HASH_FILE" ]]; then
    die "exactly one of $CHAT_FILE and $CHAT_HASH_FILE exists; refusing to rotate or half-write"
  fi
  chat="$(openssl rand -hex 32)"
  digest="$(printf '%s' "$chat" | openssl dgst -sha256 -r | cut -d' ' -f1)"
  CHAT="$chat" jq -n '{chat: {token: $ENV.CHAT}}' |
    encrypt_to "$CHAT_FILE"
  if ! DIGEST="$digest" jq -n '{agentApi: {tokenSha256: $ENV.DIGEST}}' |
    encrypt_to "$CHAT_HASH_FILE"; then
    rm -f "$CHAT_FILE"
    die "writing $CHAT_HASH_FILE failed; removed $CHAT_FILE"
  fi
}

bootstrap_orchestrator
bootstrap_database
bootstrap_service_token agents-webhook "$WH_FILE" "$WH_TOKEN_FILE"
bootstrap_service_token agents-schedule "$SCH_FILE" "$SCH_TOKEN_FILE"
bootstrap_matrix_connector
bootstrap_chat_token
echo "secrets bootstrap done (existing files left untouched)" >&2
