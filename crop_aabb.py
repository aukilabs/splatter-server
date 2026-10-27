"""Compute training crop AABB for LichtFeld ``--cropbox`` (dataset coordinates)."""

from __future__ import annotations

import argparse
from pathlib import Path

import numpy as np
import pycolmap
from plyfile import PlyData

# LFS "Fit to Scene (Trimmed)": 1% tail trim per axis, then pad.
_TRIM_FRAC = 0.01
_PAD_M = 0.3


def trimmed_padded_aabb(
    points: np.ndarray,
    *,
    trim_frac: float = _TRIM_FRAC,
    pad_m: float = _PAD_M,
) -> tuple[np.ndarray, np.ndarray]:
    """Return (min_xyz, max_xyz) after per-axis percentile trim and symmetric pad."""
    if points.size == 0:
        raise ValueError("empty point set")
    pts = np.asarray(points, dtype=np.float64)
    if pts.ndim != 2 or pts.shape[1] != 3:
        raise ValueError(f"expected Nx3 points, got {pts.shape}")

    lo_pct = trim_frac * 100.0
    hi_pct = (1.0 - trim_frac) * 100.0
    lo = np.percentile(pts, lo_pct, axis=0)
    hi = np.percentile(pts, hi_pct, axis=0)
    lo = lo - pad_m
    hi = hi + pad_m
    return lo, hi


def format_cropbox(lo: np.ndarray, hi: np.ndarray) -> str:
    """LichtFeld CLI: ``x0,y0,z0,x1,y1,z1``."""
    vals = (*lo.tolist(), *hi.tolist())
    return ",".join(f"{v:.6f}" for v in vals)


def resolve_colmap_model_dir(colmap_dir: Path) -> Path:
    colmap_dir = colmap_dir.resolve()
    if (colmap_dir / "points3D.bin").exists() or (colmap_dir / "points3D.txt").exists():
        return colmap_dir
    nested = colmap_dir / "0"
    if (nested / "points3D.bin").exists() or (nested / "points3D.txt").exists():
        return nested
    raise FileNotFoundError(f"no COLMAP points3D under {colmap_dir}")


def load_points_from_ply(ply_path: Path) -> np.ndarray:
    ply = PlyData.read(str(ply_path))
    vert = ply["vertex"].data
    return np.column_stack(
        [
            vert["x"].astype(np.float64),
            vert["y"].astype(np.float64),
            vert["z"].astype(np.float64),
        ]
    )


def load_points_from_colmap(colmap_dir: Path) -> np.ndarray:
    model_dir = resolve_colmap_model_dir(colmap_dir)
    rec = pycolmap.Reconstruction(str(model_dir))
    if len(rec.points3D) == 0:
        raise ValueError(f"COLMAP model has no points3D: {model_dir}")
    return np.array([rec.points3D[pid].xyz for pid in rec.points3D], dtype=np.float64)


def compute_training_cropbox(
    *,
    init_ply: Path | None = None,
    colmap_dir: Path | None = None,
    trim_frac: float = _TRIM_FRAC,
    pad_m: float = _PAD_M,
) -> str:
    """Pick point source (dense/init PLY preferred) and return ``--cropbox`` value."""
    points: np.ndarray | None = None
    source = ""

    if init_ply is not None:
        init_ply = init_ply.resolve()
        if init_ply.is_file():
            points = load_points_from_ply(init_ply)
            source = f"ply:{init_ply}"

    if points is None and colmap_dir is not None:
        points = load_points_from_colmap(colmap_dir)
        source = f"colmap:{resolve_colmap_model_dir(colmap_dir)}"

    if points is None:
        raise FileNotFoundError("need init_ply or colmap_dir with points for crop AABB")

    lo, hi = trimmed_padded_aabb(points, trim_frac=trim_frac, pad_m=pad_m)
    crop = format_cropbox(lo, hi)
    print(
        f"[crop_aabb] source={source} n={len(points)} "
        f"trim={trim_frac} pad={pad_m}m cropbox={crop}",
        flush=True,
    )
    return crop


def main() -> None:
    ap = argparse.ArgumentParser(description="Compute LFS --cropbox from PLY or COLMAP points.")
    ap.add_argument("--init-ply", type=Path, default=None)
    ap.add_argument("--colmap-dir", type=Path, default=None)
    ap.add_argument("--trim-frac", type=float, default=_TRIM_FRAC)
    ap.add_argument("--pad-m", type=float, default=_PAD_M)
    args = ap.parse_args()
    print(compute_training_cropbox(
        init_ply=args.init_ply,
        colmap_dir=args.colmap_dir,
        trim_frac=args.trim_frac,
        pad_m=args.pad_m,
    ))


if __name__ == "__main__":
    main()
