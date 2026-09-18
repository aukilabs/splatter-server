#!/usr/bin/env python3
"""
filter_splats.py

Filter a Gaussian-splat PLY (e.g. from LichtFeld Studio) by opacity, size,
and view count.

Typical 3DGS / LichtFeld PLY vertex fields include:
  x, y, z, nx, ny, nz, f_dc_0... (SH colors),
  opacity,
  scale_0, scale_1, scale_2,
  rot_0...rot_3,
  f_rest_* ...

This script:
  * Loads the PLY
  * Computes:
      - opacity values (optionally passed through sigmoid)
      - a scalar size from scale_0..2 (geometric mean of linear scales)
      - view count (number of cameras that see each gaussian)
  * Keeps only gaussians that satisfy the given thresholds
  * Writes a new PLY file, preserving the original format (ASCII vs binary)

Usage examples
--------------
Filter out very transparent splats, using raw opacity (pre-sigmoid, default):

    python filter_splats.py -i input.ply -o output.ply --min-opacity -2.0

Filter on *actual* opacity in [0,1] (apply sigmoid first):

    python filter_splats.py -i input.ply -o output.ply \
        --opacity-space sigmoid --min-opacity 0.1 --max-opacity 0.95

Filter by size (in "linear" space, i.e. after exp on log-scales):

    python filter_splats.py -i input.ply -o output.ply \
        --min-size 0.01 --max-size 0.5

Filter by view count (remove gaussians seen by fewer than N cameras):

    python filter_splats.py -i input.ply -o output.ply \
        --min-view-count 3 --colmap-path /path/to/colmap/sparse/0

Filter by view count with depth constraints:

    python filter_splats.py -i input.ply -o output.ply \
        --min-view-count 3 --colmap-path /path/to/colmap \
        --view-min-depth 0.2 --view-max-depth 5.0

Using job-root mode with view count filtering:

    python filter_splats.py --job-root /path/to/job --min-view-count 3
"""

import argparse
import sys
import numpy as np
from plyfile import PlyData
import open3d as o3d
from pathlib import Path
from collections import defaultdict
import pycolmap

def default_args() -> argparse.Namespace:
    return argparse.Namespace(
        input_ply=None,
        output_ply=None,
        output_ply_2=None,
        job_root=None,
        splat_filename="splat_20000",
        overwrite=False,
        min_opacity=None,
        max_opacity=None,
        opacity_space="raw",
        min_size=None,
        max_size=None,
        size_space="linear",
        outlier_radius=None,
        outlier_neighbors=10,
        view_min_depth=0.1,
        view_max_depth=10.0,
        min_view_count=0,
        colmap_path=None,
        colmap_rel_path="dense/sparse"
    )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Filter Gaussian-splat PLY files (LichtFeld Studio / 3DGS) "
                    "by opacity and size."
    )
    parser.add_argument("--input-ply", "-i", help="Input Gaussian-splat .ply file", default=None)
    parser.add_argument("--output-ply", "-o", help="Output .ply file after filtering", default=None)
    parser.add_argument(
        "--output-ply-2",
        nargs="?",
        const=True,
        default=None,
        help=(
            "Optional output .ply file for removed splats (split operation). "
            "In explicit mode, provide a path. In job-root mode, use as a flag "
            "to auto-derive paths as {splat_filename}.filtered_removed.ply"
        ),
    )
    parser.add_argument("--job-root", type=Path, help="Job root directory, to process splats within refinement", default=None)
    parser.add_argument("--splat-filename", type=str, help="Base name of the splat file to filter", default="splat_20000")
    parser.add_argument("--overwrite", action="store_true", help="Force re-processing of existing output splat PLYs.")

    # Opacity options
    parser.add_argument(
        "--min-opacity",
        type=float,
        default=None,
        help=(
            "Minimum opacity threshold. "
            "Interpretation depends on --opacity-space."
        ),
    )
    parser.add_argument(
        "--max-opacity",
        type=float,
        default=None,
        help=(
            "Maximum opacity threshold. "
            "Interpretation depends on --opacity-space."
        ),
    )
    parser.add_argument(
        "--opacity-space",
        choices=["raw", "sigmoid"],
        default="raw",
        help=(
            "How to interpret the stored 'opacity' field:\n"
            "  raw     : use the stored value directly (3DGS-style logit).\n"
            "  sigmoid : apply 1 / (1 + exp(-opacity)) and threshold in [0,1]."
        ),
    )

    # Size options
    parser.add_argument(
        "--min-size",
        type=float,
        default=None,
        help=(
            "Minimum size threshold. Size is derived from scale_0..2 using the "
            "geometric mean of linear scales (exp of log-scales) by default, "
            "or directly in log-space if --size-space=log."
        ),
    )
    parser.add_argument(
        "--max-size",
        type=float,
        default=None,
        help=(
            "Maximum size threshold. See --min-size for size definition."
        ),
    )
    parser.add_argument(
        "--size-space",
        choices=["linear", "log"],
        default="linear",
        help=(
            "Space in which to apply size thresholds:\n"
            "  linear : thresholds in the space of linear scales (after exp).\n"
            "  log    : thresholds in log-scale space directly."
        ),
    )

    # Outlier removal options
    parser.add_argument(
        "--outlier-radius",
        type=float,
        default=None,
        help=(
            "Radius for outlier removal. If set, removes points that have fewer "
            "than --outlier-neighbors within this radius. Default: None (disabled)."
        ),
    )
    parser.add_argument(
        "--outlier-neighbors",
        type=int,
        default=10,
        help=(
            "Minimum number of neighbors required within --outlier-radius. "
            "Points with fewer neighbors are removed. Default: 10."
        ),
    )

    # View count filtering options
    parser.add_argument(
        "--min-view-count",
        type=int,
        default=None,
        help=(
            "Minimum number of cameras that must see a gaussian for it to be kept. "
            "Requires --colmap-path. Default: None (disabled)."
        ),
    )
    parser.add_argument(
        "--colmap-path",
        type=Path,
        default=None,
        help=(
            "Path to COLMAP reconstruction folder (containing cameras.bin, images.bin). "
            "Required for --min-view-count filtering in explicit mode."
        ),
    )
    parser.add_argument(
        "--colmap-rel-path",
        type=str,
        default="dense/sparse",
        help=(
            "Relative path from job_root/refined/local/<scan_id> to COLMAP folder. "
            "Used with --job-root mode. Default: dense/sparse"
        ),
    )
    parser.add_argument(
        "--view-min-depth",
        type=float,
        default=0.1,
        help=(
            "Minimum depth (meters) for a gaussian to count as seen by a camera. "
            "Default: 0.1"
        ),
    )
    parser.add_argument(
        "--view-max-depth",
        type=float,
        default=10.0,
        help=(
            "Maximum depth (meters) for a gaussian to count as seen by a camera. "
            "Default: 10.0"
        ),
    )

    args = parser.parse_args()
    
    if args.min_view_count is not None and args.colmap_path is None and args.job_root is None:
        parser.error("--min-view-count requires --colmap-path or --job-root")
    
    if not args.job_root and (not args.input_ply or not args.output_ply):
        parser.error("Either --job-root or both --input-ply and --output-ply must be provided")
    if args.job_root and (args.input_ply or args.output_ply):
        parser.error("If --job-root is provided, --input-ply and --output-ply are not used")

    return args

def sigmoid(x: np.ndarray) -> np.ndarray:
    """Numerically stable-ish sigmoid."""
    # Avoid overflow on huge negatives/positives
    out = np.empty_like(x, dtype=np.float64)
    pos_mask = x >= 0
    neg_mask = ~pos_mask

    out[pos_mask] = 1.0 / (1.0 + np.exp(-x[pos_mask]))
    exp_x = np.exp(x[neg_mask])
    out[neg_mask] = exp_x / (1.0 + exp_x)
    return out


def compute_opacity_values(vert, args: argparse.Namespace) -> np.ndarray:
    if "opacity" not in vert.dtype.names:
        raise KeyError(
            "PLY vertex element does not contain an 'opacity' field. "
            "Is this really a Gaussian-splat PLY?"
        )

    raw = vert["opacity"].astype(np.float64)

    if args.opacity_space == "raw":
        return raw
    else:
        # Map to [0,1]
        return sigmoid(raw)



def compute_view_counts(
    rec: pycolmap.Reconstruction,
    xyz: np.ndarray = None,
    min_depth: float = 0.1,
    max_depth: float = 5.0
) -> np.ndarray:
    """
    Count how many cameras see each 3D point.
    
    A point is considered "seen" by a camera if:
    - It projects inside the image bounds
    - It is in front of the camera (positive depth)
    - Its depth is within [min_depth, max_depth]
    
    Args:
        rec: pycolmap.Reconstruction object
        xyz: (N, 3) array of 3D points, or None to use 3D points from the reconstruction
        min_depth: Minimum valid depth
        max_depth: Maximum valid depth
    
    Returns:
        (N,) array of view counts
    """
    if xyz is None:
        # Use points from reconstruction if none provided
        xyz = np.stack([
            rec.points3D[pid].xyz for pid in rec.point3D_ids()
        ], axis=0)
    n_points = len(xyz)
    point_view_counts = np.zeros(n_points, dtype=np.int32)
    point_counts_per_cam = defaultdict(int)

    for img_id, img in rec.images.items():
        cam = rec.cameras[img.camera_id]

        fx = cam.focal_length_x
        fy = cam.focal_length_y
        cx = cam.principal_point_x
        cy = cam.principal_point_y
        width = cam.width
        height = cam.height

        R = img.cam_from_world().rotation.matrix()
        t = img.cam_from_world().translation

        # Transform points to camera coordinates: p_cam = R @ p_world + t
        p_cam = (R @ xyz.T).T + t
        
        # Depth is z coordinate in camera space
        depth = p_cam[:, 2]
        
        # Check depth bounds (must be in front of camera and within range)
        valid_depth = (depth > min_depth) & (depth < max_depth)
        
        # Project to image plane
        # u = fx * x/z + cx, v = fy * y/z + cy
        with np.errstate(divide='ignore', invalid='ignore'):
            u = fx * p_cam[:, 0] / depth + cx
            v = fy * p_cam[:, 1] / depth + cy
        
        # Check if within image bounds
        valid_uv = (u >= 0) & (u < width) & (v >= 0) & (v < height)
        
        # Point is seen if both depth and projection are valid
        seen = valid_depth & valid_uv
        seen_01 = seen.astype(np.int32)
        point_view_counts += seen_01 # +1 for each point this camera sees

        point_counts_per_cam[cam.camera_id] = seen_01.sum() # Total number of points seen by this camera
    
    # We can filter both low confidence points and cams based on the same logic
    return point_view_counts, point_counts_per_cam


def compute_size_values(vert, args: argparse.Namespace) -> np.ndarray:
    """Compute a scalar size from scale_0, scale_1, scale_2."""
    names = vert.dtype.names or ()
    required = ("scale_0", "scale_1", "scale_2")

    if not all(n in names for n in required):
        raise KeyError(
            "PLY vertex element does not contain scale_0/1/2 fields. "
            "Cannot compute size-based filter."
        )

    s0 = vert["scale_0"].astype(np.float64)
    s1 = vert["scale_1"].astype(np.float64)
    s2 = vert["scale_2"].astype(np.float64)

    # Log-scales: these are log of axis radii in most 3DGS implementations.
    log_geom_mean = (s0 + s1 + s2) / 3.0

    if args.size_space == "log":
        # Threshold directly in log-space
        return log_geom_mean
    else:
        # Linear size: geometric mean of linear scales
        linear_mean = np.sqrt((np.exp(s0)**2 + np.exp(s1)**2 + np.exp(s2)**2) / 3.0)
        return linear_mean
        #return np.exp(log_geom_mean)


def filter_ply(input_ply, output_ply, args, colmap_path=None, output_ply_2=None):
    print(f"Loading PLY: {input_ply}")
    ply = PlyData.read(input_ply)

    if "vertex" not in ply:
        print("Error: PLY file has no 'vertex' element.", file=sys.stderr)
        sys.exit(1)

    vertex_el = ply["vertex"]
    vert = vertex_el.data  # numpy structured array

    num_vertices = len(vert)
    print(f"Found {num_vertices} gaussians (vertices).")

    # Track indices into original array throughout all filtering stages
    kept_indices = np.arange(num_vertices)

    # Start with "keep everything"
    mask = np.ones(num_vertices, dtype=bool)

    # --- Opacity filtering ---
    if args.min_opacity is not None or args.max_opacity is not None:
        try:
            opacity_vals = compute_opacity_values(vert, args)
        except KeyError as e:
            print(f"Warning: {e}. Skipping opacity-based filtering.", file=sys.stderr)
        else:
            if args.min_opacity is not None:
                mask &= opacity_vals >= args.min_opacity
            if args.max_opacity is not None:
                mask &= opacity_vals <= args.max_opacity

    # --- Size filtering ---
    if args.min_size is not None or args.max_size is not None:
        try:
            size_vals = compute_size_values(vert, args)
        except KeyError as e:
            print(f"Warning: {e}. Skipping size-based filtering.", file=sys.stderr)
        else:
            if args.min_size is not None:
                mask &= size_vals >= args.min_size
            if args.max_size is not None:
                mask &= size_vals <= args.max_size

    # Apply opacity/size mask first
    kept_count = int(mask.sum())
    removed_count = num_vertices - kept_count
    print(f"After opacity/size filter: {kept_count} / {num_vertices} gaussians (removed {removed_count})")

    kept_indices = kept_indices[mask]
    filtered_vert = vert[mask]

    # --- View count filtering (on already filtered vertices) ---
    if args.min_view_count is not None and colmap_path is not None and len(filtered_vert) > 0:
        print(f"Loading cameras from: {colmap_path}")
        try:
            rec = pycolmap.Reconstruction()
            rec.read(str(colmap_path))
            cameras = rec.cameras

            print(f"Loaded {len(cameras)} cameras")

            xyz = np.column_stack([
                filtered_vert["x"].astype(np.float64),
                filtered_vert["y"].astype(np.float64),
                filtered_vert["z"].astype(np.float64),
            ])

            print(f"Computing view counts (min_depth={args.view_min_depth}, max_depth={args.view_max_depth})...")
            view_counts, _ = compute_view_counts(rec, xyz, args.view_min_depth, args.view_max_depth)

            view_mask = view_counts >= args.min_view_count
            removed_by_view = int((~view_mask).sum())
            print(f"View count filter: removing {removed_by_view} gaussians seen by < {args.min_view_count} cameras")

            kept_indices = kept_indices[view_mask]
            filtered_vert = filtered_vert[view_mask]
            print(f"After view count filter: {len(filtered_vert)} gaussians remaining")
        except Exception as e:
            print(f"Error: Failed to load cameras: {e}. Skipping view count filtering.", file=sys.stderr)
            raise

    if len(filtered_vert) == 0:
        print("Warning: all gaussians were filtered out!", file=sys.stderr)

    # --- Outlier removal (after view count filter) ---
    if args.outlier_radius is not None and len(filtered_vert) > 0:
        print(f"Running radius outlier removal (radius={args.outlier_radius}, "
              f"min_neighbors={args.outlier_neighbors})...")

        xyz = np.column_stack([
            filtered_vert["x"].astype(np.float64),
            filtered_vert["y"].astype(np.float64),
            filtered_vert["z"].astype(np.float64),
        ])

        print("XYZ shape: ", xyz.shape)

        pcd = o3d.geometry.PointCloud()
        pcd.points = o3d.utility.Vector3dVector(xyz)

        print("calling remove_radius_outlier")
        _, inlier_indices = pcd.remove_radius_outlier(
            nb_points=args.outlier_neighbors,
            radius=args.outlier_radius
        )

        outlier_count = len(filtered_vert) - len(inlier_indices)
        print(f"Removed {outlier_count} outliers, {len(inlier_indices)} remaining.")

        kept_indices = kept_indices[inlier_indices]
        filtered_vert = filtered_vert[inlier_indices]

    vertex_el.data = filtered_vert

    # Write out kept splats
    print(f"Writing filtered PLY to: {output_ply}")
    ply.write(output_ply)

    # Write out removed splats if requested
    if output_ply_2 is not None:
        removed_mask = np.ones(num_vertices, dtype=bool)
        removed_mask[kept_indices] = False
        removed_vert = vert[removed_mask]

        print(f"Writing {len(removed_vert)} removed splats to: {output_ply_2}")
        ply_removed = PlyData.read(input_ply)
        ply_removed["vertex"].data = removed_vert
        ply_removed.write(output_ply_2)
    


def get_colmap_path_for_scan(job_root: Path, scan_id: str, colmap_rel_path: str = "dense/sparse") -> Path:
    """
    Get the COLMAP reconstruction path for a scan.
    
    Args:
        job_root: Job root directory
        scan_id: Scan ID
        colmap_rel_path: Relative path from job_root/refined/local/<scan_id> to COLMAP folder
    
    Returns:
        Path to COLMAP reconstruction, or None if not found
    """
    scan_root = job_root / "refined" / "local" / scan_id
    colmap_path = scan_root / colmap_rel_path
    
    if colmap_path.exists():
        if (colmap_path / "cameras.bin").exists() or (colmap_path / "cameras.txt").exists():
            return colmap_path
    
    return None


def main(args: argparse.Namespace) -> None:

    merged_args = default_args()
    merged_args.__dict__.update(args.__dict__)
    args = merged_args

    print("filter splat args: ", args.__dict__)

    if args.job_root:
        scan_ids = [f.name for f in (args.job_root / "datasets").iterdir() if f.is_dir()]
        print(f"Going to process {len(scan_ids)} scans in job root {args.job_root}")

        splats_in_out_colmap = [
            (
                args.job_root / "refined" / "local" / scan_id / "splat" / f"{args.splat_filename}.ply",
                args.job_root / "refined" / "local" / scan_id / "splat" / f"{args.splat_filename}.filtered.ply",
                scan_id
            )
            for scan_id in scan_ids
        ]
        
        splats_in_out_colmap = [
            (in_ply, out_ply, scan_id) 
            for in_ply, out_ply, scan_id in splats_in_out_colmap 
            if in_ply.exists()
        ]

        print(f"Found {len(splats_in_out_colmap)} existing splats")

        if not args.overwrite:
            prev_count = len(splats_in_out_colmap)
            splats_in_out_colmap = [
                (in_ply, out_ply, scan_id) 
                for in_ply, out_ply, scan_id in splats_in_out_colmap 
                if not out_ply.exists()
            ]
            print(f"Skipping {prev_count - len(splats_in_out_colmap)} splats which have been filtered already. Use --overwrite to process them again.")

        for in_ply, out_ply, scan_id in splats_in_out_colmap:
            colmap_path = None
            if args.min_view_count is not None:
                colmap_path = get_colmap_path_for_scan(args.job_root, scan_id, args.colmap_rel_path)
                if colmap_path is None:
                    print(f"Warning: Could not find COLMAP for scan {scan_id} at {args.colmap_rel_path}, skipping view count filter.", file=sys.stderr)
            out_ply_2 = None
            if args.output_ply_2 is not None:
                out_ply_2 = (
                    args.job_root / "refined" / "local" / scan_id / "splat"
                    / f"{args.splat_filename}.filtered_removed.ply"
                )
            filter_ply(in_ply, out_ply, args, colmap_path=colmap_path, output_ply_2=out_ply_2)
    else:
        colmap_path = args.colmap_path
        out_ply_2 = None
        if args.output_ply_2 is not None:
            if args.output_ply_2 is True:
                out_base = Path(args.output_ply)
                out_ply_2 = str(out_base.with_suffix(".removed.ply"))
            else:
                out_ply_2 = args.output_ply_2
        filter_ply(args.input_ply, args.output_ply, args, colmap_path=colmap_path, output_ply_2=out_ply_2)

    print("Done.")


if __name__ == "__main__":
    args = parse_args()
    main(args)
