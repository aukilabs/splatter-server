# SOG Compression Pipeline Design

## Summary

Add SOG (Spatially Ordered Gaussians) compression as a post-processing step in the splatter pipeline. The compressed `.sog` file is uploaded alongside the existing `.splat` file, giving the domain-viewer a ~15-20x smaller file to load while preserving the lossless `.splat` as a backup.

## Motivation

The splatter server currently outputs a raw `.splat` file (hundreds of MB for large domains). The domain-viewer's Spark WASM engine already supports the SOG compressed format (`PCSOGSZIP`), mapping `data_type: splat_data_sog` to automatic decompression on the client. Adding SOG compression reduces file transfer sizes by ~15-20x, dramatically improving viewer load times with minimal visual quality loss.

## Design

### Pipeline Change

Current:
```
extract_mp4 -> ns-process-data -> ns-train -> ns-export -> rotate_ply -> convert_ply2splat -> upload .splat
```

New:
```
extract_mp4 -> ns-process-data -> ns-train -> ns-export -> rotate_ply -> convert_ply2splat -> compress_sog -> upload .sog -> upload .splat
```

SOG is uploaded first (smaller file, faster upload, viewer can display sooner). The lossless `.splat` follows as a backup.

### Files to Create

#### `compress_sog.py`
New Python script following the same pattern as `convert_ply2splat.py`:
- CLI args: `--input` (PLY path), `--output` (SOG output path)
- Uses `3dgsconverter` library to convert PLY to SOG format
- Prints compression stats (input size, output size, ratio)
- Exit code 0 on success, non-zero on failure

### Files to Modify

#### `run.py`
Add step after `convert_ply2splat.py`:
```python
# Compress to SOG (best-effort -- don't fail the pipeline if this fails)
exit_code = run_python_script("compress_sog.py",
                              "--input", args.job_root_path / "refined/splatter/splat_rot.ply",
                              "--output", args.job_root_path / "refined/splatter/splat_rot.sog")
if exit_code != 0:
    logger.warning("SOG compression failed; .splat file is still available")
```

SOG compression failure is non-fatal. The `.splat` is already produced and the pipeline exits successfully.

#### `server/rust/runner/src/lib.rs`
After the existing upload block, add SOG upload logic:
- Check if `refined/splatter/splat_rot.sog` exists in the workspace
- If present, upload as `data_type: "splat_data_sog"` with name `refined_splat_sog_{suffix}`
- Upload SOG **before** the existing `.splat` upload (SOG first so the viewer gets usable data sooner)
- If SOG file is missing (compression failed), skip silently and proceed to `.splat` upload

#### `Dockerfile`
Add `3dgsconverter` to the pip install line:
```dockerfile
RUN python3 -m pip install --no-cache-dir ply2splat git+https://github.com/francescofugazzi/3dgsconverter.git@0.8
```

Also copy the new `compress_sog.py` script:
```dockerfile
COPY run.py extract_mp4.py convert_ply2splat.py rotate_ply.py compress_sog.py /app/
```

### Error Handling

SOG compression is best-effort throughout the entire stack:

| Layer | Behavior on SOG failure |
|-------|------------------------|
| `compress_sog.py` | Returns non-zero exit code |
| `run.py` | Logs warning, does NOT exit -- pipeline continues |
| Rust runner | Checks if `.sog` file exists before upload; skips if missing |
| Domain viewer | Falls back to `splat_data` if no `splat_data_sog` exists |

The existing `.splat` pipeline is never affected by SOG failures.

### Upload Order

1. Upload `.sog` as `splat_data_sog` (smaller, faster, viewer can start loading)
2. Upload `.splat` as `splat_data` (lossless backup)

If the process is interrupted after step 1, the viewer still has a fully viewable domain.

### Viewer Integration

No viewer changes required. The domain-viewer already:
1. Scans data list for items with `data_type` matching `splat_data_sog`
2. Maps to `SplatFileType.PCSOGSZIP` via `dataTypeToSplatFileType()` in `hooks/useRefinementSplat.ts:38-48`
3. Passes format to Spark WASM engine which handles decompression
4. Falls back to `splat_data` items if no SOG exists

### Compression Characteristics

SOG compression is lossy for higher spherical harmonics bands (SH degrees 1-3) but lossless for:
- Positions (x, y, z)
- Rotations (quaternions)
- Scales
- Base color (SH degree 0)
- Opacity

Visual impact is negligible for indoor phone scans where base color dominates.

### Output Files

After the pipeline completes, the workspace contains:
```
refined/splatter/
  splat.ply          # raw nerfstudio export
  splat_rot.ply      # coordinate-transformed PLY
  splat_rot.splat    # lossless binary format
  splat_rot.sog      # SOG compressed (new)
```

### Testing

- Run the pipeline locally with sample data and verify `.sog` is produced
- Verify `.sog` loads correctly in the domain-viewer
- Verify pipeline completes successfully when `3dgsconverter` is unavailable (fallback)
- Compare file sizes between `.splat` and `.sog` outputs
