# Splatter Capability Contracts

This document defines the runtime contract for the splatter capabilities implemented by the Rust runner.

## Capability: `/splatter/colmap/v1`

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

- **Trainer**: R2 via `scripts/train_helper.sh` (`-i` default 20000, `-r 2`, SH2, random bg, depth SSI + normals; exports `ply,sog`). Legacy colmap path keeps `use_train_helper=False`.
- **Input expectation**:
  - Task input CID points to a manifest JSON containing `dataIDs`.
  - Referenced domain data for each scan includes:
    - `dmt_recording_mp4`
    - `refined_scan_zip`
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
- **Primary outputs** (SOG-only product path):
  - Partitioned SOG artifacts only:
    - `splat_partition_sog_<suffix>.splat_partition_sog`
  - Partitioned `.splat` files are **not** produced or uploaded on this capability.
- **Runner defaults** (override via env):
  - `SPLATTER_CONVERT_TO_SOG=true` (default for global; local/colmap default false)
  - `SPLATTER_CONVERT_TO_SPLAT=false` (default for global; local/colmap default true)
  - Local PLY handoff expects `splat_<iterations>.filtered.ply` (default iterations: 20000).
- **Notes**:
  - Combined unpartitioned `combined_splat.ply` is not uploaded by default.

