# Splatter Capability Contracts

This document defines the runtime contract for the splatter capabilities implemented by the Rust runner.

## Docker images (v0.2)

- **Image:** multi-stage `Dockerfile` (rust → LichtFeld build → lean runtime without `/app/vcpkg`). Tag e.g. `splatter-server:v0.2-dev`; rebuild when LichtFeld pin, base CUDA stack, or product Python/config changes.

## Product artifact split (v0.2)

| Capability | Product upload format | Typical artifact names |
|---|---|---|
| `/splatter/colmap/v1` | **`.splat`** (`data_type=splat_data`) | `refined_splat_<suffix>` — DMT ≤10-scan path |
| `/splatter/local/v1` | **SOG only** (`local_splat_sog`) | `local_splat_sog_<scan_id>.local_splat_sog` |
| `/splatter/global/v1` | **SOG partitions only** (`splat_partition_sog`) | `splat_partition_{coarse\|fine}_10_<x>_<z>_<manifestTimestamp>.splat_partition_sog` |

Colmap keeps the v0.1 **filter → rotate → `.splat`** contract for DMT. Local/global do **not** upload product `.splat` or PLY (train may still export PLY/SOG on disk for filtering/combine; only SOG is staged for domain upload).

## Trainer recipe (local + colmap)

Locked 2026-10-28 (LichtFeld Studio **v0.5.4.2**; Splattr preprocess; no RoMa densify; no monodepth priors; train-time Gaussian densification = stock LFS defaults):

Product **local** + **colmap** paths: Splattr **`preprocessing.main`** / **`preprocess` + undistort** → sparse COLMAP in `dense/` → `train_helper.sh` (no custom `--init`).

| Knob | Value |
|---|---|
| LichtFeld pin | **v0.5.4.2** (Docker `LICHTFELD_GIT_REF`) |
| Strategy / max_cap / bg | **`config/lichtfeld_optimization_params.json`**: `mrnf`, **`max_cap=1_000_000`**, **`bg_mode=random`** (no recipe env vars) |
| POPSpa / sparsity | **Not shipped** — absent from config and product CLI/env |
| Export name | Helper passes LFS `--output-name splat_<iters>` (stable export stem) |
| Iterations / resize | **30000** / **-r 2** |
| Depth prior (SSI) | **off** |
| MoGe preprocess | **off** (helper skips); normal maps via LFS **`normal_auto_generate`** when priors on |
| NC / flatten / prior normals | **0.01** / **0.1** / **0.05** (`normal_auto_generate=true`) |
| `normal_start_fraction` | **LFS default (0.08)** — not overridden |
| Custom dense init (`--init`) | Not used on product path; helper still accepts `--init` for manual experiments |
| Opacity LR / reg | stock LFS **0.025** / **0.01** |
| Scale reg / decay | stock LFS **0.01** / **0.002** |
| `min_opacity` (train) | **0.01** (post-filter still ~0.03) |
| `max_screen_share` / oversize split | **0.2** / **0.25** |
| Exposure | **on** (helper `--no-exposure` to disable) |
| Train background | **`bg_mode=random`** (per-pixel noise each iter; not solid black / not modulation) |
| Preprocess (in helper) | **none** (no MoGe / SSI depth) — prior normals from LFS auto-generate |
| Crop | Node `crop_aabb.py`: **1% per-axis trim + 0.3 m pad** on init/dense PLY (else COLMAP points3D) → `train_helper.sh --cropbox x0,y0,z0,x1,y1,z1`; `--no-crop` to skip |
| Occlusion masks | Before train, `black_pixel_masks.py` scans scene images; pure-black pixels → sidecar **`masks/`** (same relative paths as images; **0=ignore, 255=keep**). When any mask exists, helper adds **`--mask-mode ignore`** and **`--no-alpha-as-mask`**. |

Recipe source of truth: **`config/lichtfeld_optimization_params.json`**. `scripts/train_helper.sh` passes scene/runtime flags only (`-d/-o`, `-i/-r`, eval/export, cropbox, masks); optional local overrides: `--no-exposure`, `--ld`, `--init`, `--no-crop`.

**Post-filter** (unchanged product cleanup): opacity prune logit `-3.5` (≈0.03). That is above train `min_opacity`; it still strips floaters after the softer train floor.

## Preprocessing: Splattr only (no MoGe, no RoMa, no VDA)

1. **`preprocessing.py` (product path)**  
   Full Splattr preprocess (camera/point cleanup) + undistort into `dense/`. Black occlusion boxes → train **`masks/`** sidecars (not frame drop). No RoMa densification; no Video-Depth-Anything.

2. **`train_helper.sh`**  
   **Skips MoGe preprocess.** Recipe (NC / flatten / prior normals / screen-share / min_opacity) comes from **config JSON** only.

### Notes

- **Keep Splattr camera/point cleanup** (sparse-view cull, blur Laplacian variance &lt; 20). Black rects are **not** whole-frame culls; they are masked at train time.
- **RoMa densify and VDA are not shipped** in the product image or runner (no plugin bake, no torch stack for matching/densify).
- Prior normals: `normal_loss_weight=0.05` + `normal_auto_generate=true` (LFS MoGe-2 at train start when maps missing).

## Capability: `/splatter/colmap/v1`

- **Trainer**: `scripts/train_helper.sh` (config-primary; `-i` default **30000**, `-r 2`; exports `ply,sog`). Post-filter opacity prune logit `-3.5` (≈0.03).
- **Pipeline** (`splatter_pipeline.py` `colmap_v1_single_splat`): merged frames + global COLMAP → Splattr preprocess + undistort → same train_helper path as local (masks, cropbox) → filter → coordinate transform → **`refined/splatter/splat_rot.splat`**. Runner uploads that file only (no SOG product).
- **Status**: Backward-compatible required output kept.
- **Runner defaults** (override via env; local/global differ — see table above):
  - `SPLATTER_CONVERT_TO_SOG=false` (colmap default; train SOG on disk is not uploaded)
  - `SPLATTER_CONVERT_TO_SPLAT=true` (product `.splat` path; colmap mode always produces `splat_rot.splat` regardless of CLI convert flags)
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

- **Trainer**: `scripts/train_helper.sh` (`-i` default **30000**, `-r 2`; same self-supervised geo config as colmap; exports `ply,sog`). Post-filter opacity prune logit `-3.5` (≈0.03).
- **Input expectation** (preferred: direct dataset URLs):
  - **Direct inputs**: `inputsCids` may list full domain data URLs — one or more `dmt_recording_<scan_id>` (`data_type=dmt_recording_mp4`) and matching `refined_scan_<scan_id>` (`data_type=refined_scan_zip`) per scan. The runner stages them under `datasets/<scan_id>/Frames.mp4` and `datasets/<scan_id>/RefinedScan.zip`. Scan folder id is derived from **artifact name / data_type** (not the on-disk path stem). No `local_splat_input_*` envelope is required.
  - **Legacy envelope**: a task input CID may still point to a manifest JSON `{ "dataIDs": [ ... ] }` listing UUIDs or artifact names.
  - Referenced domain data for each scan includes:
    - `dmt_recording_mp4`
    - `refined_scan_zip`
  - If `dataIDs` list artifact **names** (`refined_scan_<scan_id>` / `dmt_recording_<scan_id>`) instead of UUIDs, the runner resolves both the recording and the refined zip by name.
- **Primary outputs** (SOG-only product path):
  - `local_splat_sog_<scan_id>.local_splat_sog` (`data_type=local_splat_sog`) — one per scan.
  - No product upload of `local_splat_*` (`.splat`) or `local_splat_ply_*` on this capability.
- **Runner defaults** (override via env):
  - `SPLATTER_CONVERT_TO_SOG=true` (default for local)
  - `SPLATTER_CONVERT_TO_SPLAT=false` (default for local)

## Capability: `/splatter/global/v1`

- **Input expectation**:
  - Task input CID points to a manifest JSON containing `dataIDs`.
  - Referenced domain data includes:
    - `refined_manifest_json`
    - one or more `local_splat_sog` artifacts (legacy `local_splat_ply` still accepted for download)
  - If `dataIDs` list artifact **names** (`local_splat_sog_<scan_id>`, `refined_scan_<scan_id>` → `local_splat_sog_<scan_id>`, `refined_manifest…`) instead of UUIDs, the runner resolves them by name.
  - Extra `inputs_cids` that are bare artifact names (not URLs) are resolved the same way.
- **Primary outputs** (SOG-only product path):
  - Partitioned SOG artifacts only (`data_type=splat_partition_sog`):
    - `splat_partition_{coarse|fine}_<size>_<x>_<z>_<manifestTimestamp>.splat_partition_sog`
    - Example: `splat_partition_coarse_10_2_-2_2026-09-17_14-55-56.splat_partition_sog`
    - After combine, the hub splat is split into **coarse** (sigmoid opacity ≥ 0.6, min size 0.01) and **fine** (remainder); each LOD is partitioned at **10 m** tiles.
    - `manifestTimestamp` is the suffix from the input `refined_manifest_*` artifact (same refinement as the reconstruction).
    - No `splat_partition_full_*` artifacts on this path.
  - Partitioned `.splat` files are **not** produced or uploaded on this capability.
- **Runner defaults** (override via env):
  - `SPLATTER_CONVERT_TO_SOG=true` (default for global and local only)
  - `SPLATTER_CONVERT_TO_SPLAT=false` (default for global and local only)
  - `SPLATTER_PARTITION_SIZE=10` (meters; global coarse/fine tiles)
  - Global combine accepts staged local SOG at `refined/local/<scan_id>/splat/splat_<iterations>.filtered.sog` (default iterations: 30000).
- **Notes**:
  - Combined unpartitioned `combined_splat.ply` is not uploaded by default.
