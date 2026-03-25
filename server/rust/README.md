# Hello Runner workspace

This directory vendors the `posemesh` hello runner example so you can boot the
compute node engine locally before building a real runner. Crates are pinned to
the requested versions:

- `posemesh-domain-http = 1.3.2`
- `posemesh-compute-node-runner-api = 0.1.2`
- `posemesh-compute-node = 0.3.2`

## Quick start
1) Copy `.env.example` to `.env` and fill in real DDS/DMS values (base URLs,
   registration secret, secp256k1 private key, etc).
2) From this directory run `make run` (or `cargo run -p splatter-bin`). The
   binary loads `.env` automatically and starts the HTTP server on
   `0.0.0.0:8080`.
3) Use `scripts/curl-create-hello-job.sh` to enqueue a demo job
   once the node has registered with DDS/DMS.

Packages:

- `splatter-runner` (`runner/src/lib.rs`): the hello runner implementation
  (capability logic).
- `splatter-bin` (`bin/src/main.rs`): the executable wiring up the compute node
  HTTP server and registering the runner.

Adjust the runner library as needed for the Splatter workload.
