# Splatter capability contracts

Runtime contract for Splatter capabilities implemented by the Rust runner (`server/rust/runner`).

Task inputs are always **`inputs_cids`**: each entry is either a **domain-data URL** or an **artifact name** (same pattern as reconstruction global). Names are resolved with `io.find(name, Some(data_type))`.

## Docker / image

Multi-stage `Dockerfile` (Rust → LichtFeld build → lean runtime). Rebuild when the LichtFeld pin, CUDA stack, or product Python/config changes.

## Product outputs

| Capability | Product upload | Typical artifact names |
|---|---|---|
| `/splatter/colmap/v1` | `.splat` (`data_type=splat_data`) | `refined_splat_<suffix>` |
| `/splatter/local/v1` | SOG (`local_splat_sog`) | `local_splat_sog_<scan_id>.local_splat_sog` |
| `/splatter/global/v1` | SOG partitions (`splat_partition_sog`) | `splat_partition_{coarse\|fine}_10_<x>_<z>_<manifestTimestamp>.splat_partition_sog` |

## Trainer recipe (local + colmap)

Local and colmap product paths use LichtFeld Studio **v0.5.4.2**: Splatter **`preprocessing.main`** / **`preprocess` + undistort** → sparse COLMAP in `dense/` → `train_helper.sh`.

| Knob | Value |
|---|---|
| LichtFeld pin | **v0.5.4.2** (Docker `LICHTFELD_GIT_REF`) |
| Strategy / max_cap / bg | **`config/lichtfeld_optimization_params.json`**: `mrnf`, **`max_cap=1_000_000`**, **`bg_mode=random`** |
| Export name | Helper passes LFS `--output-name splat_<iters>` |
| Iterations / resize | **30000** / **-r 2** (override via `SPLATTER_ITERATIONS`) |
| Depth prior (SSI) | **off** |
| MoGe preprocess | **off** |
| NC / flatten / prior normals | **0.01** / **0.1** / **0.05** |
| `normal_start_fraction` | **0.03** |
| Opacity LR / reg | stock LFS **0.025** / **0.01** |
| Scale reg / decay | stock LFS **0.01** / **0.002** |
| `min_opacity` (train) | **0.01** |
| `max_screen_share` / oversize split | **0.2** / **0.25** |
| Exposure | **on** |
| Train background | **`bg_mode=random`** |
| Crop | Node `crop_aabb.py`: **1% per-axis trim + 0.3 m pad** |
| Occlusion masks | `black_pixel_masks.py` → sidecar **`masks/`** |

Recipe source: **`config/lichtfeld_optimization_params.json`**. Post-filter opacity prune logit **-3.5** (≈0.03).

Global combine expects local SOG at `refined/local/<scan_id>/splat/splat_<iterations>.filtered.sog`. **`SPLATTER_PARTITION_SIZE=10`** (meters) for global tiles.

---

## `/splatter/colmap/v1`

**Inputs (`inputs_cids`)** — URLs and/or names:

| Name pattern | `data_type` | Staged path |
|---|---|---|
| `dmt_recording_<scan_id>` (optional `.mp4` stripped for lookup) | `dmt_recording_mp4` | `datasets/<scan_id>/Frames.mp4` |
| `refined_scan_<scan_id>` | `refined_scan_zip` | `datasets/<scan_id>/RefinedScan.zip` |

After inputs are staged, the runner loads matching **`colmap_*_bin`** artifacts using the refinement suffix derived from input artifact names (`refined_manifest*` suffix when present).

**Required output**

- One artifact: `data_type=splat_data`, name `refined_splat_<suffix>` (or `refined_splat` if suffix unknown) — file `refined/splatter/splat_rot.splat`.

---

## `/splatter/local/v1`

**Inputs (`inputs_cids`)** — per scan, URLs and/or names:

| Name pattern | `data_type` | Staged path |
|---|---|---|
| `dmt_recording_<scan_id>` | `dmt_recording_mp4` | `datasets/<scan_id>/Frames.mp4` |
| `refined_scan_<scan_id>` | `refined_scan_zip` | `datasets/<scan_id>/RefinedScan.zip` |

Scan folder id comes from **artifact name / data_type**, not the materialized file path stem.

**Required outputs**

- One `local_splat_sog` per scan: `local_splat_sog_<scan_id>.local_splat_sog`.

---

## `/splatter/global/v1`

**Inputs (`inputs_cids`)** — URLs and/or names:

| Name pattern | `data_type` | Staged path |
|---|---|---|
| `refined_manifest*` | `refined_manifest_json` | `refined/global/refined_manifest.json` |
| `local_splat_sog_<scan_id>` | `local_splat_sog` | `refined/local/<scan_id>/splat/splat_<iterations>.filtered.sog` |
| `refined_scan_<scan_id>` (when local SOG UUID unknown) | resolves as `local_splat_sog_<scan_id>` / `local_splat_sog` | same SOG path as above |

DMT often lists artifact **names** in `inputs_cids` because domain UUIDs are not known when the job is composed (same as reconstruction global).

**Required outputs**

- Partition SOG artifacts only (`data_type=splat_partition_sog`):
  - `splat_partition_{coarse|fine}_<size>_<x>_<z>_<manifestTimestamp>.splat_partition_sog`
  - `manifestTimestamp` from the input `refined_manifest_*` artifact name suffix.
