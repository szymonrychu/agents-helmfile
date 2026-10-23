#!/usr/bin/env bash
# Creates the two SOPS-encrypted secrets files the releases need.
# Values only ever travel between processes (pipes, env of a single command);
# nothing is echoed, traced or written to disk unencrypted.
# A target that already exists is never touched, so re-running never rotates.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

ORCH_FILE="values/agent-orchestrator/default.secrets.yaml"
DB_FILE="values/agent-orchestrator-db/default.secrets.yaml"
TOKEN_KEY="CLAUDE_CODE_OAUTH_TOKEN"
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
    -o jsonpath="{.data.$TOKEN_KEY}" | base64 -d)"
  [[ -n "$oauth_value" ]] || die "secret $FOUND_NS/$FOUND_SECRET holds an empty $TOKEN_KEY"
  key_value="$(openssl rand -hex 32)"
  OAUTH_VALUE="$oauth_value" KEY_VALUE="$key_value" \
    jq -n '{claudeOauthToken: $ENV.OAUTH_VALUE, masterKey: $ENV.KEY_VALUE}' |
    encrypt_to "$ORCH_FILE"
}

bootstrap_database() {
  [[ -e "$DB_FILE" ]] && return 0
  [[ -f "$DB_SOURCE" ]] || die "backup credentials source not found: $DB_SOURCE"
  sops --decrypt "$DB_SOURCE" |
    yq -o=json '{"backups": {"s3": {"accessKey": .backups.s3.accessKey, "secretKey": .backups.s3.secretKey}}}' |
    encrypt_to "$DB_FILE"
}

bootstrap_orchestrator
bootstrap_database
echo "secrets bootstrap done (existing files left untouched)" >&2
