# Splatter Capability Contracts

This document defines the runtime contract for the splatter capabilities implemented by the Rust runner.

## Trainer recipe (local + colmap) — s6_ld50 geo

Locked 2026-09-25 (LichtFeld LEP A/B + visual):

| Knob | Value |
|---|---|
| Strategy | `mrnf` / FastGS, `max_cap=1M` |
| Depth prior (SSI) | weight **0.15** (built-in exponential decay → 2% by last iter) |
| Depth distortion \(L_d\) | **50** (locked; **CLI skipped on nodes** until binary ≥ `3693dfb73`) |
| NC / flatten / prior normals | **0.01** / **0.5** / **0.05** |
| `normal_start_fraction` | **0.03** |
| Opacity LR / reg | **0.006** / **0.0015** |
| Scale reg / decay | **0.001** / **0.002** |
| `min_opacity` (train) | ≈ **0.004** |
| `max_screen_share` / oversize split | **0.2** / **0.25** |
| Exposure | **off** (helper `--exposure` opt-in) |
| Preprocess (in helper) | MoGe-2 **depth + normals** (`LichtFeld-Studio preprocess --mode both`) |
| Crop | `scripts/auto_fit_cropbox.py` (trimmed); `--no-crop` to skip |

Recipe sources: `config/lichtfeld_optimization_params.json` + `scripts/train_helper.sh`.
Product calls pass `--ld 0` so pre-L_d binaries keep working; config still records Ld=50. Enable with `--ld 50` after a full image rebuild that pins LichtFeld ≥ `3693dfb73` (`--use-depth-distortion`).

**Post-filter** (unchanged product cleanup): opacity prune logit `-3.5` (≈0.03). That is above train `min_opacity`; it still strips floaters after the softer train floor.

## Preprocessing: Splattr vs MoGe (how they fit)

Two separate stages — **do not conflate**:

1. **`preprocessing.py` (Splattr / COLMAP path)**  
   Runs *before* train on refined scans: optional BA, undistort into `dense/`, statistical/radius outlier cleanup on sparse points, camera culling (too few close points / black / blur).  
   **Does not** write monocular depth or normals. It prepares a clean COLMAP scene for LichtFeld.

2. **`train_helper.sh` MoGe-2**  
   Runs *at train start* on `<scene>/images`: writes `depths/` + `normals/` (or Studio’s expected layout) used by SSI depth prior + normal losses.  
   **\(L_d\) does not need depth maps** (per-ray FastGS variance), but NC / flatten / prior normals / SSI depth **do**.

### Recommendations

- **Always keep MoGe in the helper** for this recipe. Skipping it silences depth/normal losses while leaving weights enabled (wasted compute / weaker geo).
- **Keep Splattr camera/point cleanup** — it improves SfM quality and which views train; complementary to MoGe.
- **Prefer SSI depth at 0.15** (current). Higher priors (0.2–2.0) can bend monodepth bias into the splat; the built-in decay already softens late training.
- **Do not replace MoGe with VDA on the helper path** — `use_vda` / `--init` are ignored when `use_train_helper=True`. VDA remains a legacy/direct-Studio path only.
- **Bake MoGe weights** in the image (`~/.lichtfeld/onnx/` / release `.lfw`) so GPU nodes need no egress.
- **Undistorted images** from Splattr must stay the ones MoGe sees (`dense/images` → helper scene `images/`). Mixing distorted frames with COLMAP poses hurts both SfM and SSI.
- If MoGe fails on a subset of frames, LichtFeld logs cameras-without-depth; recipe still runs but geo terms weaken on those views — fix coverage rather than raising depth weight.

## Capability: `/splatter/colmap/v1`

- **Trainer**: `scripts/train_helper.sh` (same recipe as local: `-i` default 20000, `-r 2`; s6_ld50 geo in `config/lichtfeld_optimization_params.json`; exports `ply,sog`). Sparsity flag is accepted but ignored on the helper path. Post-filter opacity prune logit `-3.5` (≈0.03).
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

- **Trainer**: `scripts/train_helper.sh` (`-i` default 20000, `-r 2`; same s6_ld50 config as colmap; exports `ply,sog`). Post-filter opacity prune logit `-3.5` (≈0.03).
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
