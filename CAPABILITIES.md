# Splatter Capability Contracts

This document defines the runtime contract for the splatter capabilities implemented by the Rust runner.

## Capability: `/splatter/colmap/v1`

- **Trainer**: `scripts/train_helper.sh` (same recipe as local: `-i` default 20000, `-r 2`; recipe in `config/lichtfeld_optimization_params.json` including anti-haze `min_opacity=0.02` / `scale_reg=0.01` / `scale_decay=0.003` / `oversize_split_fraction=0.25`; exports `ply,sog`). Sparsity flag is accepted but ignored on the helper path. Post-filter opacity prune logit `-3.5` (≈0.03; slightly above train `min_opacity=0.02`).
- **Status**: Backward-compatible required output kept.
- **Input expectation**:
  - Task input CID points to a manifest JSON containing `dataIDs`.
  - Referenced domain data includes one or more `dmt_recording_*` datasets.
  - Referenced domain data may include refined COLMAP binaries matching the refinement suffix.
- **Required output**:
  - Exactly one uploaded artifact with:
    - `data_type`: `splat_data`
    - `name`: `refined_splat_<suffix>` (or `refined_splat` if suffix is unavailable)
- **Optional outputs**:
  - Enabled by `SPLATTER_UPLOAD_DEBUG_ARTIFACTS=true`.
  - Additional debug artifacts may be uploaded with `data_type` prefixed by `debug_`.

## Capability: `/splatter/local/v1`

- **Trainer**: `scripts/train_helper.sh` (`-i` default 20000, `-r 2`; same `config/lichtfeld_optimization_params.json` as colmap; exports `ply,sog`). Post-filter opacity prune logit `-3.5` (≈0.03; slightly above train `min_opacity=0.02`).
- **Input expectation** (preferred: direct dataset URLs):
  - **Direct inputs**: `inputsCids` may list full domain data URLs — one or more `dmt_recording_<scan_id>` (`data_type=dmt_recording_mp4`) and matching `refined_scan_<scan_id>` (`data_type=refined_scan_zip`) per scan. The runner stages them under `datasets/<scan_id>/Frames.mp4` and `datasets/<scan_id>/RefinedScan.zip`. Scan folder id is derived from **artifact name / data_type** (not the on-disk path stem). No `local_splat_input_*` envelope is required.
  - **Legacy envelope**: a task input CID may still point to a manifest JSON `{ "dataIDs": [ ... ] }` listing UUIDs or artifact names.
  - Referenced domain data for each scan includes:
    - `dmt_recording_mp4`
    - `refined_scan_zip`
  - If `dataIDs` list artifact **names** (`refined_scan_<scan_id>` / `dmt_recording_<scan_id>`) instead of UUIDs, the runner resolves both the recording and the refined zip by name.
- **Primary outputs**:
  - `local_splat_<scan_id>.local_splat` (`data_type=local_splat`) when splat conversion is enabled.
  - `local_splat_ply_<scan_id>.local_splat_ply` (`data_type=local_splat_ply`) as fallback.
- **Optional outputs**:
  - `local_splat_sog_<scan_id>.local_splat_sog` (`data_type=local_splat_sog`) when enabled.

## Capability: `/splatter/global/v1`

- **Input expectation**:
  - Task input CID points to a manifest JSON containing `dataIDs`.
  - Referenced domain data includes:
    - `refined_manifest_json`
    - one or more `local_splat_ply` artifacts
  - If `dataIDs` list artifact **names** (`local_splat_ply_<scan_id>`, `refined_scan_<scan_id>` → `local_splat_ply_<scan_id>`, `refined_manifest…`) instead of UUIDs, the runner resolves them by name.
  - Extra `inputs_cids` that are bare artifact names (not URLs) are resolved the same way.
- **Primary outputs** (SOG-only product path):
  - Partitioned SOG artifacts only (`data_type=splat_partition_sog`):
    - `splat_partition_{coarse|fine}_<size>_<x>_<z>_<manifestTimestamp>.splat_partition_sog`
    - Example: `splat_partition_coarse_5_2_-2_2026-09-17_14-55-56.splat_partition_sog`
    - After combine, the hub splat is split into **coarse** (sigmoid opacity ≥ 0.6, min size 0.01) and **fine** (remainder); each LOD is partitioned at **5 m** tiles.
    - `manifestTimestamp` is the suffix from the input `refined_manifest_*` artifact (same refinement as the reconstruction).
    - No `splat_partition_full_*` artifacts on this path.
  - Partitioned `.splat` files are **not** produced or uploaded on this capability.
- **Runner defaults** (override via env):
  - `SPLATTER_CONVERT_TO_SOG=true` (default for global; local/colmap default false)
  - `SPLATTER_CONVERT_TO_SPLAT=false` (default for global; local/colmap default true)
  - Local PLY handoff expects `splat_<iterations>.filtered.ply` (default iterations: 20000).
- **Notes**:
  - Combined unpartitioned `combined_splat.ply` is not uploaded by default.

