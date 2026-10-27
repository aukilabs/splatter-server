# Splatter Capability Contracts

This document defines the runtime contract for the splatter capabilities implemented by the Rust runner.

## Product artifact split (v0.2)

| Capability | Product upload format | Typical artifact names |
|---|---|---|
| `/splatter/colmap/v1` | **`.splat`** (`data_type=splat_data`) | `refined_splat_<suffix>` — DMT ≤10-scan path |
| `/splatter/local/v1` | **SOG only** (`local_splat_sog`) | `local_splat_sog_<scan_id>.local_splat_sog` |
| `/splatter/global/v1` | **SOG partitions only** (`splat_partition_sog`) | `splat_partition_{coarse\|fine}_10_<x>_<z>_<manifestTimestamp>.splat_partition_sog` |

Colmap keeps the v0.1 **filter → rotate → `.splat`** contract for DMT. Local/global do **not** upload product `.splat` or PLY (train may still export PLY/SOG on disk for filtering/combine; only SOG is staged for domain upload).

## Trainer recipe (local + colmap)

Locked 2026-10-25 (LichtFeld Studio **v0.5.4.2**; no monodepth priors; train-time densify = stock LFS defaults):

### RoMa dense init (pre-train)

| Knob | Value |
|---|---|
| Plugin | [Lichtfeld-Densification-Plugin](https://github.com/shadygm/Lichtfeld-Densification-Plugin) |
| RoMa setting | **`turbo`** (`DENSIFY_ROMA_SETTING`) |
| Reference fraction | **`0.6`** (`DENSIFY_NUM_REFS`) |
| Output | Dense PLY → train **`--init`** (sparse COLMAP unchanged) |
| Disable | `SPLATTER_DENSIFY=0` (legacy Splattr preprocess path) |
| Plugin root | `DENSIFY_PLUGIN_ROOT` (default `/app/Lichtfeld-Densification-Plugin`; cloned at Docker build, pin `DENSIFY_PLUGIN_GIT_REF`) |
| Densify Python | `DENSIFY_PYTHON` (default `/opt/densify-venv/bin/python` — **not** `/app/venv`; keeps train `pycolmap==3.13` isolated from plugin `pycolmap` 4.x) |
| RoMa v2 weights | Baked to `${TORCH_HOME}/hub/checkpoints/romav2.pt` (`TORCH_HOME=/opt/densify-venv/.torch` in image). Build arg `ROMA_WEIGHTS_URL` (default [Parskatt release](https://github.com/Parskatt/RoMaV2/releases/download/weights/romav2.pt)); empty URL fails the image build |

Product **local** + **colmap** paths: undistort-only staging → densify CLI → `train_helper.sh --init <dense_init.ply>`.

| Knob | Value |
|---|---|
| LichtFeld pin | **v0.5.4.2** (Docker `LICHTFELD_GIT_REF`) |
| Strategy | `mrnf` / FastGS, `max_cap=1M` |
| Iterations / resize | **30000** / **-r 2** |
| Depth prior (SSI) | **off** |
| Depth distortion \(L_d\) | **off** (optional `--ld` override in helper) |
| MoGe preprocess | **off** (not baked in product image) |
| NC / flatten / prior normals | **0.1** / **0.1** / **off** |
| `normal_start_fraction` | **LFS default (0.08)** — not overridden |
| Dense init (`--init`) | Passed through helper when `init_ply` is set |
| Opacity LR / reg | stock LFS **0.025** / **0.01** |
| Scale reg / decay | stock LFS **0.01** / **0.002** |
| `min_opacity` (train) | stock LFS **0.005** |
| `max_screen_share` / oversize split | stock LFS **0.3** / **0.15** |
| Exposure | **on** (helper `--no-exposure` to disable) |
| Preprocess (in helper) | **none** (no MoGe / VDA / DA3) |
| Crop | Node `crop_aabb.py`: **1% per-axis trim + 0.3 m pad** on init/dense PLY (else COLMAP points3D) → `train_helper.sh --cropbox x0,y0,z0,x1,y1,z1`; `--no-crop` to skip |
| Occlusion masks | Before train, `black_pixel_masks.py` scans scene images; pure-black pixels → sidecar **`masks/`** (same relative paths as images; **0=ignore, 255=keep**). When any mask exists, helper adds **`--mask-mode ignore`** and **`--no-alpha-as-mask`**. |

Recipe sources: `config/lichtfeld_optimization_params.json` + `scripts/train_helper.sh`.

**Post-filter** (unchanged product cleanup): opacity prune logit `-3.5` (≈0.03). That is above train `min_opacity`; it still strips floaters after the softer train floor.

## Preprocessing: Splattr only (no MoGe)

1. **`preprocessing.py` (product path with densify)**  
   **Undistort-only** into `dense/` (camera cull for blur / too-few points; **no** sparse point outlier voxel cleanup). Black occlusion boxes → train **`masks/`** sidecars (not frame drop). Then **`densify_init.run_densify`** (RoMa **turbo**, **0.6** refs) → dense PLY for **`--init`**.  
   Legacy path when `SPLATTER_DENSIFY=0`: full Splattr preprocess + undistort, no dense init.

2. **`train_helper.sh`**  
   **Skips MoGe.** Default train path does not pass `--use-depth-distortion` (\(L_d\) off). NC + flatten from helper flags (no SSI depth prior, no prior normals).

### Notes

- **Keep Splattr camera/point cleanup** (sparse-view cull, blur Laplacian variance &lt; 20). Black rects are **not** whole-frame culls; they are masked at train time.
- **Do not enable VDA / DA3** on the helper path — `use_vda` remains off for product jobs. **`--init`** is forwarded via `train_helper.sh` when densify produced `dense_init.ply`.
- NC needs `--use-normal-loss` as a schedule gate only; prior normal weight stays **0**.

## Capability: `/splatter/colmap/v1`

- **Trainer**: `scripts/train_helper.sh` (same recipe as local: `-i` default **30000**, `-r 2`; self-supervised geo in `config/lichtfeld_optimization_params.json`; exports `ply,sog`). Sparsity flag is accepted but ignored on the helper path. Post-filter opacity prune logit `-3.5` (≈0.03).
- **Pipeline** (`splatter_pipeline.py` `colmap_v1_single_splat`): merged frames + global COLMAP → undistort-only (`undistort_merged_colmap`) → RoMa densify (`turbo`, `0.6` refs) when `SPLATTER_DENSIFY` enabled → same train_helper path as local (masks, cropbox, `--init`) → filter → coordinate transform → **`refined/splatter/splat_rot.splat`**. Runner uploads that file only (no SOG product).
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
