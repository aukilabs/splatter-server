#!/usr/bin/env python3
"""
Mode-based splatter pipeline entrypoint.

This wrapper keeps algorithm code in existing modules and only adapts task
workspace layout + output naming for capability contracts.
"""

from __future__ import annotations

import argparse
import os
import shutil
import sys
import zipfile
from pathlib import Path

import cv2
import numpy as np
from plyfile import PlyData, PlyElement

import convert_splat
import filter_splats
import global_main
import local_main
import preprocessing
from combine_splats import PRE_ROTATION_MATRIX, transform_splat_data
from artifact_naming import rename_for_domain_upload


def _bool_env(name: str, default: bool) -> bool:
    v = os.environ.get(name)
    if v is None:
        return default
    return v.strip().lower() in {"1", "true", "yes", "on"}


def mp4_to_frames(mp4_path: Path, frames_path: Path, filename_prefix: str = "") -> int:
    capture = cv2.VideoCapture(str(mp4_path))
    frame_count = 0
    frames_path.mkdir(parents=True, exist_ok=True)
    while capture.isOpened():
        ret, frame = capture.read()
        if not ret:
            break
        img_path = frames_path / f"{filename_prefix}{frame_count:06d}.jpg"
        if not img_path.exists():
            cv2.imwrite(str(img_path), frame)
        frame_count += 1
    capture.release()
    return frame_count


def discover_scan_ids(job_root: Path) -> list[str]:
    datasets_dir = job_root / "datasets"
    if not datasets_dir.exists():
        return []
    return sorted([p.name for p in datasets_dir.iterdir() if p.is_dir()])


def ensure_frames_for_scan(job_root: Path, scan_id: str) -> Path:
    scan_dir = job_root / "datasets" / scan_id
    frames_dir = scan_dir / "Frames"
    if frames_dir.exists() and any(frames_dir.iterdir()):
        return frames_dir
    mp4_path = scan_dir / "Frames.mp4"
    if not mp4_path.exists():
        raise FileNotFoundError(f"missing Frames or Frames.mp4 for scan {scan_id}")
    count = mp4_to_frames(mp4_path, frames_dir, f"{scan_id}_")
    if count == 0:
        raise RuntimeError(f"no frames extracted from {mp4_path}")
    return frames_dir


def ensure_sfm_for_scan(job_root: Path, scan_id: str) -> Path:
    sfm_dir = job_root / "refined" / "local" / scan_id / "sfm"
    if sfm_dir.exists() and any(sfm_dir.iterdir()):
        return sfm_dir
    zip_path = job_root / "datasets" / scan_id / "RefinedScan.zip"
    if not zip_path.exists():
        raise FileNotFoundError(f"missing sfm and RefinedScan.zip for scan {scan_id}")
    sfm_dir.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(str(zip_path), "r") as zf:
        zf.extractall(sfm_dir)
    return sfm_dir


def stage_output(output_dir: Path, source: Path, dest_name: str) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, output_dir / dest_name)


def _progress(stage: str, pct: int | None = None, detail: str = "") -> None:
    """Print a structured progress line that the Rust runner parses."""
    parts = [f"[PROGRESS] stage={stage}"]
    if pct is not None:
        parts.append(f"pct={pct}")
    if detail:
        parts.append(f"detail={detail}")
    print(" ".join(parts), flush=True)


def run_colmap_single_splat(job_root: Path, iterations: int, enable_sparsity: bool) -> dict:
    scan_ids = discover_scan_ids(job_root)
    if not scan_ids:
        raise ValueError("no scans found in datasets")

    merged_frames = job_root / "Frames"
    merged_frames.mkdir(parents=True, exist_ok=True)
    for sid in scan_ids:
        scan_dir = job_root / "datasets" / sid
        mp4_path = scan_dir / "Frames.mp4"
        if mp4_path.exists():
            # using exactly the same image naming as in the global reconstruction.
            extracted = mp4_to_frames(mp4_path, merged_frames, f"{sid}_")
            if extracted == 0:
                raise RuntimeError(f"no frames extracted from {mp4_path}")
            continue

        # Fallback for pre-extracted scan-local frames.
        src = ensure_frames_for_scan(job_root, sid)
        for img in src.iterdir():
            if img.is_file() and img.suffix.lower() in {".jpg", ".jpeg", ".png"}:
                dst = merged_frames / f"{sid}_{img.name}"
                if not dst.exists():
                    shutil.copy2(img, dst)

    colmap_dir = job_root / "refined" / "global" / "refined_sfm_combined"
    if not colmap_dir.exists():
        raise FileNotFoundError(f"missing COLMAP directory: {colmap_dir}")

    processed_dir = job_root / "refined" / "splatter" / "processed"
    dense_dir = job_root / "refined" / "splatter" / "dense"
    splat_dir = job_root / "refined" / "splatter"

    shutil.rmtree(processed_dir, ignore_errors=True)
    shutil.rmtree(dense_dir, ignore_errors=True)
    shutil.rmtree(splat_dir, ignore_errors=True)

    processed_dir.mkdir(parents=True, exist_ok=True)
    dense_dir.mkdir(parents=True, exist_ok=True)
    splat_dir.mkdir(parents=True, exist_ok=True)

    _progress("preprocessing", 10, "bundle adjustment")
    preprocessing.preprocess(colmap_dir, processed_dir, frames_dir=merged_frames, bundle_adjust=False)
    _progress("preprocessing", 15, "undistorting images")
    import pycolmap
    pycolmap.undistort_images(
        output_path=str(dense_dir),
        input_path=str(processed_dir),
        image_path=str(merged_frames),
    )

    colmap_sparse = dense_dir / "sparse"
    images_dir = dense_dir / "images"
    sparsify_steps = 15000 if enable_sparsity else 0
    _progress("training", 20, "starting gaussian splatting training")
    local_main.train_splat(
        colmap_dir=colmap_sparse,
        output_dir=splat_dir,
        images_dir=images_dir,
        iterations=iterations,
        enable_sparsity=enable_sparsity,
        sparsify_steps=sparsify_steps,
        init_ply=None,
        use_train_helper=False,
    )

    _progress("filtering", 85, "filtering gaussians")
    total_iters = iterations + sparsify_steps
    input_ply = splat_dir / f"splat_{total_iters}.ply"
    filtered_ply = splat_dir / f"splat_{total_iters}.filtered.ply"
    filter_args = argparse.Namespace(
        input_ply=input_ply,
        output_ply=filtered_ply,
        colmap_path=colmap_sparse,
        overwrite=True,
        min_opacity=-2.2, # logits approx same as 0.1 alpha in 0..1 range
        #max_size=10.0,
        #min_size=0.001,
        min_view_count=5,
        view_min_depth=0.1,
        view_max_depth=3.0,
        outlier_radius=None,
    )
    filter_splats.main(filter_args)

    if not filtered_ply.exists():
        raise FileNotFoundError(f"filtered output missing: {filtered_ply}")

    _progress("postprocessing", 90, "coordinate transform")
    vert = PlyData.read(str(filtered_ply))["vertex"].data
    vert = transform_splat_data(vert, scale=1.0, R=PRE_ROTATION_MATRIX, t=np.zeros(3))
    
    rotated_ply_path = splat_dir / f"splat_{total_iters}.filtered.rotated.ply"
    output_el = PlyElement.describe(vert, "vertex")
    PlyData([output_el], text=False).write(str(rotated_ply_path))

    _progress("converting", 95, "converting to .splat format")
    output_path = splat_dir / "splat_rot.splat"
    convert_splat.convert_ply_to_splat(rotated_ply_path, output_path)

    _progress("done", 100)
    return {
        "scan_ids": scan_ids,
        "outputs": [str(output_path)],
        "filtered_ply": str(filtered_ply),
    }


def run_local_only(
    job_root: Path,
    scan_ids: list[str],
    iterations: int,
    enable_sparsity: bool,
    reuse_trained: bool,
    convert_to_splat: bool,
    convert_to_sog: bool,
) -> dict:
    if not scan_ids:
        scan_ids = discover_scan_ids(job_root)
    if not scan_ids:
        raise ValueError("no scans found for local mode")

    _progress("preprocessing", 0, f"preparing {len(scan_ids)} scan(s)")
    for scan_id in scan_ids:
        ensure_frames_for_scan(job_root, scan_id)
        ensure_sfm_for_scan(job_root, scan_id)

    _progress("training", 10, "starting local splat training")
    local_main.main(
        job_root=job_root,
        scan_ids=scan_ids,
        iterations=iterations,
        enable_sparsity=enable_sparsity,
        sparsify_steps=15000 if enable_sparsity else 0,
        reuse_trained=reuse_trained,
        convert_splat=convert_to_splat,
        convert_sog=convert_to_sog,
        use_vda=False,
    )

    _progress("postprocessing", 90, "staging outputs")
    output_dir = job_root / "output"
    # Local R2 helper ignores sparsity; artifact names use iterations only.
    produced = []
    for scan_id in scan_ids:
        splat_dir = job_root / "refined" / "local" / scan_id / "splat"
        base = f"splat_{iterations}.filtered"
        if convert_to_splat:
            splat_file = splat_dir / f"{base}.splat"
            if splat_file.exists():
                name = f"local_splat_{scan_id}.local_splat"
                stage_output(output_dir, splat_file, name)
                produced.append(name)
        if convert_to_sog:
            sog_file = splat_dir / f"{base}.sog"
            if sog_file.exists():
                name = f"local_splat_sog_{scan_id}.local_splat_sog"
                stage_output(output_dir, sog_file, name)
                produced.append(name)
        ply_file = splat_dir / f"{base}.ply"
        if ply_file.exists():
            name = f"local_splat_ply_{scan_id}.local_splat_ply"
            stage_output(output_dir, ply_file, name)
            produced.append(name)
    _progress("done", 100)
    return {"scan_ids": scan_ids, "outputs": produced}


def run_global_only(
    job_root: Path,
    scan_ids: list[str],
    iterations: int,
    use_filtered: bool,
    partition: bool,
    partition_size: float,
    convert_to_splat: bool,
    convert_to_sog: bool,
) -> dict:
    if not scan_ids:
        scan_ids = discover_scan_ids(job_root)
    if not scan_ids:
        raise ValueError("no scans found for global mode")

    _progress("combining", 0, f"combining {len(scan_ids)} local splat(s)")
    result = global_main.main(
        job_root=job_root,
        scan_ids=scan_ids,
        base_filename=f"splat_{iterations}",
        sparsify_steps=0,
        use_filtered=use_filtered,
        do_partition=partition,
        partition_size=partition_size,
        do_convert_splat=convert_to_splat,
        do_convert_sog=convert_to_sog,
    )

    _progress("postprocessing", 80, "staging outputs")
    output_dir = job_root / "output"
    produced = []
    global_dir = job_root / "refined" / "global"
    if global_dir.exists():
        for f in global_dir.iterdir():
            if not f.is_file():
                continue
            dest_name = rename_for_domain_upload(f.name)
            if dest_name:
                stage_output(output_dir, f, dest_name)
                produced.append(dest_name)
    _progress("done", 100)
    return {"scan_ids": scan_ids, "outputs": produced, "result": result}


def parse_scan_ids(value: str | None) -> list[str]:
    if not value:
        return []
    return [v.strip() for v in value.split(",") if v.strip()]


def main() -> None:
    parser = argparse.ArgumentParser(description="Splatter pipeline entrypoint")
    parser.add_argument("--mode", required=True, choices=["colmap_v1_single_splat", "local_only", "global_only"])
    parser.add_argument("--job_root_path", type=Path, required=True)
    parser.add_argument("--scan_ids", type=str, default="")
    parser.add_argument("--iterations", type=int, default=20000)
    parser.add_argument("--enable_sparsity", action="store_true", default=False)
    parser.add_argument("--reuse_trained", action="store_true", default=False)
    parser.add_argument("--use_filtered", action="store_true", default=True)
    parser.add_argument("--no_use_filtered", action="store_true", default=False)
    parser.add_argument("--partition", action="store_true", default=True)
    parser.add_argument("--no_partition", action="store_true", default=False)
    parser.add_argument("--partition_size", type=float, default=2.0)
    parser.add_argument("--convert_to_splat", action="store_true", default=True)
    parser.add_argument("--no_convert_to_splat", action="store_true", default=False)
    parser.add_argument("--convert_to_sog", action="store_true", default=False)
    args = parser.parse_args()

    job_root = args.job_root_path
    job_root.mkdir(parents=True, exist_ok=True)
    scan_ids = parse_scan_ids(args.scan_ids)

    if args.mode == "colmap_v1_single_splat":
        run_colmap_single_splat(job_root, args.iterations, args.enable_sparsity)
    elif args.mode == "local_only":
        convert_to_splat = args.convert_to_splat and not args.no_convert_to_splat
        run_local_only(
            job_root=job_root,
            scan_ids=scan_ids,
            iterations=args.iterations,
            enable_sparsity=args.enable_sparsity,
            reuse_trained=args.reuse_trained,
            convert_to_splat=convert_to_splat,
            convert_to_sog=args.convert_to_sog,
        )
    else:
        use_filtered = args.use_filtered and not args.no_use_filtered
        do_partition = args.partition and not args.no_partition
        convert_flags = {
            f for f in sys.argv
            if f in {"--convert_to_sog", "--convert_to_splat", "--no_convert_to_splat"}
        }
        if convert_flags:
            convert_to_splat = args.convert_to_splat and not args.no_convert_to_splat
            convert_to_sog = args.convert_to_sog
        else:
            convert_to_sog = _bool_env("SPLATTER_CONVERT_TO_SOG", True)
            convert_to_splat = _bool_env("SPLATTER_CONVERT_TO_SPLAT", False)
        run_global_only(
            job_root=job_root,
            scan_ids=scan_ids,
            iterations=args.iterations,
            use_filtered=use_filtered,
            partition=do_partition,
            partition_size=args.partition_size,
            convert_to_splat=convert_to_splat,
            convert_to_sog=convert_to_sog,
        )


if __name__ == "__main__":
    main()
