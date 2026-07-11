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
- A private API image and a minimal worker process definition containing only
  document fetching and document processing queues.

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

## T1 Overlay Allowlist

The CE runtime contract may change only these five overlay paths:

- `SKYBASE_UPSTREAM.md`
- `scripts/verify-skybase-provenance.sh`
- `backend/Dockerfile.skybase-ce`
- `backend/requirements/skybase-ce.txt`
- `backend/supervisord.skybase-ce.conf`

The verifier checks the pinned-commit-to-HEAD diff, tracked working-tree
changes, and both ordinary and ignored untracked files against this allowlist.
It also rejects an `ee` path under every copied source root. A later task may
expand the allowlist only through a reviewed contract update.

## Provider And Database Boundaries

- Onyx receives no provider credentials. A later configuration slice must
  allow only a Skybase LLM proxy base URL and reject direct provider base URLs.
- A later request-transport slice must calculate an exact-body HMAC at the
  LiteLLM HTTP boundary. Private Railway networking alone does not prove that
  direct provider egress is blocked.
- PostgreSQL extension availability, including `pgcrypto`, is a separate
  Supabase preflight. This CE image contract does not assume or create global
  extensions.

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
6. Complete the disposable Supabase schema rehearsal and private runtime smoke
   gates before deploying a changed image.

Do not merge a newer upstream release by changing only an image tag or only
this document. The commit and tree are a coupled provenance pin.
