#!/usr/bin/env bash
set -Eeuo pipefail

readonly EXPECTED_UPSTREAM_REMOTE="https://github.com/onyx-dot-app/onyx.git"
readonly EXPECTED_UPSTREAM_TAG="v4.3.1"
readonly EXPECTED_UPSTREAM_COMMIT="c8ba005da719ad880afbff08b067241eb3f9adc1"
readonly EXPECTED_UPSTREAM_TREE="25e636429b1161a161aabfa40e9b29cf4b70b778"
readonly PYTHON_BASE_IMAGE="docker.io/library/python:3.13-slim@sha256:b04b5d7233d2ad9c379e22ea8927cd1378cd15c60d4ef876c065b25ea8fb3bf3"
readonly UV_COPY_LINE="COPY --from=ghcr.io/astral-sh/uv:0.11.25@sha256:1e3808aa9023d0980e7c15b1fa7c1ac16ff35925780cf5c459858b2d693f01a9 /uv /uvx /bin/"

readonly SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
readonly REPO_ROOT="$(git -C "${SCRIPT_DIR}/.." rev-parse --show-toplevel)"
readonly PROVENANCE_FILE="${REPO_ROOT}/SKYBASE_UPSTREAM.md"
readonly IMAGE_FILE="${REPO_ROOT}/backend/Dockerfile.skybase-ce"
readonly REQUIREMENTS_FILE="${REPO_ROOT}/backend/requirements/skybase-ce.txt"
readonly WORKER_FILE="${REPO_ROOT}/backend/supervisord.skybase-ce.conf"
readonly T1_ALLOWED_OVERLAY_PATHS=(
    "SKYBASE_UPSTREAM.md"
    "scripts/verify-skybase-provenance.sh"
    "backend/Dockerfile.skybase-ce"
    "backend/requirements/skybase-ce.txt"
    "backend/supervisord.skybase-ce.conf"
)
readonly COPIED_SOURCE_ROOTS=(
    "backend/alembic"
    "backend/onyx"
    "backend/shared_configs"
    "backend/static"
)

fail() {
    printf 'skybase provenance verification failed: %s\n' "$*" >&2
    exit 1
}

require_file() {
    local file_path="$1"
    [[ -f "${file_path}" ]] || fail "missing required file: ${file_path}"
}

require_fixed_line() {
    local file_path="$1"
    local expected_line="$2"
    grep -Fqx -- "${expected_line}" "${file_path}" || \
        fail "missing expected line in ${file_path}: ${expected_line}"
}

require_disabled_env() {
    local env_name="$1"
    local env_values
    local value_count

    env_values="$(awk -v env_name="${env_name}" '
        $1 == "ENV" {
            in_env_block = 1
        }
        in_env_block {
            for (field_index = 1; field_index <= NF; field_index++) {
                if ($field_index ~ "^" env_name "=") {
                    env_value = $field_index
                    sub("^" env_name "=", "", env_value)
                    sub(/^"/, "", env_value)
                    sub(/"$/, "", env_value)
                    print env_value
                }
            }
            if ($0 !~ /\\[[:space:]]*$/) {
                in_env_block = 0
            }
        }
    ' "${IMAGE_FILE}")"
    value_count="$(printf '%s\n' "${env_values}" | sed '/^$/d' | wc -l | tr -d ' ')"

    [[ "${value_count}" == "1" && "${env_values}" == "false" ]] || \
        fail "${env_name} must be assigned exactly once and set to false"
}

require_t1_overlay_path() {
    local changed_path="$1"
    local allowed_path

    for allowed_path in "${T1_ALLOWED_OVERLAY_PATHS[@]}"; do
        [[ "${changed_path}" == "${allowed_path}" ]] && return 0
    done

    fail "T1 overlay path is not allowlisted: ${changed_path}"
}

verify_t1_overlay_allowlist() {
    local changed_path
    local copied_root
    local ee_descendant

    for copied_root in "${COPIED_SOURCE_ROOTS[@]}"; do
        ee_descendant="$(find "${REPO_ROOT}/${copied_root}" -path '*/ee' -print -quit)"
        [[ -z "${ee_descendant}" ]] || \
            fail "CE image copied source root contains an ee descendant: ${ee_descendant}"
    done

    while IFS= read -r changed_path; do
        [[ -n "${changed_path}" ]] && require_t1_overlay_path "${changed_path}"
    done < <(git -C "${REPO_ROOT}" diff --name-only "${EXPECTED_UPSTREAM_COMMIT}...HEAD")

    while IFS= read -r changed_path; do
        [[ -n "${changed_path}" ]] && require_t1_overlay_path "${changed_path}"
    done < <(git -C "${REPO_ROOT}" diff --name-only "${EXPECTED_UPSTREAM_COMMIT}")

    while IFS= read -r -d '' changed_path; do
        require_t1_overlay_path "${changed_path}"
    done < <(git -C "${REPO_ROOT}" ls-files --others --exclude-standard -z)

    while IFS= read -r -d '' changed_path; do
        require_t1_overlay_path "${changed_path}"
    done < <(git -C "${REPO_ROOT}" ls-files --others --ignored --exclude-standard -z)
}

for required_file in \
    "${PROVENANCE_FILE}" \
    "${IMAGE_FILE}" \
    "${REQUIREMENTS_FILE}" \
    "${WORKER_FILE}"; do
    require_file "${required_file}"
done

upstream_remote="$(git -C "${REPO_ROOT}" remote get-url upstream 2>/dev/null || true)"
[[ "${upstream_remote}" == "${EXPECTED_UPSTREAM_REMOTE}" ]] || \
    fail "upstream remote must be ${EXPECTED_UPSTREAM_REMOTE}"

git -C "${REPO_ROOT}" cat-file -e "${EXPECTED_UPSTREAM_COMMIT}^{commit}" || \
    fail "pinned upstream commit is unavailable locally"

tag_commit="$(git -C "${REPO_ROOT}" rev-parse --verify "${EXPECTED_UPSTREAM_TAG}^{}" 2>/dev/null || true)"
[[ "${tag_commit}" == "${EXPECTED_UPSTREAM_COMMIT}" ]] || \
    fail "${EXPECTED_UPSTREAM_TAG} must resolve to ${EXPECTED_UPSTREAM_COMMIT}"

commit_tree="$(git -C "${REPO_ROOT}" rev-parse "${EXPECTED_UPSTREAM_COMMIT}^{tree}")"
[[ "${commit_tree}" == "${EXPECTED_UPSTREAM_TREE}" ]] || \
    fail "pinned upstream tree does not match ${EXPECTED_UPSTREAM_TREE}"

git -C "${REPO_ROOT}" merge-base --is-ancestor "${EXPECTED_UPSTREAM_COMMIT}" HEAD || \
    fail "current branch is not based on the pinned upstream commit"

verify_t1_overlay_allowlist

upstream_default_lock_blob="$(git -C "${REPO_ROOT}" rev-parse "${EXPECTED_UPSTREAM_COMMIT}:backend/requirements/default.txt")"
current_default_lock_blob="$(git -C "${REPO_ROOT}" hash-object backend/requirements/default.txt)"
[[ "${current_default_lock_blob}" == "${upstream_default_lock_blob}" ]] || \
    fail "CE dependency lock differs from the pinned upstream commit"

require_fixed_line "${PROVENANCE_FILE}" "- Upstream remote: \`${EXPECTED_UPSTREAM_REMOTE}\`"
require_fixed_line "${PROVENANCE_FILE}" "- Upstream release tag: \`${EXPECTED_UPSTREAM_TAG}\`"
require_fixed_line "${PROVENANCE_FILE}" "- Upstream commit: \`${EXPECTED_UPSTREAM_COMMIT}\`"
require_fixed_line "${PROVENANCE_FILE}" "- Upstream source tree: \`${EXPECTED_UPSTREAM_TREE}\`"
require_fixed_line "${REQUIREMENTS_FILE}" "-r default.txt"
require_fixed_line "${PROVENANCE_FILE}" "- Onyx receives no provider credentials. A later configuration slice must"
require_fixed_line "${PROVENANCE_FILE}" "  allow only a Skybase LLM proxy base URL and reject direct provider base URLs."
require_fixed_line "${PROVENANCE_FILE}" "  LiteLLM HTTP boundary. Private Railway networking alone does not prove that"
require_fixed_line "${PROVENANCE_FILE}" "- PostgreSQL extension availability, including \`pgcrypto\`, is a separate"

non_comment_requirements="$(grep -Ev '^[[:space:]]*(#.*)?$' "${REQUIREMENTS_FILE}" || true)"
[[ "${non_comment_requirements}" == '-r default.txt' ]] || \
    fail "CE requirements must contain only the pinned default lock"

for forbidden_pattern in \
    '(^|[^[:alnum:]_])ee([^[:alnum:]_]|$)' \
    'alembic_tenants' \
    'seed_dev_license' \
    'reencrypt_secrets' \
    'rotate_llm_provider_keys'; do
    if grep -nE -- "${forbidden_pattern}" "${IMAGE_FILE}" "${REQUIREMENTS_FILE}" "${WORKER_FILE}"; then
        fail "CE image contract contains a forbidden runtime input"
    fi
done

for forbidden_provider_input in \
    'ANTHROPIC_API_KEY' \
    'COHERE_API_KEY' \
    'GOOGLE_API_KEY' \
    'LITELLM_MASTER_KEY' \
    'OPENAI_API_KEY' \
    'VOYAGE_API_KEY' \
    'api.anthropic.com' \
    'api.cohere.ai' \
    'api.openai.com' \
    'api.voyageai.com' \
    'generativelanguage.googleapis.com'; do
    if grep -nF -- "${forbidden_provider_input}" "${IMAGE_FILE}" "${WORKER_FILE}"; then
        fail "CE image contract contains a direct provider credential or base URL"
    fi
done

if grep -nE -- '^[[:space:]]*(COPY|ADD)[[:space:]]+([^[:space:]]+[[:space:]]+)?\./?[[:space:]]' "${IMAGE_FILE}"; then
    fail "CE image contract must not use a broad build-context copy"
fi

if grep -nF -- 'BASE_IMAGE_REGISTRY' "${IMAGE_FILE}"; then
    fail "CE image contract must not permit a base-image registry override"
fi

expected_from_lines="$(printf 'FROM %s AS builder\nFROM %s AS runtime' "${PYTHON_BASE_IMAGE}" "${PYTHON_BASE_IMAGE}")"
actual_from_lines="$(grep -E '^[[:space:]]*FROM[[:space:]]' "${IMAGE_FILE}" || true)"
[[ "${actual_from_lines}" == "${expected_from_lines}" ]] || \
    fail "CE image contract must use only the approved digest-qualified Python base image"

uv_copy_count="$(grep -Fxc -- "${UV_COPY_LINE}" "${IMAGE_FILE}" || true)"
[[ "${uv_copy_count}" == "1" ]] || \
    fail "CE image contract must use the approved digest-qualified uv copy"

for expected_image_line in \
    "org.opencontainers.image.revision=\"${EXPECTED_UPSTREAM_COMMIT}\"" \
    "io.skybase.upstream.tag=\"${EXPECTED_UPSTREAM_TAG}\"" \
    "io.skybase.upstream.tree=\"${EXPECTED_UPSTREAM_TREE}\"" \
    'COPY ./requirements/default.txt /tmp/requirements/default.txt' \
    'COPY ./requirements/skybase-ce.txt /tmp/requirements/skybase-ce.txt' \
    '-r /tmp/requirements/skybase-ce.txt' \
    'COPY --chown=onyx:onyx ./alembic /app/alembic' \
    'COPY --chown=onyx:onyx ./onyx /app/onyx' \
    'COPY --chown=onyx:onyx ./shared_configs /app/shared_configs' \
    'COPY ./supervisord.skybase-ce.conf /etc/supervisor/conf.d/supervisord.conf'; do
    grep -Fq -- "${expected_image_line}" "${IMAGE_FILE}" || \
        fail "CE image contract is missing: ${expected_image_line}"
done

require_disabled_env 'ENABLE_PAID_ENTERPRISE_EDITION_FEATURES'
require_disabled_env 'LICENSE_ENFORCEMENT_ENABLED'

for expected_worker_line in \
    'celery -A onyx.background.celery.versioned_apps.docfetching worker' \
    'celery -A onyx.background.celery.versioned_apps.docprocessing worker'; do
    grep -Fq -- "${expected_worker_line}" "${WORKER_FILE}" || \
        fail "CE worker contract is missing: ${expected_worker_line}"
done

printf 'skybase provenance verification passed for %s (%s)\n' \
    "${EXPECTED_UPSTREAM_TAG}" "${EXPECTED_UPSTREAM_COMMIT}"
