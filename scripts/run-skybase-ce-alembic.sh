#!/usr/bin/env bash
set -Eeuo pipefail

readonly SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
readonly REPO_ROOT="$(git -C "${SCRIPT_DIR}/.." rev-parse --show-toplevel)"

usage() {
  printf 'usage: %s --env-file /absolute/path/to/migrator.env\n' "${0##*/}" >&2
  exit 64
}

[[ "$#" == "2" && "$1" == "--env-file" ]] || usage
readonly ENV_FILE="$2"
[[ -f "${ENV_FILE}" ]] || { printf 'missing env file\n' >&2; exit 65; }
[[ "$(stat -f '%Lp' "${ENV_FILE}")" == "600" ]] || {
  printf 'migrator env file must have mode 0600\n' >&2
  exit 65
}

# shellcheck disable=SC1090
set -a
source "${ENV_FILE}"
set +a
[[ "${SKYBASE_ONYX_SHARED_SUPABASE:-}" == "true" ]] || {
  printf 'shared-Supabase profile is required\n' >&2
  exit 65
}
[[ "${SKYBASE_ONYX_DISPOSABLE_BRANCH:-}" == "true" ]] || {
  printf 'only disposable branch migrations are supported\n' >&2
  exit 65
}
[[ "${SKYBASE_ONYX_ROLE_PROFILE:-}" == "migrator" ]] || {
  printf 'only the migrator role may run Alembic\n' >&2
  exit 65
}
[[ "${POSTGRES_USER:-}" == "skybase_onyx_migrator" ]] || {
  printf 'POSTGRES_USER must be skybase_onyx_migrator\n' >&2
  exit 65
}

cd "${REPO_ROOT}/backend"
exec alembic -x create_schema=false -x schemas=skybase_onyx upgrade head
