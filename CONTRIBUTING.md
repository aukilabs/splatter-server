# Contributing to splatter-server

Thank you for your interest in contributing. We welcome issues and pull requests.

## How to contribute

- **Bug reports and feature ideas:** Open an [issue](https://github.com/aukilabs/splatter-server/issues).
- **Code changes:** Open a pull request (PR) against `develop`. Use a branch name that matches our CI (e.g. `feature/your-feature`, `fix/fix-description`, `chore/your-change`).

## Code standards

- **Rust:** In `server/rust`, please run before pushing:
  - `cargo fmt --all`
  - `cargo clippy --workspace --all-targets --all-features -- -D warnings`
  - `cargo test --workspace --all-features`
- CI runs the same checks on push; keep the pipeline green.

## Pull request process

1. Point your PR at the `develop` branch.
2. Ensure CI passes (Rust format, clippy, tests).
3. Keep changes focused; link related issues where applicable.

By contributing, you agree that your contributions will be licensed under the same [MIT License](LICENSE) that covers this project.

# Local Development

## Development setup

- **Rust (compute node):** See [server/rust/README.md](server/rust/README.md). Copy `server/rust/.env.example` to `server/rust/.env` and configure for local DDS/DMS if needed.
- **Docker:** From the repo root, `docker build -t splatter-server .` then run with `--env-file .env`.

## Run Trainer

Node capabilities are driven by `splatter_pipeline.py` (see `docs/compute-capability.md` for contracts):

```bash
# Legacy single-scan .splat upload path
python3 splatter_pipeline.py --mode colmap_v1_single_splat --job_root_path {path/to/job/root}

# Per-scan local SOG
python3 splatter_pipeline.py --mode local_only --job_root_path {path/to/job/root} --scan_ids {scan_id}

# Global merge + 10 m SOG partitions
python3 splatter_pipeline.py --mode global_only --job_root_path {path/to/job/root} \
  --scan_ids {scan_id,...} --manifest_timestamp {YYYY-MM-DD_HH-MM-SS}
```

## Input Files
```bash
# Input Files (local / colmap)
{job_root_path}
├── datasets
│   └── {scan_id}
│       ├── Frames.mp4          # or extracted Frames/
│       └── RefinedScan.zip     # COLMAP *.{bin,csv,txt}
└── refined
    └── global
        └── refined_manifest.json   # global mode only
```

## Output Files
```bash
# Output Files (node capability paths)
{job_root_path}
├── refined
│   ├── splatter
│   │   └── splat_rot.splat              # /splatter/colmap/v1 upload
│   ├── local
│   │   └── {scan_id}/splat/
│   │       └── splat_{iters}.filtered.sog   # /splatter/local/v1
│   └── global
│       ├── coarse_partition_*.sog
│       └── fine_partition_*.sog         # staged as splat_partition_* for /splatter/global/v1
└── output/                              # staged domain-upload names
```
