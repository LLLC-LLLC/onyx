#!/usr/bin/env bash
set -Eeuo pipefail

readonly SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
readonly REPO_ROOT="$(git -C "${SCRIPT_DIR}/.." rev-parse --show-toplevel)"
readonly EXPECTED_GRANTS_SHA256="4cfe23d4b8de53b0afce8bd5868a61c401fbd1d6320b5299adb18448f05dd5dc"

# shellcheck source=scripts/_lib/skybase-ce-private-env.sh
source "${SCRIPT_DIR}/_lib/skybase-ce-private-env.sh"

usage() {
  printf 'usage: %s --env-file /absolute/path/to/migrator.env --grants-file /absolute/path/to/post-migrate-grants.sql\n' "${0##*/}" >&2
  exit 64
}

[[ "$#" == "4" && "$1" == "--env-file" && "$3" == "--grants-file" ]] || usage
readonly ENV_FILE="$2"
readonly GRANTS_FILE="$4"
[[ "${ENV_FILE}" = /* && "${GRANTS_FILE}" = /* ]] || {
  printf 'env and grants files must use absolute paths\n' >&2
  exit 65
}
[[ "${ENV_FILE##*/}" == "migrator.env" ]] || {
  printf 'only the rendered migrator.env is accepted\n' >&2
  exit 65
}
[[ "${GRANTS_FILE##*/}" == "post-migrate-grants.sql" ]] || {
  printf 'only the rendered post-migrate-grants.sql is accepted\n' >&2
  exit 65
}
[[ -f "${GRANTS_FILE}" && "$(stat -f '%Lp' "${GRANTS_FILE}")" == "600" ]] || {
  printf 'post-migration grants file must have mode 0600\n' >&2
  exit 65
}
[[ "$(cd -- "$(dirname -- "${ENV_FILE}")" && pwd -P)" == "$(cd -- "$(dirname -- "${GRANTS_FILE}")" && pwd -P)" ]] || {
  printf 'env and grants files must be rendered in the same private directory\n' >&2
  exit 65
}
actual_grants_sha256="$(shasum -a 256 "${GRANTS_FILE}" | awk '{print $1}')"
[[ "${actual_grants_sha256}" == "${EXPECTED_GRANTS_SHA256}" ]] || {
  printf 'post-migration grants file does not match the reviewed renderer output\n' >&2
  exit 65
}

load_skybase_ce_private_env "${ENV_FILE}"
[[ "${SKYBASE_ONYX_SHARED_SUPABASE:-}" == "true" ]] || {
  printf 'shared-Supabase profile is required\n' >&2
  exit 65
}
[[ "${SKYBASE_ONYX_DISPOSABLE_BRANCH:-}" == "true" ]] || {
  printf 'only disposable branch grants are supported\n' >&2
  exit 65
}
[[ "${SKYBASE_ONYX_ROLE_PROFILE:-}" == "migrator" ]] || {
  printf 'only the migrator role may apply post-migration grants\n' >&2
  exit 65
}
[[ "${POSTGRES_USER:-}" == "skybase_onyx_migrator" ]] || {
  printf 'POSTGRES_USER must be skybase_onyx_migrator\n' >&2
  exit 65
}

expected_head="$(cd "${REPO_ROOT}/backend" && run_with_skybase_ce_private_env alembic heads | awk '/\(head\)/ {print $1}')"
[[ "${expected_head}" =~ ^[0-9a-z]+$ ]] || {
  printf 'unable to determine exactly one Alembic head\n' >&2
  exit 65
}

export PGDATABASE="${POSTGRES_DB}"
export PGHOST="${POSTGRES_HOST}"
export PGPASSWORD="${POSTGRES_PASSWORD}"
export PGPORT="${POSTGRES_PORT}"
export PGSSLMODE="${POSTGRES_SSLMODE}"
export PGSSLROOTCERT="${POSTGRES_SSLROOTCERT}"
export PGUSER="${POSTGRES_USER}"
actual_head="$(psql --no-psqlrc --tuples-only --no-align --quiet --set=ON_ERROR_STOP=1 \
  --command='SELECT version_num FROM skybase_onyx.alembic_version')"
[[ "${actual_head}" == "${expected_head}" ]] || {
  printf 'post-migration grants require a successful upgrade to Alembic head\n' >&2
  exit 65
}

psql --no-psqlrc --quiet --set=ON_ERROR_STOP=1 --file="${GRANTS_FILE}" >/dev/null
printf 'Applied reviewed shared-Supabase post-migration grants.\n'
