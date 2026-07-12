#!/usr/bin/env bash
set -Eeuo pipefail

readonly SKYBASE_CE_PRIVATE_ENV_KEYS=(
  "SKYBASE_ONYX_SHARED_SUPABASE"
  "SKYBASE_ONYX_ROLE_PROFILE"
  "SKYBASE_ONYX_DISPOSABLE_BRANCH"
  "MULTI_TENANT"
  "POSTGRES_USER"
  "POSTGRES_PASSWORD"
  "POSTGRES_HOST"
  "POSTGRES_PORT"
  "POSTGRES_DB"
  "POSTGRES_DEFAULT_SCHEMA"
  "POSTGRES_EXTENSION_SCHEMA"
  "POSTGRES_SSLMODE"
  "POSTGRES_SSLROOTCERT"
  "POSTGRES_USE_NULL_POOL"
  "POSTGRES_API_SERVER_POOL_SIZE"
  "POSTGRES_API_SERVER_POOL_OVERFLOW"
  "POSTGRES_API_SERVER_READ_ONLY_POOL_SIZE"
  "POSTGRES_API_SERVER_READ_ONLY_POOL_OVERFLOW"
  "DB_READONLY_USER"
  "DB_READONLY_PASSWORD"
  "CELERY_WORKER_DOCFETCHING_CONCURRENCY"
  "CELERY_WORKER_DOCPROCESSING_CONCURRENCY"
  "SKIP_WARM_UP"
  "FILE_STORE_BACKEND"
  "USE_IAM_AUTH"
)

skybase_ce_private_env_fail() {
  printf 'invalid private shared-Supabase environment file: %s\n' "$1" >&2
  return 65
}

skybase_ce_private_env_key_allowed() {
  local candidate="$1"
  local key
  for key in "${SKYBASE_CE_PRIVATE_ENV_KEYS[@]}"; do
    [[ "${candidate}" == "${key}" ]] && return 0
  done
  return 1
}

load_skybase_ce_private_env() {
  local env_file="$1"
  local line
  local key
  local value

  [[ -f "${env_file}" ]] || skybase_ce_private_env_fail "missing file"
  [[ "$(stat -f '%Lp' "${env_file}")" == "600" ]] || \
    skybase_ce_private_env_fail "file must have mode 0600"

  while IFS= read -r line || [[ -n "${line}" ]]; do
    [[ -n "${line}" ]] || continue
    if [[ ! "${line}" =~ ^([A-Z][A-Z0-9_]*)=(.*)$ ]]; then
      skybase_ce_private_env_fail "line is not a plain KEY=value assignment"
    fi
    key="${BASH_REMATCH[1]}"
    value="${BASH_REMATCH[2]}"
    skybase_ce_private_env_key_allowed "${key}" || \
      skybase_ce_private_env_fail "unexpected key ${key}"
    export "${key}=${value}"
  done < "${env_file}"
}

run_with_skybase_ce_private_env() {
  local key
  local -a environment=("PATH=${PATH}" "HOME=${HOME:-/tmp}" "LANG=${LANG:-C}")

  for key in "${SKYBASE_CE_PRIVATE_ENV_KEYS[@]}"; do
    if [[ -n "${!key+x}" ]]; then
      environment+=("${key}=${!key}")
    fi
  done
  env -i "${environment[@]}" "$@"
}
