# Skybase Onyx CE Upstream Contract

## Pinned Upstream

- Upstream remote: `https://github.com/onyx-dot-app/onyx.git`
- Upstream release tag: `v4.3.1`
- Upstream commit: `c8ba005da719ad880afbff08b067241eb3f9adc1`
- Upstream source tree: `25e636429b1161a161aabfa40e9b29cf4b70b778`
- Fork remote: `https://github.com/LLLC-LLLC/onyx.git`
- Skybase deployment baseline: `skybase/v4.3.1`

Run `bash scripts/verify-skybase-provenance.sh` from any directory before
building or changing the Skybase CE image. The verifier confirms the pinned
upstream object and the CE image boundary; it is not a substitute for the
later Supabase schema rehearsal or runtime smoke tests.

## License Boundary

Skybase uses only the upstream source outside separately licensed `ee`
directories. The root [LICENSE](LICENSE) calls this source the MIT Expat
licensed portion of Onyx. No source, generated artifact, dependency lock, or
runtime process from an `ee` directory is part of this fork contract. Third
party dependencies retain their own licenses and must be reviewed separately
when the dependency lock changes.

## Included CE Runtime Surface

- `backend/onyx`, `backend/shared_configs`, and the CE Alembic history.
- The pinned CE backend lock at `backend/requirements/default.txt`, included by
  `backend/requirements/skybase-ce.txt`.
- A private health-only API process at `onyx.shared_supabase_health:app` and a
  minimal worker process definition containing only document fetching and
  document processing queues. It honors Railway's `PORT` with a local `8080`
  fallback. The profile must not start `onyx.main`.

## Explicit Exclusions

- All upstream `ee` directories, enterprise requirement locks, enterprise
  migrations, license enforcement code, and paid feature behavior.
- The stock `backend/Dockerfile` and `backend/supervisord.conf`, which include
  enterprise code and unrelated processes.
- The Onyx web UI, nginx, bundled PostgreSQL, MinIO, MCP server, code
  interpreter, Slack and Discord bots, scheduler, watchdog, monitoring, and
  unrelated background workers.
- Source connector credentials, user identity, authorization, audit records,
  public API exposure, and action execution. Skybase owns those controls.

## T2 Overlay Allowlist

The shared-Supabase contract expands the reviewed overlay to the following CE
paths only. No Enterprise source, `alembic_tenants`, or copied EE behavior is
permitted.

- T1 image/provenance paths: `SKYBASE_UPSTREAM.md`,
  `scripts/verify-skybase-provenance.sh`, `backend/Dockerfile.skybase-ce`,
  `backend/requirements/skybase-ce.txt`, and
  `backend/supervisord.skybase-ce.conf`.
- Host-only launch tools: `scripts/render-skybase-supabase-env.py`,
  `scripts/run-skybase-ce-alembic.sh`, and
  `scripts/apply-skybase-ce-post-migration-grants.sh`.
- The health-only shared-profile entrypoint:
  `backend/onyx/shared_supabase_health.py`.
- Shared contract and engine paths: `backend/onyx/db/skybase_shared_supabase.py`,
  `backend/onyx/db/engine/sql_engine.py`,
  `backend/onyx/db/engine/async_sql_engine.py`,
  `backend/onyx/db/engine/connection_warmup.py`,
  `backend/onyx/configs/app_configs.py`, and
  `backend/onyx/configs/constants.py`.
- The eight audited CE migrations, `backend/alembic/env.py`, the two KG
  trigram call sites, the bounded Celery app/configuration paths, and the
  v1 FileStore/native-surface gates.
- Focused contract tests under `backend/tests/unit/onyx/db/engine/` and
  `backend/tests/unit/scripts/`.

The verifier checks the pinned-commit-to-HEAD diff, tracked working-tree
changes, and both ordinary and ignored untracked files against this exact
allowlist. It also rejects an `ee` path under every copied source root. A
later task may expand the allowlist only through a reviewed contract update.

## Shared-Supabase V1 Boundaries

- The profile is opt-in through `SKYBASE_ONYX_SHARED_SUPABASE=true`; it fails
  before engine initialization unless it uses `skybase_onyx`, the Supabase
  `extensions` schema, verified TLS, named roles, and the fixed `<=12`
  connection budget. The migration preflight also requires every controlled
  role to be login-only, `NOINHERIT`, non-superuser, `NOBYPASSRLS`,
  membership-free, connection-limited, and unable to create in `public` or
  `extensions`; `skybase_onyx` must be owned by the migrator role.
- Every sync, readonly, async, worker-child, and Alembic connection uses and
  asserts `search_path=skybase_onyx,extensions`. `public` is absent from the
  path; the only approved `public` access is explicit
  `public.gen_random_uuid()` for the pre-managed `pgcrypto` extension.
- Skybase creates roles/extensions/schema ownership on a disposable branch
  outside Onyx. CE migrations assert those prerequisites and do not create,
  move, drop, or grant global database resources. Shared branches are
  discarded rather than downgraded. The host renderer emits schema-local
  post-migration grants only after a successful upgrade.
- The T2 FileStore is health-only. Direct S3/GCS configuration and all native
  object operations are denied until the separate Skybase storage-broker task
  provides an independently reviewed adapter.
- Only the docfetching and docprocessing Celery apps are launchable; their
  concurrency and database overflow are fixed to one and zero. Other native
  workers fail before `SqlEngine.init_engine()`.
- Native credential, connector, identity, upload, chat, tenant, skill, tool,
  and MCP surfaces are disabled. The shared-profile ASGI boundary is
  default-deny: `onyx.shared_supabase_health:app` serves only `/health` in v1
  and closes every WebSocket without accepting it. Skybase remains the owner
  of provider credentials, connector configuration, identity, authorization,
  audit, public API, and action execution.

## Provider And Database Boundaries

- Onyx receives no provider, connector, identity, storage, or telemetry
  secrets. The profile accepts only the two generated database passwords and
  rejects secret-shaped variables and native service namespaces, including
  OpenRouter. HF_HUB_DISABLE_TELEMETRY is the sole non-secret library flag
  allowed because upstream sets it to disable telemetry while loading CE
  migrations. A later configuration slice must introduce a reviewed Skybase
  proxy contract instead of relaxing this boundary.
- A later request-transport slice must calculate an exact-body HMAC at the
  LiteLLM HTTP boundary. Private Railway networking alone does not prove that
  direct provider egress is blocked.
- PostgreSQL extension availability is a branch-bootstrap preflight:
  `pg_trgm` must already be in `extensions` and `pgcrypto` in `public`. This
  CE image never creates global extensions.
- `skybase_onyx_kg_ro` has schema usage but no table or sequence grants in v1.
  CE has no readonly retrieval caller, and raw document/KG tables carry
  permission-bearing data. A future retrieval adapter must expose a reviewed,
  permission-filtered view or service before adding a non-empty allowlist.
- The host launchers parse only the renderer's known `KEY=value` fields and
  start Alembic and post-migration `psql` under scrubbed environments.
  Unrelated desktop or CI secrets are never inherited by CE migration tools.

## Update Procedure

1. Fetch the official upstream remote and select a reviewed release tag.
2. Record its exact tag, commit, and tree in this file and in
   `scripts/verify-skybase-provenance.sh` together.
3. Review the upstream license boundary, CE dependency lock, Docker build
   inputs, and any new dynamic imports before changing the runtime contract.
4. Run `bash scripts/verify-skybase-provenance.sh` from the repository root and
   from a subdirectory. Deliberately add a forbidden image input in a temporary
   copy to prove the verifier fails, then restore it.
5. Create a new reviewed `skybase/<release-tag>` deployment baseline rather
   than rebasing the overlay onto upstream `main` implicitly.
6. Render 0600 role files on the operator host, bootstrap only a disposable
   branch with the rendered `bootstrap.sql`, then run
   `scripts/run-skybase-ce-alembic.sh --env-file /absolute/path/migrator.env`.
7. Only after the upgrade reaches Alembic head, run
   `scripts/apply-skybase-ce-post-migration-grants.sh --env-file
   /absolute/path/migrator.env --grants-file
   /absolute/path/post-migrate-grants.sql`. The launcher accepts no arbitrary
   SQL and verifies the generated grants checksum and migration head.
8. Complete the schema rehearsal and private runtime smoke gates before
   deploying a changed image.

Do not merge a newer upstream release by changing only an image tag or only
this document. The commit and tree are a coupled provenance pin.
