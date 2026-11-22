#!/usr/bin/env python3
"""Generate the local-mode service tokens, per-release values and Secret manifests.

Writes under --state: tokens.json (name -> value, never rotated), values/<release>.yaml for
every app release and secrets.yaml (Secrets for namespace agents). Prints only paths and
counts, never a value. Stdlib only; the YAML files are written as JSON, which is valid YAML.
"""

import argparse
import base64
import hashlib
import json
import os
import secrets
import sys
from collections.abc import Mapping
from pathlib import Path

NAMESPACE = "agents"
REGISTRY = "harbor.szymonrichert.pl"
# Optional releases by profile: the release, the service token the orchestrator holds the hash
# of, its admin relay upstream (key, url, token name) if any, and its values beyond
# orchestrator.token.
PROFILES = {
    "webhook": (
        "agents-webhook",
        "svc_webhook",
        ("webhook", "http://agents-webhook-api:8001", "up_webhook_admin"),
        lambda t, public: {"endpointKey": t["webhook_endpoint_key"], "publicBaseUrl": public},
    ),
    "schedule": (
        "agents-schedule",
        "svc_schedule",
        ("schedule", "http://agents-schedule-api:8001", "up_schedule_admin"),
        lambda t, public: {},
    ),
    "app-connector": (
        "agents-app-connector",
        "svc_app_connector",
        None,
        lambda t, public: {"taskboard": {"token": t["taskboard_delegate"]}},
    ),
}
HEX_NAMES = (
    "master_key",
    "svc_vault",
    "svc_taskboard",
    "svc_mindmap",
    "svc_usage",
    "svc_webhook",
    "svc_schedule",
    "svc_app_connector",
    "up_usage_orch",
    "up_usage_taskboard",
    "up_taskboard_relay",
    "up_mindmap_relay",
    "up_vault",
    "up_history",
    "up_webhook_admin",
    "up_schedule_admin",
    "webhook_schedule",
    "taskboard_delegate",
    "mindmap_taskboard",
    "ingest_usage",
    "ingest_history",
)


def ensure_tokens(existing: dict[str, str], env: Mapping[str, str]) -> dict[str, str]:
    """Return existing plus every declared name still missing; only `claude` follows the env."""
    out = dict(existing)
    for name in HEX_NAMES:
        out.setdefault(name, secrets.token_hex(32))
    out.setdefault("webhook_endpoint_key", base64.b64encode(secrets.token_bytes(32)).decode())
    real = env.get("CLAUDE_CODE_OAUTH_TOKEN")
    if real:
        out["claude"] = real
    elif "claude" not in out:
        out["claude"] = f"fake-{secrets.token_hex(16)}"
    return out


def _sha256(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def release_values(tokens: Mapping[str, str], profiles: frozenset[str], http_port: int) -> dict:
    t = tokens
    hashes = {
        "agents-vault": _sha256(t["svc_vault"]),
        "agents-taskboard": _sha256(t["svc_taskboard"]),
        "agents-mindmap": _sha256(t["svc_mindmap"]),
        "agents-usage": _sha256(t["svc_usage"]),
    }
    admin = {"history": {"token": t["up_history"]}}
    public = f"http://localhost:{http_port}"
    optional: dict[str, dict] = {}
    for profile in sorted(profiles):
        release, service_token, relay, extra = PROFILES[profile]
        hashes[release] = _sha256(t[service_token])
        if relay:
            key, url, token = relay
            admin[key] = {"url": url, "token": t[token]}
        optional[release] = {"orchestrator": {"token": t[service_token]}, **extra(t, public)}
    return {
        "agents-orchestrator": {
            "claude": {"token": t["claude"], "credentialExpiresAt": None},
            "masterKey": {"value": t["master_key"]},
            "serviceApi": {"tokenHashes": hashes},
            "usage": {"token": t["up_usage_orch"]},
            "taskboard": {"token": t["up_taskboard_relay"]},
            "mindmap": {"token": t["up_mindmap_relay"]},
            "vault": {"token": t["up_vault"]},
            "admin": admin,
            "env": {"ORCH_ALLOWED_ORIGINS": public},
        },
        "agents-vault": {"orchestrator": {"token": t["svc_vault"]}},
        "agents-usage": {"orchestrator": {"token": t["svc_usage"]}},
        "agents-taskboard": {
            "orchestrator": {"token": t["svc_taskboard"]},
            "relayToken": t["up_taskboard_relay"],
            "delegateTokens": t["taskboard_delegate"],
            "mindmapTokens": t["mindmap_taskboard"],
            "usage": {"token": t["up_usage_taskboard"]},
            "publicUrl": public,
        },
        "agents-mindmap": {"publicBaseUrl": public},
        "agents-history": {},
        "agents-web": {},
        "agents-embeddings": {},
        **optional,
    }


def _secret(name: str, key: str, value: str) -> dict:
    return {
        "apiVersion": "v1",
        "kind": "Secret",
        "metadata": {"name": name, "namespace": NAMESPACE},
        "type": "Opaque",
        "stringData": {key: value},
    }


def secret_manifests(
    tokens: Mapping[str, str], harbor_user: str, harbor_password: str
) -> list[dict]:
    t = tokens
    auth = base64.b64encode(f"{harbor_user}:{harbor_password}".encode()).decode()
    dockercfg = json.dumps({"auths": {REGISTRY: {"auth": auth}}})
    regcred = {
        "apiVersion": "v1",
        "kind": "Secret",
        "metadata": {"name": "regcred", "namespace": NAMESPACE},
        "type": "kubernetes.io/dockerconfigjson",
        "data": {".dockerconfigjson": base64.b64encode(dockercfg.encode()).decode()},
    }
    return [
        regcred,
        _secret(
            "agents-usage-service-tokens",
            "tokens",
            f"{t['up_usage_orch']}\n{t['up_usage_taskboard']}",
        ),
        _secret("agents-usage-ingest", "token", t["ingest_usage"]),
        _secret("agents-history-service-tokens", "tokens", t["up_history"]),
        _secret("agents-history-ingest", "token", t["ingest_history"]),
        _secret("agents-vault-service-tokens", "tokens", t["up_vault"]),
        _secret("agents-mindmap-orchestrator-token", "token", t["svc_mindmap"]),
        _secret("agents-mindmap-relay-token", "token", t["up_mindmap_relay"]),
        _secret("agents-mindmap-taskboard-token", "token", t["mindmap_taskboard"]),
        _secret("agents-webhook-admin-tokens", "tokens", t["up_webhook_admin"]),
        _secret("agents-webhook-schedule-token", "token", t["webhook_schedule"]),
        _secret(
            "agents-schedule-admin-tokens",
            "tokens",
            f"{t['up_schedule_admin']}\n{t['webhook_schedule']}",
        ),
    ]


def _write_private(path: Path, text: str) -> None:
    tmp = path.with_name(f".{path.name}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as fh:
        fh.write(text)
    tmp.chmod(0o600)
    os.replace(tmp, path)


def _missing_env(env: Mapping[str, str]) -> str | None:
    for var in ("HARBOR_USERNAME", "HARBOR_PASSWORD"):
        if not env.get(var):
            return var
    if not env.get("CLAUDE_CODE_OAUTH_TOKEN") and env.get("AGENTS_LOCAL_FAKE_CLAUDE") != "1":
        return "CLAUDE_CODE_OAUTH_TOKEN"
    return None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state", required=True, type=Path)
    parser.add_argument("--profiles", default="")
    parser.add_argument("--http-port", type=int, default=8080)
    args = parser.parse_args(argv)
    env = os.environ
    missing = _missing_env(env)
    if missing:
        print(f"error: required environment variable {missing} is empty or unset", file=sys.stderr)
        return 2
    profiles = frozenset(p for p in args.profiles.split(",") if p)
    unknown = profiles - set(PROFILES)
    if unknown:
        print(f"error: unknown profile(s): {', '.join(sorted(unknown))}", file=sys.stderr)
        return 2

    state: Path = args.state
    state.mkdir(mode=0o700, parents=True, exist_ok=True)
    state.chmod(0o700)
    (state / "values").mkdir(mode=0o700, exist_ok=True)
    tokens_path = state / "tokens.json"
    try:
        existing = json.loads(tokens_path.read_text()) if tokens_path.exists() else {}
        if not isinstance(existing, dict):
            raise TypeError("not an object")
    except (ValueError, TypeError):
        print(f"error: {tokens_path} is not valid JSON; fix or delete it", file=sys.stderr)
        return 2
    tokens = ensure_tokens(existing, env)
    _write_private(tokens_path, json.dumps(tokens, indent=2) + "\n")

    for stale in (state / "values").glob("*.yaml"):
        stale.unlink()
    values = release_values(tokens, profiles, args.http_port)
    for release, doc in values.items():
        _write_private(state / "values" / f"{release}.yaml", json.dumps(doc, indent=2) + "\n")
    manifests = secret_manifests(tokens, env["HARBOR_USERNAME"], env["HARBOR_PASSWORD"])
    _write_private(
        state / "secrets.yaml", "".join("---\n" + json.dumps(m, indent=2) + "\n" for m in manifests)
    )
    print(f"{tokens_path} ({len(tokens)} tokens)")
    print(f"{state / 'values'} ({len(values)} release value files)")
    print(f"{state / 'secrets.yaml'} ({len(manifests)} secrets)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
