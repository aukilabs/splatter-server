"""
Global splat processing: combine, partition, and convert splats.

This module handles post-training steps:
1. Combine individual scan splats into a single file
2. Partition the combined splat into tiles
3. Convert PLY files to .splat and/or .sog formats
"""

from pathlib import Path
from typing import Optional
import argparse
import time

import combine_splats
import partition_splat
import convert_splat


def combine(job_root: Path, scan_ids: list[str], use_filtered: bool = True,
            base_filename: str = "splat_20000") -> Path:
    """Combine splats from multiple scans into a single file."""
    print(f"[global] Combining splats from {len(scan_ids)} scans...")
    
    combine_args = argparse.Namespace(
        job_root=job_root,
        scan_ids=scan_ids,
        use_filtered=use_filtered,
        splats=None,
        transforms=None,
        output=None,
        best_per_voxel=True,
        voxel_size=0.5,
        min_gaussians_per_voxel=20,
        save_aligned_scans=False,
        base_filename=base_filename
    )
    combine_splats.combine_splats_from_job_root(combine_args)
    
    combined_ply_path = job_root / "refined" / "global" / "combined_splat.ply"
    print(f"[global] Combined PLY: {combined_ply_path}")
    return combined_ply_path


def partition(combined_ply_path: Path, partition_size: float = 5.0) -> list[Path]:
    """Partition a combined splat into tiles."""
    print(f"[global] Partitioning with size {partition_size}m...")
    
    partition_paths = partition_splat.partition_splat(
        input_path=combined_ply_path,
        partition_size=partition_size,
        output_dir=combined_ply_path.parent,
    )
    
    print(f"[global] Created {len(partition_paths)} partitions")
    return partition_paths


def convert_to_splat(ply_paths: list[Path], skip_combined: bool = True,
                     combined_ply_path: Optional[Path] = None):
    """Convert PLY files to .splat format."""
    print("[global] Converting to .splat format...")
    
    for ply_path in ply_paths:
        if skip_combined and combined_ply_path and ply_path == combined_ply_path:
            continue  # skip combined file (too big, OOM risk)
        if ply_path.suffix == ".ply":
            try:
                convert_splat.convert_ply_to_splat(ply_path)
            except Exception as e:
                print(f"[global] Error converting {ply_path.name}: {e}")


def convert_to_sog(ply_paths: list[Path], skip_combined: bool = True,
                   combined_ply_path: Optional[Path] = None):
    """Convert PLY files to .sog format."""
    print("[global] Converting to .sog format...")
    
    for ply_path in ply_paths:
        if skip_combined and combined_ply_path and ply_path == combined_ply_path:
            continue  # skip combined file (too big, OOM risk)
        if ply_path.suffix == ".ply":
            try:
                convert_splat.convert_ply_to_sog(ply_path)
            except Exception as e:
                print(f"[global] Error converting {ply_path.name} to SOG: {e}")


def convert_aligned_scans(job_root: Path, to_splat: bool = True, to_sog: bool = False):
    """Convert aligned scan splats to .splat and/or .sog format."""
    aligned_dir = job_root / "refined" / "global" / "aligned_splats"
    if not aligned_dir.exists():
        return
    
    aligned_plys = [p for p in aligned_dir.iterdir() if p.suffix == ".ply"]
    if to_splat:
        convert_to_splat(aligned_plys, skip_combined=False)
    if to_sog:
        convert_to_sog(aligned_plys, skip_combined=False)


def main(job_root: Path, scan_ids: list[str],
         base_filename: str = "splat_20000",
         sparsify_steps: int = 0,
         use_filtered: bool = True,
         do_partition: bool = True, partition_size: float = 5.0,
         do_convert_splat: bool = True, do_convert_sog: bool = False) -> dict:
    """
    Run global splat processing pipeline.
    
    Returns dict with results for API response.
    """
    start_time = time.time()
    
    # Step 1: Combine splats
    combined_ply_path = combine(job_root, scan_ids, use_filtered, base_filename)
    output_files = [combined_ply_path]
    
    # Step 2: Partition if requested
    partition_count = 0
    if do_partition:
        partition_paths = partition(combined_ply_path, partition_size)
        partition_count = len(partition_paths)
        output_files.extend(partition_paths)
        # TODO: Generate LOD levels
    
    # Step 3: Convert formats
    if do_convert_splat:
        convert_to_splat(output_files, skip_combined=do_partition,
                         combined_ply_path=combined_ply_path)
        #convert_aligned_scans(job_root, to_splat=True, to_sog=False)
    
    if do_convert_sog:
        convert_to_sog(output_files, skip_combined=do_partition,
                       combined_ply_path=combined_ply_path)
        #convert_aligned_scans(job_root, to_splat=False, to_sog=True)
    
    elapsed = time.time() - start_time
    print(f"[global] Done in {elapsed:.1f}s")
    
    return {
        "combined_ply": str(combined_ply_path),
        "partitioned": do_partition,
        "partition_count": partition_count,
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Global splat processing")
    parser.add_argument("--job-root", type=Path, required=True)
    parser.add_argument("--scan-ids", type=str, nargs="+", default=None,
                        help="Scan IDs to process (default: all in datasets/)")
    parser.add_argument("--base-filename", type=str, default="splat_20000")
    parser.add_argument("--sparsify-steps", type=int, default=0)
    parser.add_argument("--use-filtered", action="store_true", default=True)
    parser.add_argument("--no-partition", action="store_true", default=False)
    parser.add_argument("--partition-size", type=float, default=5.0)
    parser.add_argument("--convert-splat", action="store_true", default=True)
    parser.add_argument("--convert-sog", action="store_true", default=False)
    
    args = parser.parse_args()
    
    if args.scan_ids is None:
        datasets_dir = args.job_root / "datasets"
        scan_ids = [f.name for f in datasets_dir.iterdir() if f.is_dir()][-20:]
    else:
        scan_ids = args.scan_ids
    
    main(
        job_root=args.job_root,
        scan_ids=scan_ids,
        base_filename=args.base_filename,
        sparsify_steps=args.sparsify_steps,
        use_filtered=args.use_filtered,
        do_partition=not args.no_partition,
        partition_size=args.partition_size,
        do_convert_splat=args.convert_splat,
        do_convert_sog=args.convert_sog,
    )

