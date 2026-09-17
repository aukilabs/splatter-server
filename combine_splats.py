#!/usr/bin/env python3
"""
combine_splats.py

Merge multiple trained and filtered Gaussian splat PLY files into a single
global coordinate system.

Each input splat is transformed using a SIM3 (similarity transform: scale + rotation + translation)
from the refined_manifest.json. All splats are concatenated into one output file.
The output contains exactly the sum of all input splat counts.

Usage with job-root (recommended):
----------------------------------
    python combine_splats.py --job-root /path/to/job

    This will:
    - Discover scan IDs from job_root/datasets/
    - Read COLMAP Sim3 transforms from job_root/refined/global/refined_manifest.json
      (alignedScans or scanAlignmentTransforms)
    - Load splats from job_root/refined/local/<scan_id>/splat/splat_20000.ply
    - Write output to job_root/refined/global/combined_splat.ply

Usage with explicit files:
--------------------------
    python combine_splats.py \
        --splats splat1.ply splat2.ply splat3.ply \
        --transforms transforms.json \
        --output combined.ply

Transform JSON format (refined_manifest.json style):
Transforms are COLMAP-frame Sim3 (same frame as local refined SFM COLMAP).
Quaternion dict keys x,y,z,w are manifest storage format only (xyzw → wxyz internally).
Use scanAlignmentTransforms or alignedScans (equivalent).

{
    "scanAlignmentTransforms": {
        "scan_id": {
            "localToGlobalColmap": {
                "scale": "1.0",
                "position": {"x": "0", "y": "0", "z": "0"},
                "rotation": {"x": "0", "y": "0", "z": "0", "w": "1"}
            }
        }
    }
}
"""

import argparse
import json
import sys
from pathlib import Path
from typing import Optional

import numpy as np
from numba import njit, prange
from plyfile import PlyData, PlyElement
from pycolmap import Sim3d, Rotation3d

# =============================================================================
# Numba-compiled math helpers (for performance-critical operations)
# =============================================================================

@njit(cache=True)
def _quaternion_multiply_njit(w1: float, x1: float, y1: float, z1: float,
                               w2: float, x2: float, y2: float, z2: float) -> tuple:
    """Multiply two quaternions [w, x, y, z]. Returns (w, x, y, z)."""
    return (
        w1*w2 - x1*x2 - y1*y2 - z1*z2,
        w1*x2 + x1*w2 + y1*z2 - z1*y2,
        w1*y2 - x1*z2 + y1*w2 + z1*x2,
        w1*z2 + x1*y2 - y1*x2 + z1*w2
    )


@njit(cache=True)
def _rotation_matrix_to_quaternion_njit(R: np.ndarray) -> tuple:
    """Convert 3x3 rotation matrix to quaternion (w, x, y, z)."""
    trace = R[0, 0] + R[1, 1] + R[2, 2]
    if trace > 0:
        s = 0.5 / np.sqrt(trace + 1.0)
        w = 0.25 / s
        x = (R[2, 1] - R[1, 2]) * s
        y = (R[0, 2] - R[2, 0]) * s
        z = (R[1, 0] - R[0, 1]) * s
    elif R[0, 0] > R[1, 1] and R[0, 0] > R[2, 2]:
        s = 2.0 * np.sqrt(1.0 + R[0, 0] - R[1, 1] - R[2, 2])
        w = (R[2, 1] - R[1, 2]) / s
        x = 0.25 * s
        y = (R[0, 1] + R[1, 0]) / s
        z = (R[0, 2] + R[2, 0]) / s
    elif R[1, 1] > R[2, 2]:
        s = 2.0 * np.sqrt(1.0 + R[1, 1] - R[0, 0] - R[2, 2])
        w = (R[0, 2] - R[2, 0]) / s
        x = (R[0, 1] + R[1, 0]) / s
        y = 0.25 * s
        z = (R[1, 2] + R[2, 1]) / s
    else:
        s = 2.0 * np.sqrt(1.0 + R[2, 2] - R[0, 0] - R[1, 1])
        w = (R[1, 0] - R[0, 1]) / s
        x = (R[0, 2] + R[2, 0]) / s
        y = (R[1, 2] + R[2, 1]) / s
        z = 0.25 * s
    return (w, x, y, z)


@njit(cache=True, parallel=True)
def _transform_quaternions_batch(rot_0: np.ndarray, rot_1: np.ndarray,
                                  rot_2: np.ndarray, rot_3: np.ndarray,
                                  rw: float, rx: float, ry: float, rz: float,
                                  out_0: np.ndarray, out_1: np.ndarray,
                                  out_2: np.ndarray, out_3: np.ndarray) -> None:
    """
    Transform all quaternions by pre-multiplying with rotation quaternion R_quat.
    Operates in-place on the output arrays.
    """
    n = len(rot_0)
    for i in prange(n):
        qw, qx, qy, qz = rot_0[i], rot_1[i], rot_2[i], rot_3[i]
        
        # q_new = R_quat * q
        nw, nx, ny, nz = _quaternion_multiply_njit(rw, rx, ry, rz, qw, qx, qy, qz)
        
        # normalize
        norm = np.sqrt(nw*nw + nx*nx + ny*ny + nz*nz)
        out_0[i] = nw / norm
        out_1[i] = nx / norm
        out_2[i] = ny / norm
        out_3[i] = nz / norm


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Combine multiple Gaussian splat PLY files into global coordinates."
    )
    # Job-root mode
    parser.add_argument(
        "--job-root",
        type=Path,
        default=None,
        help="Job root directory. Discovers scans from datasets/, reads transforms from "
             "refined/global/refined_manifest.json, loads splats from refined/local/<scan_id>/splat/"
    )
    parser.add_argument(
        "--scan-ids",
        nargs="+",
        default=None,
        help="Specific scan IDs to process (optional, defaults to all scans in datasets/)"
    )
    parser.add_argument(
        "--use-filtered",
        action="store_true",
        help="Use filtered splat (splat_20000.filtered.ply) if available, else fall back to original."
    )
    
    # Explicit file mode
    parser.add_argument(
        "--splats", "-s",
        nargs="+",
        default=None,
        help="Input Gaussian-splat .ply files to combine (explicit mode)."
    )
    parser.add_argument(
        "--transforms", "-t",
        default=None,
        help="JSON file containing SIM3 transforms for each splat file (explicit mode)."
    )
    parser.add_argument(
        "--base-filename",
        default="splat_20000",
        help="Base filename of the splat files to combine. Default: splat_20000"
    )
    
    # Common options
    parser.add_argument(
        "--output", "-o",
        default=None,
        help="Output combined .ply file. Default: job_root/refined/global/combined_splat.ply"
    )
    
    # Voxel-based merging options
    parser.add_argument(
        "--best-per-voxel",
        action="store_true",
        help="Enable voxel-based merging: for each voxel, keep only gaussians from the "
             "splat with the highest gaussian count in that voxel."
    )
    parser.add_argument(
        "--voxel-size",
        type=float,
        default=0.5,
        help="Voxel size in meters for --best-per-voxel merging. Default: 0.5"
    )
    parser.add_argument(
        "--min-gaussians-per-voxel",
        type=int,
        default=10,
        help="Skip voxels with fewer than this many gaussians (helps remove floaters). Default: 10"
    )
    parser.add_argument(
        "--save-aligned-scans",
        action="store_true",
        help="Save each aligned scan as a separate PLY in an 'aligned_splats' folder "
             "next to the combined output. Useful for debugging. Off by default."
    )
    
    args = parser.parse_args()
    
    # Validate argument combinations
    if args.job_root is None and (args.splats is None or args.transforms is None):
        parser.error("Either --job-root or both --splats and --transforms must be provided")
    if args.job_root is not None and (args.splats is not None or args.transforms is not None):
        parser.error("Cannot use --job-root together with --splats or --transforms")
    
    return args


def quaternion_to_rotation_matrix(q: np.ndarray) -> np.ndarray:
    """Convert quaternion [w, x, y, z] to 3x3 rotation matrix."""
    w, x, y, z = q
    return np.array([
        [1 - 2*y*y - 2*z*z,     2*x*y - 2*z*w,     2*x*z + 2*y*w],
        [    2*x*y + 2*z*w, 1 - 2*x*x - 2*z*z,     2*y*z - 2*x*w],
        [    2*x*z - 2*y*w,     2*y*z + 2*x*w, 1 - 2*x*x - 2*y*y]
    ])


def rotation_matrix_to_quaternion(R: np.ndarray) -> np.ndarray:
    """Convert 3x3 rotation matrix to quaternion [w, x, y, z]."""
    trace = np.trace(R)
    if trace > 0:
        s = 0.5 / np.sqrt(trace + 1.0)
        w = 0.25 / s
        x = (R[2, 1] - R[1, 2]) * s
        y = (R[0, 2] - R[2, 0]) * s
        z = (R[1, 0] - R[0, 1]) * s
    elif R[0, 0] > R[1, 1] and R[0, 0] > R[2, 2]:
        s = 2.0 * np.sqrt(1.0 + R[0, 0] - R[1, 1] - R[2, 2])
        w = (R[2, 1] - R[1, 2]) / s
        x = 0.25 * s
        y = (R[0, 1] + R[1, 0]) / s
        z = (R[0, 2] + R[2, 0]) / s
    elif R[1, 1] > R[2, 2]:
        s = 2.0 * np.sqrt(1.0 + R[1, 1] - R[0, 0] - R[2, 2])
        w = (R[0, 2] - R[2, 0]) / s
        x = (R[0, 1] + R[1, 0]) / s
        y = 0.25 * s
        z = (R[1, 2] + R[2, 1]) / s
    else:
        s = 2.0 * np.sqrt(1.0 + R[2, 2] - R[0, 0] - R[1, 1])
        w = (R[1, 0] - R[0, 1]) / s
        x = (R[0, 2] + R[2, 0]) / s
        y = (R[1, 2] + R[2, 1]) / s
        z = 0.25 * s
    return np.array([w, x, y, z])


def quaternion_multiply(q1: np.ndarray, q2: np.ndarray) -> np.ndarray:
    """Multiply two quaternions [w, x, y, z]."""
    w1, x1, y1, z1 = q1
    w2, x2, y2, z2 = q2
    return np.array([
        w1*w2 - x1*x2 - y1*y2 - z1*z2,
        w1*x2 + x1*w2 + y1*z2 - z1*y2,
        w1*y2 - x1*z2 + y1*w2 + z1*x2,
        w1*z2 + x1*y2 - y1*x2 + z1*w2
    ])


def rotation_matrix_z(angle_deg: float) -> np.ndarray:
    """Create a 3x3 rotation matrix for rotation around Z axis."""
    angle_rad = np.deg2rad(angle_deg)
    c, s = np.cos(angle_rad), np.sin(angle_rad)
    return np.array([
        [c, -s, 0],
        [s,  c, 0],
        [0,  0, 1]
    ])

# Pre-rotation applied to splats before alignment (splats are saved rotated)
PRE_ROTATION_Z_DEG = -90.0
PRE_ROTATION_MATRIX = rotation_matrix_z(PRE_ROTATION_Z_DEG)


def load_transform(transform_data: dict, apply_pre_rotation: bool = True) -> tuple[float, np.ndarray, np.ndarray]:
    """
    Parse transform data from JSON.
    Returns (scale, rotation_matrix_3x3, translation_3).
    
    If apply_pre_rotation is True, composes with PRE_ROTATION_MATRIX so that
    splats are first rotated by the pre-rotation, then by the alignment rotation.
    Combined: R_final = R_align @ R_pre

    Supports two formats:
    1. Manifest format (COLMAP-frame localToGlobalColmap from refined_manifest.json):
       {"scale": "1.0", "position": {"x": ..., "y": ..., "z": ...}, 
        "rotation": {"x": ..., "y": ..., "z": ..., "w": ...}}
    2. Simple format:
       {"scale": 1.0, "rotation": [w, x, y, z], "translation": [x, y, z]}
    """
    # Handle scale (may be string in manifest format)
    scale_val = transform_data.get("scale", 1.0)
    scale = float(scale_val) if isinstance(scale_val, str) else scale_val
    
    if "matrix" in transform_data:
        matrix = np.array(transform_data["matrix"])
        R = matrix[:3, :3]
        t = matrix[:3, 3]
    elif "position" in transform_data:
        # Manifest format: position/rotation as dicts with x,y,z,w keys
        pos = transform_data["position"]
        rot = transform_data.get("rotation", {"x": "0", "y": "0", "z": "0", "w": "1"})
        
        t = np.array([float(pos["x"]), float(pos["y"]), float(pos["z"])])
        
        # Manifest xyzw storage -> wxyz for internal quaternion math
        qx, qy, qz, qw = float(rot["x"]), float(rot["y"]), float(rot["z"]), float(rot["w"])
        rotation_wxyz = np.array([qw, qx, qy, qz])
        R = quaternion_to_rotation_matrix(rotation_wxyz)
    else:
        # Simple array format
        rotation = np.array(transform_data.get("rotation", [1, 0, 0, 0]))  # identity quat wxyz
        R = quaternion_to_rotation_matrix(rotation)
        t = np.array(transform_data.get("translation", [0, 0, 0]))
    
    # Compose with pre-rotation: first apply pre-rotation, then alignment
    # p' = scale * R_align @ (R_pre @ p) + t = scale * (R_align @ R_pre) @ p + t
    if apply_pre_rotation:
        R = R @ PRE_ROTATION_MATRIX

    return scale, R, t


def transform_splat_data(vert: np.ndarray, scale: float, R: np.ndarray, t: np.ndarray) -> np.ndarray:
    """
    Apply SIM3 transform to Gaussian splat vertices.
    
    Transforms:
    - Position (x, y, z): p' = s * R @ p + t
    - Rotation (rot_0..3): q' = R_quat * q (compose rotations)
    - Scale (scale_0..2): log(s * exp(scale_i)) = scale_i + log(s)
    """
    vert = vert.copy()
    
    # Transform positions (vectorized numpy)
    x, y, z = vert["x"].astype(np.float64), vert["y"].astype(np.float64), vert["z"].astype(np.float64)
    xyz = np.column_stack([x,y,z])
    xyz_transformed = scale * (xyz @ R.T) + t
    vert["x"] = xyz_transformed[:, 0].astype(vert["x"].dtype)
    vert["y"] = xyz_transformed[:, 1].astype(vert["y"].dtype)
    vert["z"] = xyz_transformed[:, 2].astype(vert["z"].dtype)
    
    # Transform rotations using numba-compiled batch function
    if all(f"rot_{i}" in vert.dtype.names for i in range(4)):
        R_contiguous = np.ascontiguousarray(R, dtype=np.float64)
        rw, rx, ry, rz = _rotation_matrix_to_quaternion_njit(R_contiguous)
        
        # Extract rotation arrays as float64 for numba
        rot_0 = vert["rot_0"].astype(np.float64)
        rot_1 = vert["rot_1"].astype(np.float64)
        rot_2 = vert["rot_2"].astype(np.float64)
        rot_3 = vert["rot_3"].astype(np.float64)
        
        # Output arrays
        out_0 = np.empty_like(rot_0)
        out_1 = np.empty_like(rot_1)
        out_2 = np.empty_like(rot_2)
        out_3 = np.empty_like(rot_3)
        
        _transform_quaternions_batch(rot_0, rot_1, rot_2, rot_3,
                                     rw, rx, ry, rz,
                                     out_0, out_1, out_2, out_3)
        
        vert["rot_0"] = out_0.astype(vert["rot_0"].dtype)
        vert["rot_1"] = out_1.astype(vert["rot_1"].dtype)
        vert["rot_2"] = out_2.astype(vert["rot_2"].dtype)
        vert["rot_3"] = out_3.astype(vert["rot_3"].dtype)
    
    # Transform scales (vectorized numpy, already efficient)
    if scale != 1.0:
        log_scale = np.log(scale)
        for i in range(3):
            field = f"scale_{i}"
            if field in vert.dtype.names:
                vert[field] = vert[field] + log_scale
    
    return vert


def discover_scans_and_transforms(job_root: Path, scan_ids: Optional[list] = None) -> dict:
    """
    Discover scans from job_root/datasets/ and load transforms from refined_manifest.json.
    Returns dict: {scan_id: {"splat_path": Path, "transform": dict}}
    """
    datasets_dir = job_root / "datasets"
    if not datasets_dir.exists():
        raise FileNotFoundError(f"Datasets directory not found: {datasets_dir}")
    
    # Discover scan IDs
    available_scan_ids = [f.name for f in datasets_dir.iterdir() if f.is_dir()]
    print(f"Found {len(available_scan_ids)} scans in {datasets_dir}")
    
    if scan_ids is not None:
        # Filter to requested scans
        missing = set(scan_ids) - set(available_scan_ids)
        if missing:
            print(f"Warning: Requested scans not found: {missing}", file=sys.stderr)
        scan_ids = [s for s in scan_ids if s in available_scan_ids]
    else:
        scan_ids = available_scan_ids
    
    # Load transforms from refined_manifest.json
    manifest_path = job_root / "refined" / "global" / "refined_manifest.json"
    if not manifest_path.exists():
        raise FileNotFoundError(f"Manifest not found: {manifest_path}")
    
    print(f"Loading transforms from: {manifest_path}")
    with open(manifest_path, "r") as f:
        manifest = json.load(f)

    # Reconstruction historically used "alignedScans"; newer manifests may use
    # "scanAlignmentTransforms". Both hold {scan_id: {localToGlobalColmap: {...}}}.
    aligned_scans = (
        manifest.get("alignedScans")
        or manifest.get("scanAlignmentTransforms")
        or {}
    )
    if not aligned_scans:
        raise ValueError(
            "Manifest has no alignedScans or scanAlignmentTransforms; "
            "cannot combine splats without COLMAP Sim3 alignment transforms."
        )

    result = {}
    for scan_id in scan_ids:
        if scan_id not in aligned_scans:
            print(f"Warning: No transform for scan '{scan_id}' in manifest, skipping.", file=sys.stderr)
            continue

        entry = aligned_scans[scan_id]
        transform_data = None
        if isinstance(entry, dict):
            # Prefer new key; accept legacy localToDomain from older drafts.
            transform_data = entry.get("localToGlobalColmap") or entry.get("localToDomain")
            if transform_data is None and (
                "position" in entry or "matrix" in entry or "translation" in entry
            ):
                # Allow bare transform-shaped entries.
                transform_data = entry
        if transform_data is None:
            print(
                f"Warning: No 'localToGlobalColmap' for scan '{scan_id}', skipping.",
                file=sys.stderr,
            )
            continue

        result[scan_id] = {"transform": transform_data}

    return result


def get_splat_path(job_root: Path, scan_id: str, use_filtered: bool, base_filename: str = "splat_20000") -> Optional[Path]:
    """Get the splat PLY path for a scan, preferring filtered version if requested."""
    splat_dir = job_root / "refined" / "local" / scan_id / "splat"

    extensions = ["ply", "splat", "sog"]

    if use_filtered:
        for ext in extensions:
            filtered_path = splat_dir / f"{base_filename}.filtered.{ext}"
            if filtered_path.exists():
                return filtered_path
        return None
    
    for ext in extensions:
        original_path = splat_dir / f"{base_filename}.{ext}"
        if original_path.exists():
            return original_path
    return None


def combine_splats_from_job_root(args: argparse.Namespace) -> None:
    """Process splats using job-root discovery."""
    job_root = args.job_root
    base_filename = args.base_filename if "base_filename" in args.__dict__ else "splat_20000"

    # Discover scans and transforms
    scans = discover_scans_and_transforms(job_root, args.scan_ids)

    print(f"Processing {len(scans)} scans with transforms")
    
    # Find splat files
    for scan_id in list(scans.keys()):
        splat_path = get_splat_path(job_root, scan_id, args.use_filtered, base_filename)
        if splat_path is None:
            print(f"Warning: No splat found for scan '{scan_id}', skipping.", file=sys.stderr)
            del scans[scan_id]
        else:
            scans[scan_id]["splat_path"] = splat_path
    
    if not scans:
        print("Error: No valid scans with splats found.", file=sys.stderr)
        sys.exit(1)
    
    print(f"\nFound {len(scans)} scans with splats:")
    for scan_id, info in scans.items():
        print(f"  - {scan_id}: {info['splat_path'].name}")
    
    # Process splats
    all_vertices = []
    scan_ids_processed = []
    for scan_id, info in scans.items():
        splat_path = info["splat_path"]
        transform_data = info["transform"]
        
        print(f"\nLoading splat: {splat_path}")
        ply = PlyData.read(str(splat_path))
        
        if "vertex" not in ply:
            print(f"Warning: {splat_path} has no vertex element, skipping.", file=sys.stderr)
            continue
        
        vert = ply["vertex"].data
        print(f"  - {len(vert)} gaussians")
        
        scale, R, t = load_transform(transform_data)
        print(f"  - t SIM3: scale={scale:.6f}, t=[{t[0]:.3f}, {t[1]:.3f}, {t[2]:.3f}]")
        
        transformed_vert = transform_splat_data(vert, scale, R, t)
        all_vertices.append(transformed_vert)
        scan_ids_processed.append(scan_id)
    
    # Merge and write output
    output_path = args.output or (job_root / "refined" / "global" / "combined_splat.ply")
    _merge_and_write(all_vertices, output_path, args, scan_ids=scan_ids_processed)


def combine_splats_explicit(args: argparse.Namespace) -> None:
    """Process splats using explicit file paths."""
    print(f"Loading transforms from: {args.transforms}")
    with open(args.transforms, "r") as f:
        transforms = json.load(f)
    
    # Handle manifest-style transforms (alignedScans or scanAlignmentTransforms)
    for key in ("alignedScans", "scanAlignmentTransforms"):
        if key in transforms and isinstance(transforms[key], dict):
            transforms = {
                scan_id: (
                    (data.get("localToGlobalColmap") or data.get("localToDomain") or data)
                    if isinstance(data, dict)
                    else data
                )
                for scan_id, data in transforms[key].items()
            }
            break
    
    all_vertices = []
    scan_ids_processed = []
    
    for idx, splat_path in enumerate(args.splats):
        splat_path = Path(splat_path)
        splat_name = splat_path.name
        # Try to match by filename stem (without extension) or parent folder name
        scan_id = splat_path.parent.parent.name  # e.g., refined/local/<scan_id>/splat/
        
        transform_data = (
            transforms.get(str(splat_path)) or
            transforms.get(splat_name) or
            transforms.get(scan_id)
        )
        if transform_data is None:
            raise ValueError(
                f"No COLMAP Sim3 transform found for splat {splat_path} "
                f"(tried path, filename, scan_id '{scan_id}'); "
                "cannot combine without alignment transforms."
            )
        
        print(f"Loading splat: {splat_path}")
        ply = PlyData.read(str(splat_path))
        
        if "vertex" not in ply:
            print(f"Warning: {splat_path} has no vertex element, skipping.", file=sys.stderr)
            continue
        
        vert = ply["vertex"].data
        print(f"  - {len(vert)} gaussians")
        
        scale, R, t = load_transform(transform_data)
        print(f"  - Applying SIM3: scale={scale:.6f}, t=[{t[0]:.3f}, {t[1]:.3f}, {t[2]:.3f}]")
        
        transformed_vert = transform_splat_data(vert, scale, R, t)
        all_vertices.append(transformed_vert)
        # Use scan_id if found in transforms, otherwise use index-based name
        scan_ids_processed.append(scan_id if scan_id in transforms else f"splat_{idx}")
    
    if not args.output:
        print("Error: --output is required in explicit mode.", file=sys.stderr)
        sys.exit(1)
    
    _merge_and_write(all_vertices, Path(args.output), args, scan_ids=scan_ids_processed)


def compute_voxel_hash(x: float, y: float, z: float, voxel_size: float) -> tuple:
    """Compute a spatial hash (voxel index tuple) from xyz position."""
    return (
        int(np.floor(x / voxel_size)),
        int(np.floor(y / voxel_size)),
        int(np.floor(z / voxel_size))
    )


def merge_best_per_voxel(all_vertices: list, voxel_size: float, min_gaussians: int = 10) -> np.ndarray:
    """
    Merge splats by keeping only the best source PLY per voxel.
    
    For each voxel, the PLY with the highest gaussian count in that voxel wins,
    and only its gaussians are kept for that voxel. Voxels with fewer than
    min_gaussians total are skipped to help remove floaters.
    
    Args:
        all_vertices: List of vertex arrays, one per source PLY
        voxel_size: Size of voxels in meters
        min_gaussians: Skip voxels with fewer than this many gaussians
    
    Returns:
        Merged vertex array
    """
    num_plys = len(all_vertices)
    print(f"\nBuilding voxel map for {num_plys} splats with voxel_size={voxel_size}m...")
    
    # For each voxel: {voxel_hash: {ply_idx: [gaussian_indices]}}
    voxel_to_ply_gaussians: dict[tuple, dict[int, list]] = {}
    
    # Process each PLY
    for ply_idx, vert in enumerate(all_vertices):
        xyz_x = vert["x"].astype(np.float64)
        xyz_y = vert["y"].astype(np.float64)
        xyz_z = vert["z"].astype(np.float64)
        
        for i in range(len(vert)):
            voxel_hash = compute_voxel_hash(xyz_x[i], xyz_y[i], xyz_z[i], voxel_size)
            
            if voxel_hash not in voxel_to_ply_gaussians:
                voxel_to_ply_gaussians[voxel_hash] = {}
            
            if ply_idx not in voxel_to_ply_gaussians[voxel_hash]:
                voxel_to_ply_gaussians[voxel_hash][ply_idx] = []
            
            voxel_to_ply_gaussians[voxel_hash][ply_idx].append(i)
        
        print(f"  PLY {ply_idx}: {len(vert)} gaussians processed")
    
    print(f"Total voxels with gaussians: {len(voxel_to_ply_gaussians)}")
    
    # For each voxel, find the winning PLY (highest gaussian count)
    # and collect the gaussians to keep
    gaussians_to_keep: dict[int, list] = {i: [] for i in range(num_plys)}
    skipped_voxels = 0
    
    for voxel_hash, ply_gaussians in voxel_to_ply_gaussians.items():
        # Count total gaussians in this voxel across all PLYs
        total_in_voxel = sum(len(indices) for indices in ply_gaussians.values())
        if total_in_voxel < min_gaussians:
            skipped_voxels += 1
            continue
        
        # Find PLY with most gaussians in this voxel
        best_ply_idx = max(ply_gaussians.keys(), key=lambda idx: len(ply_gaussians[idx]))
        gaussians_to_keep[best_ply_idx].extend(ply_gaussians[best_ply_idx])
    
    if skipped_voxels > 0:
        print(f"Skipped {skipped_voxels} voxels with fewer than {min_gaussians} gaussians")
    
    # Build the merged result
    result_parts = []
    total_kept = 0
    for ply_idx, indices in gaussians_to_keep.items():
        if indices:
            kept = all_vertices[ply_idx][indices]
            result_parts.append(kept)
            total_kept += len(kept)
            print(f"  PLY {ply_idx}: keeping {len(kept)} gaussians")
    
    print(f"Total gaussians after voxel merging: {total_kept}")
    
    if not result_parts:
        return np.array([], dtype=all_vertices[0].dtype)
    
    return np.concatenate(result_parts)


def _merge_and_write(
    all_vertices: list,
    output_path: Path,
    args: argparse.Namespace,
    scan_ids: Optional[list] = None
) -> None:
    """Merge vertex arrays and write output."""
    if not all_vertices:
        print("Error: No valid splat files loaded.", file=sys.stderr)
        sys.exit(1)
    
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    
    # Save aligned scans before any merging/voxelization
    if args.save_aligned_scans and scan_ids is not None:
        aligned_dir = output_path.parent / "aligned_splats"
        aligned_dir.mkdir(parents=True, exist_ok=True)
        print(f"\nSaving aligned scans to: {aligned_dir}")
        
        for idx, (vert, scan_id) in enumerate(zip(all_vertices, scan_ids)):
            aligned_path = aligned_dir / f"splat_{idx}_{scan_id}.ply"
            aligned_el = PlyElement.describe(vert, "vertex")
            aligned_ply = PlyData([aligned_el], text=False)
            aligned_ply.write(str(aligned_path))
            print(f"  Saved: {aligned_path.name} ({len(vert)} gaussians)")
    
    total_input = sum(len(v) for v in all_vertices)
    print(f"\nTotal input gaussians: {total_input}")
    
    if args.best_per_voxel:
        combined = merge_best_per_voxel(all_vertices, args.voxel_size, args.min_gaussians_per_voxel)
    else:
        print(f"Concatenating {len(all_vertices)} splats...")
        combined = np.concatenate(all_vertices)
    
    print(f"Total gaussians in output: {len(combined)}")
    
    output_el = PlyElement.describe(combined, "vertex")
    output_ply = PlyData([output_el], text=False)
    
    print(f"Writing output to: {output_path}")
    output_ply.write(str(output_path))
    print("Done.")


def main() -> None:
    args = parse_args()
    
    if args.job_root is not None:
        combine_splats_from_job_root(args)
    else:
        combine_splats_explicit(args)


if __name__ == "__main__":
    main()
