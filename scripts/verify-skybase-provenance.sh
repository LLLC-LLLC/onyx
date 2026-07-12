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
readonly T2_ALLOWED_OVERLAY_PATHS=(
    "SKYBASE_UPSTREAM.md"
    "scripts/verify-skybase-provenance.sh"
    "scripts/render-skybase-supabase-env.py"
    "scripts/run-skybase-ce-alembic.sh"
    "scripts/apply-skybase-ce-post-migration-grants.sh"
    "scripts/_lib/skybase-ce-private-env.sh"
    "backend/Dockerfile.skybase-ce"
    "backend/requirements/skybase-ce.txt"
    "backend/supervisord.skybase-ce.conf"
    "backend/alembic/env.py"
    "backend/alembic/versions/c9e2cd766c29_add_s3_file_store_table.py"
    "backend/alembic/versions/6756efa39ada_id_uuid_for_chat_session.py"
    "backend/alembic/versions/9b66d3156fc6_user_file_schema_additions.py"
    "backend/alembic/versions/495cb26ce93e_create_knowlege_graph_tables.py"
    "backend/alembic/versions/36e9220ab794_update_kg_trigger_functions.py"
    "backend/alembic/versions/c7bc8cc2921d_drop_unused_kg_indexes.py"
    "backend/alembic/versions/0cd424f32b1d_user_file_data_preparation_and_backfill.py"
    "backend/alembic/versions/7cc3fcc116c1_user_file_uuid_primary_key_swap.py"
    "backend/onyx/background/celery/apps/beat.py"
    "backend/onyx/background/celery/apps/client.py"
    "backend/onyx/background/celery/apps/docfetching.py"
    "backend/onyx/background/celery/apps/docprocessing.py"
    "backend/onyx/background/celery/apps/heavy.py"
    "backend/onyx/background/celery/apps/light.py"
    "backend/onyx/background/celery/apps/monitoring.py"
    "backend/onyx/background/celery/apps/primary.py"
    "backend/onyx/background/celery/apps/scheduled_tasks.py"
    "backend/onyx/background/celery/apps/user_file_processing.py"
    "backend/onyx/background/indexing/job_client.py"
    "backend/onyx/configs/app_configs.py"
    "backend/onyx/configs/constants.py"
    "backend/onyx/db/engine/async_sql_engine.py"
    "backend/onyx/db/engine/connection_warmup.py"
    "backend/onyx/db/engine/sql_engine.py"
    "backend/onyx/db/skybase_shared_supabase.py"
    "backend/onyx/file_store/file_store.py"
    "backend/onyx/kg/clustering/clustering.py"
    "backend/onyx/kg/clustering/normalizations.py"
    "backend/onyx/main.py"
    "backend/onyx/shared_supabase_health.py"
    "backend/onyx/setup.py"
    "backend/tests/unit/onyx/db/engine/test_skybase_db_contract.py"
    "backend/tests/unit/onyx/test_shared_supabase_health.py"
    "backend/tests/unit/alembic/test_skybase_shared_supabase_alembic_contract.py"
    "backend/tests/unit/scripts/test_skybase_supabase_scripts.py"
    "backend/tests/unit/scripts/test_skybase_post_migration_grants.py"
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

require_t2_overlay_path() {
    local changed_path="$1"
    local allowed_path

    for allowed_path in "${T2_ALLOWED_OVERLAY_PATHS[@]}"; do
        [[ "${changed_path}" == "${allowed_path}" ]] && return 0
    done

    fail "T2 overlay path is not allowlisted: ${changed_path}"
}

verify_t2_overlay_allowlist() {
    local changed_path
    local copied_root
    local ee_descendant

    for copied_root in "${COPIED_SOURCE_ROOTS[@]}"; do
        ee_descendant="$(find "${REPO_ROOT}/${copied_root}" -path '*/ee' -print -quit)"
        [[ -z "${ee_descendant}" ]] || \
            fail "CE image copied source root contains an ee descendant: ${ee_descendant}"
    done

    while IFS= read -r changed_path; do
        [[ -n "${changed_path}" ]] && require_t2_overlay_path "${changed_path}"
    done < <(git -C "${REPO_ROOT}" diff --name-only "${EXPECTED_UPSTREAM_COMMIT}...HEAD")

    while IFS= read -r changed_path; do
        [[ -n "${changed_path}" ]] && require_t2_overlay_path "${changed_path}"
    done < <(git -C "${REPO_ROOT}" diff --name-only "${EXPECTED_UPSTREAM_COMMIT}")

    while IFS= read -r -d '' changed_path; do
        require_t2_overlay_path "${changed_path}"
    done < <(git -C "${REPO_ROOT}" ls-files --others --exclude-standard -z)

    while IFS= read -r -d '' changed_path; do
        require_t2_overlay_path "${changed_path}"
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

verify_t2_overlay_allowlist

upstream_default_lock_blob="$(git -C "${REPO_ROOT}" rev-parse "${EXPECTED_UPSTREAM_COMMIT}:backend/requirements/default.txt")"
current_default_lock_blob="$(git -C "${REPO_ROOT}" hash-object backend/requirements/default.txt)"
[[ "${current_default_lock_blob}" == "${upstream_default_lock_blob}" ]] || \
    fail "CE dependency lock differs from the pinned upstream commit"

require_fixed_line "${PROVENANCE_FILE}" "- Upstream remote: \`${EXPECTED_UPSTREAM_REMOTE}\`"
require_fixed_line "${PROVENANCE_FILE}" "- Upstream release tag: \`${EXPECTED_UPSTREAM_TAG}\`"
require_fixed_line "${PROVENANCE_FILE}" "- Upstream commit: \`${EXPECTED_UPSTREAM_COMMIT}\`"
require_fixed_line "${PROVENANCE_FILE}" "- Upstream source tree: \`${EXPECTED_UPSTREAM_TREE}\`"
require_fixed_line "${REQUIREMENTS_FILE}" "-r default.txt"
require_fixed_line "${PROVENANCE_FILE}" "- Onyx receives no provider, connector, identity, storage, or telemetry"
require_fixed_line "${PROVENANCE_FILE}" "  secrets. The profile accepts only the two generated database passwords and"
require_fixed_line "${PROVENANCE_FILE}" "  LiteLLM HTTP boundary. Private Railway networking alone does not prove that"
require_fixed_line "${PROVENANCE_FILE}" "- PostgreSQL extension availability is a branch-bootstrap preflight:"

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

for expected_image_line in \
    'FILE_STORE_BACKEND="disabled"' \
    'SKYBASE_ONYX_SHARED_SUPABASE="true"' \
    'SKIP_WARM_UP="true"'; do
    grep -Fq -- "${expected_image_line}" "${IMAGE_FILE}" || \
        fail "CE image contract is missing shared-profile setting: ${expected_image_line}"
done

for expected_worker_line in \
    'exec uvicorn onyx.shared_supabase_health:app --host=0.0.0.0 --port="${PORT:-8080}"' \
    'celery -A onyx.background.celery.versioned_apps.docfetching worker --hostname=skybase-docfetching@%%h --concurrency=1 --pool=threads -Q connector_doc_fetching' \
    'celery -A onyx.background.celery.versioned_apps.docprocessing worker --hostname=skybase-docprocessing@%%h --concurrency=1 --pool=threads -Q docprocessing'; do
    grep -Fq -- "${expected_worker_line}" "${WORKER_FILE}" || \
        fail "CE worker contract is missing: ${expected_worker_line}"
done

[[ "$(grep -c '^\[program:' "${WORKER_FILE}")" == "3" ]] || \
    fail "shared supervisor may define only the health API and approved workers"

readonly SHARED_MIGRATIONS=(
    "backend/alembic/versions/c9e2cd766c29_add_s3_file_store_table.py"
    "backend/alembic/versions/6756efa39ada_id_uuid_for_chat_session.py"
    "backend/alembic/versions/9b66d3156fc6_user_file_schema_additions.py"
    "backend/alembic/versions/495cb26ce93e_create_knowlege_graph_tables.py"
    "backend/alembic/versions/36e9220ab794_update_kg_trigger_functions.py"
    "backend/alembic/versions/c7bc8cc2921d_drop_unused_kg_indexes.py"
    "backend/alembic/versions/0cd424f32b1d_user_file_data_preparation_and_backfill.py"
    "backend/alembic/versions/7cc3fcc116c1_user_file_uuid_primary_key_swap.py"
)
shared_migration_paths=()

for migration in "${SHARED_MIGRATIONS[@]}"; do
    require_file "${REPO_ROOT}/${migration}"
    shared_migration_paths+=("${REPO_ROOT}/${migration}")
done

if grep -nE -- 'CREATE[[:space:]]+EXTENSION|DROP[[:space:]]+EXTENSION|CREATE[[:space:]]+(USER|ROLE)|DROP[[:space:]]+(USER|ROLE)|GRANT[[:space:]]+CONNECT|REVOKE[[:space:]]+ALL[[:space:]]+ON[[:space:]]+DATABASE' \
    "${shared_migration_paths[@]}"; then
    fail "shared migrations must not mutate global extensions, roles, or database grants"
fi

if grep -nE -- 'public\.gin_trgm_ops|POSTGRES_DEFAULT_SCHEMA[)}.]*(show_trgm|similarity_op)' \
    "${REPO_ROOT}/backend/alembic/versions/495cb26ce93e_create_knowlege_graph_tables.py" \
    "${REPO_ROOT}/backend/alembic/versions/36e9220ab794_update_kg_trigger_functions.py" \
    "${REPO_ROOT}/backend/alembic/versions/c7bc8cc2921d_drop_unused_kg_indexes.py" \
    "${REPO_ROOT}/backend/onyx/kg/clustering/normalizations.py" \
    "${REPO_ROOT}/backend/onyx/kg/clustering/clustering.py"; then
    fail "trigram functions/operators must resolve through POSTGRES_EXTENSION_SCHEMA"
fi

for uuid_migration in \
    "backend/alembic/versions/6756efa39ada_id_uuid_for_chat_session.py" \
    "backend/alembic/versions/9b66d3156fc6_user_file_schema_additions.py" \
    "backend/alembic/versions/0cd424f32b1d_user_file_data_preparation_and_backfill.py" \
    "backend/alembic/versions/7cc3fcc116c1_user_file_uuid_primary_key_swap.py"; do
    grep -Fq -- 'public.gen_random_uuid()' "${REPO_ROOT}/${uuid_migration}" || \
        fail "UUID migration lacks explicit public.gen_random_uuid() qualification: ${uuid_migration}"
done

readonly C9E2_FILE="${REPO_ROOT}/backend/alembic/versions/c9e2cd766c29_add_s3_file_store_table.py"
if grep -nE -- '^from onyx\.file_store\.file_store import get_s3_file_store' "${C9E2_FILE}"; then
    fail "c9e2 must not import a direct S3 client before its shared-profile gate"
fi
grep -Fq -- 'if is_shared_supabase_profile():' "${C9E2_FILE}" || \
    fail "c9e2 lacks the no-S3 shared-profile gate"
grep -Fq -- 'Shared profile: skipped direct object-storage migration.' "${C9E2_FILE}" || \
    fail "c9e2 lacks the fail-closed no-S3 migration path"

readonly CONTRACT_FILE="${REPO_ROOT}/backend/onyx/db/skybase_shared_supabase.py"
readonly SYNC_ENGINE_FILE="${REPO_ROOT}/backend/onyx/db/engine/sql_engine.py"
readonly ASYNC_ENGINE_FILE="${REPO_ROOT}/backend/onyx/db/engine/async_sql_engine.py"
readonly WARMUP_FILE="${REPO_ROOT}/backend/onyx/db/engine/connection_warmup.py"
readonly ALEMBIC_ENV_FILE="${REPO_ROOT}/backend/alembic/env.py"
readonly RENDERER_FILE="${REPO_ROOT}/scripts/render-skybase-supabase-env.py"
readonly MIGRATION_LAUNCHER_FILE="${REPO_ROOT}/scripts/run-skybase-ce-alembic.sh"
readonly GRANTS_LAUNCHER_FILE="${REPO_ROOT}/scripts/apply-skybase-ce-post-migration-grants.sh"
readonly PRIVATE_ENV_LIB_FILE="${REPO_ROOT}/scripts/_lib/skybase-ce-private-env.sh"
readonly HEALTH_APP_FILE="${REPO_ROOT}/backend/onyx/shared_supabase_health.py"
for contract_line in \
    'SEARCH_PATH: Final = f"{SHARED_SCHEMA},{EXTENSION_SCHEMA}"' \
    'MAX_RUNTIME_CONNECTIONS: Final = 12' \
    'ALLOWED_WORKER_APPS: Final = frozenset({"docfetching", "docprocessing"})' \
    'FILE_STORE_BACKEND' \
    'ROLE_CONNECTION_LIMITS: Final' \
    'rolbypassrls' \
    'pg_catalog.pg_auth_members' \
    'has_table_privilege' \
    'PROFILE_NON_SECRET_ENV_ALLOWLIST: Final = frozenset({"HF_HUB_DISABLE_TELEMETRY"})' \
    'SHARED_PROFILE_ALLOWED_PATHS: Final = frozenset({"/health"})' \
    'The shared-Supabase profile accepts only its reviewed database'; do
    grep -Fq -- "${contract_line}" "${CONTRACT_FILE}" || \
        fail "shared contract is missing: ${contract_line}"
done
for engine_file in "${SYNC_ENGINE_FILE}" "${ASYNC_ENGINE_FILE}"; do
    grep -Fq -- 'shared_search_path' "${engine_file}" || \
        fail "engine does not enforce the shared search path: ${engine_file}"
done
grep -Fq -- 'Connection warmup is disabled in the shared-Supabase profile.' "${WARMUP_FILE}" || \
    fail "shared profile must disable legacy connection warmup"
[[ "$(grep -Fc -- 'assert_shared_migration_preconditions(connection)' "${ALEMBIC_ENV_FILE}")" == "2" ]] || \
    fail "shared-profile Alembic paths must assert the reviewed catalog twice"
for renderer_line in \
    'NOBYPASSRLS CONNECTION LIMIT 1 PASSWORD' \
    'NOBYPASSRLS CONNECTION LIMIT 12 PASSWORD' \
    'ALTER SCHEMA skybase_onyx OWNER TO skybase_onyx_migrator;' \
    'KG_READONLY_TABLE_ALLOWLIST: Final[tuple[str, ...]] = ()' \
    'REVOKE ALL PRIVILEGES ON ALL TABLES IN SCHEMA skybase_onyx FROM skybase_onyx_kg_ro;'; do
    grep -Fq -- "${renderer_line}" "${RENDERER_FILE}" || \
        fail "shared renderer is missing: ${renderer_line}"
done
if grep -Fq -- 'GRANT USAGE ON SCHEMA public' "${RENDERER_FILE}"; then
    fail "shared renderer must not grant public schema usage"
fi
grep -Fq -- 'EXPECTED_GRANTS_SHA256=' "${GRANTS_LAUNCHER_FILE}" || \
    fail "post-migration grants launcher must pin reviewed SQL"
grep -Fq -- 'SELECT version_num FROM skybase_onyx.alembic_version' "${GRANTS_LAUNCHER_FILE}" || \
    fail "post-migration grants launcher must verify Alembic head"
grep -Fq -- 'run_reviewed_psql()' "${GRANTS_LAUNCHER_FILE}" || \
    fail "post-migration grants launcher must use the reviewed psql boundary"
grep -Fq -- 'run_with_skybase_ce_private_env env' "${GRANTS_LAUNCHER_FILE}" || \
    fail "post-migration psql must run in a scrubbed environment"
if grep -nE -- '^export PG' "${GRANTS_LAUNCHER_FILE}"; then
    fail "post-migration grants launcher must not export inherited PG settings"
fi
for launcher_file in "${MIGRATION_LAUNCHER_FILE}" "${GRANTS_LAUNCHER_FILE}"; do
    grep -Fq -- 'load_skybase_ce_private_env' "${launcher_file}" || \
        fail "launcher must parse private env without sourcing it: ${launcher_file}"
    grep -Fq -- 'run_with_skybase_ce_private_env' "${launcher_file}" || \
        fail "launcher must scrub inherited environment values: ${launcher_file}"
done

require_file "${HEALTH_APP_FILE}"
for health_line in \
    'is_shared_supabase_profile()' \
    'validate_shared_supabase_contract()' \
    '"type": "websocket.close", "code": 1008' \
    '_ALLOWED_HEALTH_PATHS = frozenset({"/health", "/health/"})' \
    'scope["path"] not in _ALLOWED_HEALTH_PATHS'; do
    grep -Fq -- "${health_line}" "${HEALTH_APP_FILE}" || \
        fail "shared health entrypoint is missing: ${health_line}"
done
grep -Fq -- 'onyx.shared_supabase_health:app, not onyx.main:app.' \
    "${REPO_ROOT}/backend/onyx/main.py" || \
    fail "native main must reject the shared-Supabase profile before router imports"

for denied_worker in primary light heavy user_file_processing scheduled_tasks monitoring beat client; do
    grep -Fq -- "assert_worker_app_allowed(\"${denied_worker}\")" \
        "${REPO_ROOT}/backend/onyx/background/celery/apps/${denied_worker}.py" || \
        fail "denied worker is missing its import-time shared-profile guard: ${denied_worker}"
done
for allowed_worker in docfetching docprocessing; do
    grep -Fq -- "assert_worker_app_allowed(\"${allowed_worker}\")" \
        "${REPO_ROOT}/backend/onyx/background/celery/apps/${allowed_worker}.py" || \
        fail "allowed worker is missing its shared-profile guard: ${allowed_worker}"
done

grep -Fq -- '# file-under-test: backend/onyx/db/skybase_shared_supabase.py' \
    "${REPO_ROOT}/backend/tests/unit/onyx/db/engine/test_skybase_db_contract.py" || \
    fail "contract test must name its file under test"
grep -Fq -- '# file-under-test: scripts/render-skybase-supabase-env.py' \
    "${REPO_ROOT}/backend/tests/unit/scripts/test_skybase_supabase_scripts.py" || \
    fail "renderer test must name its file under test"
grep -Fq -- '# file-under-test: backend/alembic/env.py' \
    "${REPO_ROOT}/backend/tests/unit/alembic/test_skybase_shared_supabase_alembic_contract.py" || \
    fail "Alembic contract test must name its file under test"
grep -Fq -- '# file-under-test: backend/onyx/shared_supabase_health.py' \
    "${REPO_ROOT}/backend/tests/unit/onyx/test_shared_supabase_health.py" || \
    fail "health entrypoint test must name its file under test"
grep -Fq -- '# file-under-test: scripts/apply-skybase-ce-post-migration-grants.sh' \
    "${REPO_ROOT}/backend/tests/unit/scripts/test_skybase_post_migration_grants.py" || \
    fail "post-migration grants test must name its file under test"

renderer_pyc="$(mktemp "${TMPDIR:-/tmp}/skybase-renderer.XXXXXX.pyc")"
trap 'rm -f "${renderer_pyc}"' EXIT
python3 -c 'import py_compile, sys; py_compile.compile(sys.argv[1], cfile=sys.argv[2], doraise=True)' \
    "${REPO_ROOT}/scripts/render-skybase-supabase-env.py" "${renderer_pyc}" || \
    fail "shared-Supabase renderer does not compile"
for shell_file in "${MIGRATION_LAUNCHER_FILE}" "${GRANTS_LAUNCHER_FILE}" "${PRIVATE_ENV_LIB_FILE}"; do
    bash -n "${shell_file}" || fail "shared-Supabase shell tooling has invalid syntax: ${shell_file}"
done

printf 'skybase provenance verification passed for %s (%s)\n' \
    "${EXPECTED_UPSTREAM_TAG}" "${EXPECTED_UPSTREAM_COMMIT}"
