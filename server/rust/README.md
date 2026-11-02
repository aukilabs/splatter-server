# Splatter node workspace

The node runs on the [Auki SDK](https://github.com/aukilabs/auki-sdk) task
runtime (`auki-sdk`, pinned by git revision in `Cargo.toml`). The SDK owns
machine registration, authentication, DMS leases and heartbeats.

## Quick start
1) Copy `.env.example` to `.env` and fill in real DDS/DMS values (base URLs,
   registration secret, secp256k1 private key, etc).
2) From this directory run `make run` (or `cargo run -p splatter-bin`). The
   binary loads `.env` automatically and starts the HTTP server on
   `0.0.0.0:8080` (`GET /health`).
3) Use `scripts/curl-create-hello-job.sh` to enqueue a demo job
   once the node has registered with DDS/DMS.

Packages:

- `splatter-runner` (`runner/src/lib.rs`): the `/splatter/colmap/v1` runner
  (capability logic).
- `node-host` (`node-host/src`): the host on the SDK runtime — claim loop and
  shutdown, task Domain IO (input download layout, artifact upserts) and the
  DMS completion/failure receipts. It keeps the wire behaviour of the former
  `posemesh-compute-node` 0.3.2 host; `node-host/tests` pins that contract.
- `splatter-bin` (`bin/src/main.rs`): the executable wiring up the health
  endpoint and the runner.

SIGINT/SIGTERM stops claiming and lets an active task finish; a second signal
interrupts it.
