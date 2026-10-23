#!/bin/bash
set -e
set -o nounset
set -o pipefail

readonly CURRENT_PWD="$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")"

if [[ -z "${DEFAULT_TIMEOUT_S:-}" ]]; then
  readonly DEFAULT_TIMEOUT_S='600'
fi

fail() {
    local output="${1}"
    printf "%s\n" "${output}" >&2
    exit 1
}

# issue #26: every side-effecting command used to run through
# `find ... -exec bash -xec "..." \;`, which discards the exec'd command's
# exit status (find's own rc only reflects find-internal errors) and, for the
# `sops -d {} | kubectl ...` sites, started a fresh subshell whose `pipefail`
# was never inherited from this script. Both together meant a hard-failing
# `kubectl apply`/`sops -d` was invisible: `set -e` never tripped and the
# release reported synced (see MEMORY.md, 2026-07-12, vpa-recommender).
#
# `apply_dir` below runs the loop body in *this* shell via process
# substitution (`< <(find ... -print0)` rather than `find ... | while ...`),
# so it inherits `set -e`/`pipefail` directly - one construct for all 8
# call sites instead of eight independent `bash -xec` strings to keep in
# sync.
#
# `prepare` is the one event where a non-zero `kubectl diff` is not
# necessarily a failure: rc=1 means "differences present" (by design), rc>1
# is a genuine error. `run_kubectl`/`run_kubectl_secrets` special-case that
# event so a real raw/ drift does not abort the hook; DRIFT_DETECTED is
# tracked and turned into exit code 2 at the very end - `prepare` fires on
# every `helmfile` operation, including real `apply`/`sync` runs
# which run with
# `--rollback-on-failure`. Exiting non-zero mid-script on drift alone would
# roll back a healthy release every time raw/ merely differs from live,
# which is a worse outage than the bug this fixes. presync/postsync only
# ever run `kubectl apply`, where any non-zero is unambiguously a real
# failure, so those events abort immediately without this rc-banding.
DRIFT_DETECTED=0

run_kubectl() {
    local file="${1}"
    if [[ "${EVENT_NAME}" == "prepare" ]]; then
        set +e
        ${KUBECTL} -n "${RELEASE_NAMESPACE}" -f "${file}"
        local rc=$?
        set -e
        handle_diff_rc "${rc}" "${file}"
    else
        ${KUBECTL} -n "${RELEASE_NAMESPACE}" -f "${file}"
    fi
}

run_kubectl_secrets() {
    local file="${1}"
    if [[ "${EVENT_NAME}" == "prepare" ]]; then
        set +e
        sops -d "${file}" | ${KUBECTL} -n "${RELEASE_NAMESPACE}" -f -
        local rc=$?
        set -e
        handle_diff_rc "${rc}" "${file}"
    else
        sops -d "${file}" | ${KUBECTL} -n "${RELEASE_NAMESPACE}" -f -
    fi
}

handle_diff_rc() {
    local rc="${1}" file="${2}"
    if [[ "${rc}" -eq 1 ]]; then
        printf "Drift detected in '%s'\n" "${file}"
        DRIFT_DETECTED=1
    elif [[ "${rc}" -gt 1 ]]; then
        fail "kubectl diff failed (rc=${rc}) on '${file}'"
    fi
}

run_hook_script() {
    local script="${1}"
    bash -xe "${script}" "${RELEASE_NAME}" "${RELEASE_NAMESPACE}" "${RELEASE_VERSION}"
}

# $1 dir, $2 find -name pattern, $3 mode: plain|secrets|hook
apply_dir() {
    local dir="${1}" pattern="${2}" mode="${3}"
    [[ -d "${dir}" ]] || return 0
    # `prepare` also fires on a bare `helmfile build`, which may have no cluster or
    # no decryption key. Skip the work then; a decrypt failure during a REAL apply
    # must stay fatal, so its rc is never banded into handle_diff_rc.
    if [[ "${EVENT_NAME}" == "prepare" ]] && [[ "${CLUSTER_REACHABLE}" -eq 0 ]]; then
        return 0
    fi
    while IFS= read -r -d '' f; do
        case "${mode}" in
            secrets) run_kubectl_secrets "${f}" ;;
            hook) run_hook_script "${f}" ;;
            *) run_kubectl "${f}" ;;
        esac
    done < <(find "${dir}" -name "${pattern}" -print0)
}

readonly HELMFILE_DIR="${1:-}"
readonly EVENT_NAME="${2:-}"
readonly RELEASE_NAME="${3:-}"
readonly RELEASE_NAMESPACE="${4:-}"
readonly RELEASE_VERSION="${5:-}"
# 6th arg is the release's helmfile `timeout` (seconds), passed by the
# helmfile hook args. It sizes how long we wait for a prior operation on this
# release to settle before reaping (issue #12, sub-problem 3B): a release like
# a slow release legitimately installs for hours (large model pull) and declares
# a long `timeout`, so the old hard-coded 600 s would fire while helm is still
# working. A release with no explicit timeout renders this arg empty (or Go
# template's '<no value>'); fall back to the default so the arithmetic below
# stays valid.
TIMEOUT_S="${6:-$DEFAULT_TIMEOUT_S}"
if ! [[ "${TIMEOUT_S}" =~ ^[0-9]+$ ]]; then
  TIMEOUT_S="${DEFAULT_TIMEOUT_S}"
fi
readonly TIMEOUT_S

[[ -z "${HELMFILE_DIR}" ]] && fail "Missing 1st parameter HELMFILE_DIR"
[[ -z "${EVENT_NAME}" ]] && fail "Missing 2nd parameter EVENT_NAME"

if [[ "${EVENT_NAME}" == "presync" ]]; then
    readonly KUBECTL="kubectl apply --wait=true"
    readonly SEARCH_SUFF="pre"
elif [[ "${EVENT_NAME}" == "prepare" ]]; then
    readonly KUBECTL="kubectl diff"
    readonly SEARCH_SUFF="pre"
elif [[ "${EVENT_NAME}" == "postsync" ]]; then
    readonly KUBECTL="kubectl apply --wait=true"
    readonly SEARCH_SUFF="post"
else
    fail "Not implemented event: '${EVENT_NAME}'"
fi
readonly TIMEOUT_TMSTP="$(($(date +%s) + TIMEOUT_S))"

# Probed ONCE, and only for `prepare` (a bare `helmfile build` may have no cluster).
# presync/postsync only run during a real apply, where an unreachable cluster is a
# genuine failure helmfile itself reports.
CLUSTER_REACHABLE=1
if [[ "${EVENT_NAME}" == "prepare" ]] && ! kubectl cluster-info > /dev/null 2>&1; then
    CLUSTER_REACHABLE=0
    printf "No reachable cluster during 'prepare'; skipping raw/ diff and pre hooks\n"
fi
readonly CLUSTER_REACHABLE

if [[ "${EVENT_NAME}" == "presync" ]] && [[ -n "${RELEASE_NAMESPACE}" ]]; then
    # raw/ (regcred) is applied before helm creates the namespace on first install.
    printf "Ensuring namespace '%s' exists\n" "${RELEASE_NAMESPACE}"
    kubectl create namespace "$RELEASE_NAMESPACE" --dry-run=client -o yaml | kubectl apply -f -
fi
if [[ ! -z "${RELEASE_NAME}" ]] && [[ ! -z "${RELEASE_NAMESPACE}" ]]; then

    if [[ "${EVENT_NAME}" == "presync" ]] && kubectl get namespace "${RELEASE_NAMESPACE}" > /dev/null 2>&1; then
        printf "Checking '%s' helm relase health in '%s' namespace\n" "${RELEASE_NAME}" "${RELEASE_NAMESPACE}"

        faulty_release_revision=""
        while true; do
            release_json="$(helm list --all-namespaces --output json | jq -r ".[] | select(.name==\"${RELEASE_NAME}\" and .namespace==\"${RELEASE_NAMESPACE}\")")"
            if [[ -z "${release_json}" ]]; then
                printf "Release ${RELEASE_NAMESPACE}/${RELEASE_NAME} is missing, it's a fresh start!\n"
                break
            fi
            release_state="$(echo "${release_json}" | jq -r ".status")"
            if [[ "${release_state}" == "deployed" ]]; then
                printf "Release ${RELEASE_NAMESPACE}/${RELEASE_NAME} ok!\n"
                break
            elif [[ "${release_state}" == "failed" ]]; then
                printf "Previous release ${RELEASE_NAMESPACE}/${RELEASE_NAME} was 'failed', deleting faulty release from helm history!\n"
                release_revision="$(echo "${release_json}" | jq -r ".revision")"
                kubectl delete secret -n "${RELEASE_NAMESPACE}" "sh.helm.release.v1.${RELEASE_NAME}.v${release_revision}"
                break
            elif [[ "$(date +%s)" -ge "${TIMEOUT_TMSTP}" ]]; then
                # Concurrency guard (issue #12, sub-problem 3A): a release in a
                # pending-* state has a genuine helm operation in flight - most
                # often a sibling default-branch `helmfile apply` from a Renovate
                # auto-merge burst, or a legitimately slow install. Deleting its
                # history secret here would corrupt that in-flight release (helm
                # would lose the release and treat the next reconcile as a fresh
                # install). Only reap releases that are actually stuck; the
                # `failed` case above is the sole state we delete on. Never touch
                # a pending one - back off and let the in-flight operation win.
                if [[ "${release_state}" == pending-* ]]; then
                    printf "Timeout waiting for release ${RELEASE_NAMESPACE}/${RELEASE_NAME}, but it is '${release_state}' (operation in progress) - leaving helm history intact.\n"
                    break
                fi
                printf "Timeout waiting for release ${RELEASE_NAMESPACE}/${RELEASE_NAME} to stabilize reached, deleting faulty release from helm history!\n"
                release_revision="$(echo "${release_json}" | jq -r ".revision")"
                kubectl delete secret -n "${RELEASE_NAMESPACE}" "sh.helm.release.v1.${RELEASE_NAME}.v${release_revision}"
                break
            fi
            sleep 1
        done
    fi

    readonly RELEASE_DIR="${CURRENT_PWD}/values/${RELEASE_NAME}"
    if [[ -d "${RELEASE_DIR}/raw" ]]; then
        printf "Searching for files to apply for '%s' in '%s' namespace!\n" "${RELEASE_NAME}" "${RELEASE_NAMESPACE}"
        apply_dir "${RELEASE_DIR}/raw" "*.common.${SEARCH_SUFF}.yaml" plain
        apply_dir "${RELEASE_DIR}/raw" "*.common.${SEARCH_SUFF}.secrets.yaml" secrets
        apply_dir "${RELEASE_DIR}/raw" "*.${RELEASE_NAME}.${SEARCH_SUFF}.yaml" plain
        apply_dir "${RELEASE_DIR}/raw" "*.${RELEASE_NAME}.${SEARCH_SUFF}.secrets.yaml" secrets
    fi
    if [[ -d "${RELEASE_DIR}/hooks" ]]; then
        printf "Searching for hook scripts to run for '%s' in '%s'!\n" "${RELEASE_NAME}" "${RELEASE_NAMESPACE}"
        apply_dir "${RELEASE_DIR}/hooks" "*.common.${SEARCH_SUFF}.sh" hook
        apply_dir "${RELEASE_DIR}/hooks" "*.${RELEASE_NAME}.${SEARCH_SUFF}.sh" hook
    fi
fi
printf "Running hook globally during '%s'\n" "${EVENT_NAME}"
if [[ -d "${CURRENT_PWD}/raw" ]]; then
    printf "Searching for files to apply!\n"
    apply_dir "${CURRENT_PWD}/raw" "*.${SEARCH_SUFF}.yaml" plain
    apply_dir "${CURRENT_PWD}/raw" "*.${SEARCH_SUFF}.secrets.yaml" secrets
fi

if [[ "${EVENT_NAME}" == "prepare" ]] && [[ "${DRIFT_DETECTED}" -eq 1 ]]; then
    # Drift during `prepare` only prints: presync is what applies raw/, so
    # failing here would block the very apply that converges the drift.
    printf "raw/ drift detected during 'prepare'\n"
fi
exit 0
