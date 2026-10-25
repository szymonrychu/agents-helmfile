# agents-helmfile

Component `agents-helmfile` (generic) of the claude-code-tatara project. Its own tests, `MEMORY.md` and `ROADMAP.md` live here.

Run `mise run test`, `mise run lint` and `mise run fmt` from this directory.

## Releases and names

- Releases: `agents-orchestrator-db` (CNPG), `agents-orchestrator`, `agents-webhook-db`, `agents-webhook` and
  `agents-web`. The orchestrator's Secrets are `agents-orchestrator-claude`, `-master-key`, `-services`,
  `-proxy` (SSO, from terraform) and `-tls` (cert-manager); its values live in `values/agents-orchestrator/`.
- The orchestrator's unit is a session (`/api/v1/sessions`, label `agents.szymonrichert.pl/session`).
  `tests/test_nomenclature.py` guards every tracked file against the old unit word and the old project name.
- Cutover: the retired app release is declared with `installed: false`, the new database imports the old one, and
  the old database release stays declared, hibernated, as the rollback point (README, "Cutover to
  agents-orchestrator").
