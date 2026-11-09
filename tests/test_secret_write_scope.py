"""The Secret write scope policy and the opt-in vault-service NetworkPolicy."""

import re

import yaml

POLICY = "raw/secret-write-scope.pre.yaml"
NETPOL = "raw/vault-service-ingress.disabled.yaml"
SA = "system:serviceaccount:agents:"
# ServiceAccount -> the Secret name prefixes it writes (verified against the vault and
# orchestrator sources: vault k8s.secret_name, orchestrator k8s/secrets.py and k8s/labels.py).
PRESET_OBJ = "ps-aaaa-bbbb-v7"
SCOPES = {
    "agents-vault": ["vault-"],
    "agents-orchestrator-api": ["ps-", "webhook-"],
    "agents-orchestrator-reconciler": ["agent-"],
}


def _docs(repo_root, path) -> list[dict]:
    return [d for d in yaml.safe_load_all((repo_root / path).read_text()) if d]


def _allowed(policy: dict, user: str, op: str, obj: dict) -> bool:
    """Python mirror of the CEL validations: startsWith terms joined by ||."""
    spec = policy["spec"]
    matched = re.findall(r"'([^']+)'", spec["matchConditions"][0]["expression"])
    if user not in matched or op not in spec["matchConstraints"]["resourceRules"][0]["operations"]:
        return True
    target = obj["metadata"]["name"]
    for rule in spec["validations"]:
        expr = " ".join(rule["expression"].split())
        who = re.match(r"variables\.user != '([^']+)'", expr).group(1)
        if who == user:
            return any(target.startswith(p) for p in re.findall(r"startsWith\('([^']+)'\)", expr))
    return False


def test_policy_and_binding_shape(repo_root):
    policy, binding = _docs(repo_root, POLICY)
    assert policy["apiVersion"] == binding["apiVersion"] == "admissionregistration.k8s.io/v1"
    assert policy["kind"] == "ValidatingAdmissionPolicy"
    assert binding["kind"] == "ValidatingAdmissionPolicyBinding"
    assert binding["spec"]["policyName"] == policy["metadata"]["name"]
    assert binding["spec"]["validationActions"] == ["Warn", "Audit"]
    assert binding["spec"]["matchResources"]["namespaceSelector"]["matchLabels"] == {
        "kubernetes.io/metadata.name": "agents"
    }
    rule = policy["spec"]["matchConstraints"]["resourceRules"][0]
    assert rule["resources"] == ["secrets"]
    assert sorted(rule["operations"]) == ["CREATE", "DELETE", "UPDATE"]
    assert policy["spec"]["failurePolicy"] == "Fail"
    assert "oldObject" in str(policy["spec"]["variables"])


def test_each_service_account_has_exactly_its_prefixes(repo_root):
    policy, _ = _docs(repo_root, POLICY)
    cond = policy["spec"]["matchConditions"][0]["expression"]
    assert sorted(re.findall(r"'([^']+)'", cond)) == sorted(SA + n for n in SCOPES)
    by_user = {}
    for rule in policy["spec"]["validations"]:
        expr = " ".join(rule["expression"].split())
        user = re.match(r"variables\.user != '([^']+)'", expr).group(1)
        by_user[user.removeprefix(SA)] = re.findall(r"startsWith\('([^']+)'\)", expr)
    assert by_user == SCOPES


def test_policy_allows_real_names_and_denies_foreign_ones(repo_root):
    policy, _ = _docs(repo_root, POLICY)

    def sec(name):
        return {"metadata": {"name": name}}

    ok = [
        ("agents-vault", "vault-3f1c2a9e-0000-4000-8000-000000000000-v2"),
        ("agents-orchestrator-api", PRESET_OBJ),
        ("agents-orchestrator-api", "webhook-12-password"),
        ("agents-orchestrator-reconciler", "agent-my-session-ccw"),
    ]
    for user, name in ok:
        for op in ("CREATE", "UPDATE", "DELETE"):
            assert _allowed(policy, SA + user, op, sec(name)), (user, name, op)
    denied = [
        ("agents-vault", "agents-orchestrator-master-key"),
        ("agents-vault", PRESET_OBJ),
        ("agents-orchestrator-api", "agents-vault-service-tokens"),
        ("agents-orchestrator-api", "vault-x-v1"),
        ("agents-orchestrator-reconciler", "agents-orchestrator-claude"),
        ("agents-orchestrator-reconciler", PRESET_OBJ),
        ("agents-orchestrator-reconciler", ""),
    ]
    for user, name in denied:
        assert not _allowed(policy, SA + user, "UPDATE", sec(name)), (user, name)
    # Other identities are not matched at all.
    assert _allowed(policy, "system:serviceaccount:agents:default", "DELETE", sec("anything"))


def test_policy_applied_by_the_hook_at_presync(repo_root):
    assert POLICY.endswith(".pre.yaml")
    assert (repo_root / POLICY).is_file()


def test_vault_service_netpol_is_shipped_disabled(repo_root):
    assert not NETPOL.endswith(".pre.yaml")
    (pol,) = _docs(repo_root, NETPOL)
    assert pol["kind"] == "NetworkPolicy"
    assert pol["spec"]["policyTypes"] == ["Ingress"]
    assert pol["spec"]["podSelector"]["matchLabels"]["app.kubernetes.io/component"] == "service"
    (rule,) = pol["spec"]["ingress"]
    assert rule["from"][0]["podSelector"]["matchLabels"]["app.kubernetes.io/name"] == (
        "agents-orchestrator"
    )
    hook = (repo_root / ".hook.sh").read_text()
    assert "*.${SEARCH_SUFF}.yaml" in hook  # only *.pre.yaml / *.post.yaml are applied
