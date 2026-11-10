# AGENTS.md

Guidance for AI agents working in `aukilabs/splatter-server`.

## Repository scope

- This repo contains the Splatter Node, a compute node that trains 3D Gaussian Splats from DMT scan data and refined reconstruction outputs.
- Keep changes focused. Do not change product code, deployment config, workflows, generated files, secrets, or runtime infrastructure unless the task explicitly asks for that class of change.
- Pull requests should target `develop`. `CONTRIBUTING.md` says to use branch names such as `feature/*`, `fix/*`, `chore/*`, or similar; note that `.github/workflows/ci.yml` currently triggers on `main`, `feature/**`, `bug/**`, `chore/**`, and `hotfix/**` pushes, so do not promise CI coverage for every branch prefix.

## Source layout

- `server/rust/` is the Rust compute-node workspace.
  - `server/rust/bin/src/main.rs` starts the HTTP server, loads node config from environment, registers the Splatter runner capabilities, and runs the compute node through `node-host`.
  - `server/rust/runner/src/lib.rs` implements `/splatter/colmap/v1`, `/splatter/local/v1`, and `/splatter/global/v1`: materializes input CIDs, fetches DMT recordings and refined artifacts through `node-host` task IO, runs the Python pipeline, and uploads node capability outputs (see `docs/compute-capability.md`).
  - `server/rust/Makefile` wraps local Rust commands for run, check, fmt, clippy, test, and ci.
- Root-level Python scripts drive the training/export pipeline:
  - `splatter_pipeline.py` is the capability entrypoint (`colmap_v1_single_splat`, `local_only`, `global_only`).
  - `local_main.py` / `global_main.py` / `preprocessing.py` train and merge with LichtFeld Studio; helpers include `convert_splat.py`, `filter_splats.py`, `partition_splat.py`, `crop_aabb.py`, and `black_pixel_masks.py`.
- `Dockerfile` is multi-stage (Rust build → LichtFeld build → lean CUDA runtime). The image ships `splatter-bin`, the LichtFeld Studio binary, and the Python pipeline under `/app`, with task workspaces under `/app/tasks`.
- `charts/splatter-server/` contains the Helm chart and environment values consumed by deployment automation.
- `docs/` contains operator-facing deployment and minimum-requirements docs.

## DDS, DMS, and domain-service dependencies

- Node registration and task polling depend on the compute-node environment. Use `server/rust/.env.example` only as a list of variable names/default shapes; do not commit real values.
- Important environment names include `DDS_BASE_URL`, `DMS_BASE_URL`, `REG_SECRET`, `SECP256K1_PRIVHEX`, `REQUEST_TIMEOUT_SECS`, `REGISTER_INTERVAL_SECS`, `CLIENT_ID`, and `LOG_FORMAT`.
- The node runs on the Auki SDK task runtime via `server/rust/node-host`, which reads metadata, downloads data artifacts from CIDs and uploads outputs with the SDK data client. `node-host` keeps the wire behaviour (input layout, artifact names/upserts, DMS receipt shapes) of the former `posemesh-compute-node` 0.3.2 host; `node-host/tests` pins it. Changes to CID parsing, metadata lookups, download behavior, token/client handling, or output upload naming can affect DDS, DMS, DMT, reconstruction-server, and domain-service compatibility.
- The input path expects a refined manifest plus DMT recording data and COLMAP outputs. The output upload path uses `refined_splat...` names with data type `splat_data`.

## Kubernetes and deployment cautions

- Splatter workloads are GPU-heavy. Preserve GPU scheduling and resource settings unless a task explicitly scopes deployment changes.
- In the chart, keep the `nvidia.com/gpu` limit, CPU/memory/ephemeral-storage requests and limits, `/dev/shm` `emptyDir`, `/app/tasks` `emptyDir`, and the `dedicated=karpenterGPU` toleration intact unless you have explicit approval to change scheduling or runtime capacity.
- The chart template is a `Deployment` with `Recreate` strategy, not a StatefulSet. Verify the live workload kind before making operational assumptions; neighboring reconstruction workloads may use different workload kinds.
- Do not deploy, restart, scale, ArgoCD sync, mutate Kubernetes resources, or read/print Kubernetes Secret contents from documentation or code-review tasks.
- `charts/splatter-server/values.yaml` uses `latest` and `Always`; staging/prod values pin `v0.1.0` with `IfNotPresent`. Do not infer promotion or deployment behavior without checking the active environment and source-of-truth config.

## Secrets and safety

- Never commit or print real `.env` values, app JWTs, DDS/DMS credentials, `REG_SECRET`, `SECP256K1_PRIVHEX`, SOPS files, Kubernetes Secret data, private keys, or token-bearing URLs.
- Redact any accidental secret-looking values in logs and PR text.
- Do not decrypt secret files or copy secret material into examples. Prefer placeholders from `server/rust/.env.example`.

## Verification guidance

- For Rust code changes, start in `server/rust/` and run the relevant checks from `CONTRIBUTING.md` or the Makefile: `cargo fmt --all`, clippy, and tests.
- For docs-only changes such as this file, do not run GPU, Docker, Kubernetes, ArgoCD, or long training jobs. Verify with `git diff --check`, confirm the changed-file list is scoped, and review the Markdown for secret leakage and copied line-number prefixes.
