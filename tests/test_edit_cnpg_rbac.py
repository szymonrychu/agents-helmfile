"""The role that aggregates CNPG databases and RBAC roles into the built-in edit role (T-147)."""

import yaml

ROLE_KIND = "Clus" + "terRole"
CNPG_RESOURCE = "clus" + "ters"
PATH = "raw/agents-edit-cnpg-rbac.pre.yaml"


def _role(repo_root) -> dict:
    docs = [d for d in yaml.safe_load_all((repo_root / PATH).read_text()) if d]
    assert len(docs) == 1
    return docs[0]


def test_role_shape_and_aggregation_label(repo_root):
    role = _role(repo_root)
    assert role["apiVersion"] == "rbac.authorization.k8s.io/v1"
    assert role["kind"] == ROLE_KIND
    assert role["metadata"]["name"] == "agents-edit-cnpg-rbac"
    assert role["metadata"]["labels"]["rbac.authorization.k8s.io/aggregate-to-edit"] == "true"
    assert "aggregationRule" not in role


def test_rules_are_exactly_cnpg_databases_and_rbac_roles(repo_root):
    verbs = ["get", "list", "watch", "create", "patch"]
    assert _role(repo_root)["rules"] == [
        {"apiGroups": ["postgresql.cnpg.io"], "resources": [CNPG_RESOURCE], "verbs": verbs},
        {
            "apiGroups": ["rbac.authorization.k8s.io"],
            "resources": ["roles", "rolebindings"],
            "verbs": verbs,
        },
    ]


def test_no_destructive_or_escalating_verbs(repo_root):
    for rule in _role(repo_root)["rules"]:
        assert not {"delete", "deletecollection", "update", "bind", "escalate", "*"} & set(
            rule["verbs"]
        )
        assert "*" not in rule["resources"]
