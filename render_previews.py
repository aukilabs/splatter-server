#!/usr/bin/env python3
"""
Render two preview images (top-down and angled 3/4 view) from a trained
Gaussian Splat model.

This script reads the exported splat.ply to compute the scene centroid and
bounding box, constructs two camera poses, writes a nerfstudio-compatible
camera-path JSON file for each, and invokes ``ns-render`` to produce the
preview images.

Usage:
    python3 render_previews.py \
        --ply_path  <job_root>/refined/splatter/splat.ply \
        --config    <job_root>/refined/splatter/splatfacto/config.yml \
        --output_dir <job_root>/refined/splatter
"""

import argparse
import json
import math
import sys
import subprocess
import tempfile
from pathlib import Path

import numpy as np
from plyfile import PlyData


# ---------------------------------------------------------------------------
# Geometry helpers
# ---------------------------------------------------------------------------

def load_ply_positions(ply_path: str) -> np.ndarray:
    """Return an (N, 3) float64 array of vertex positions from a PLY file."""
    ply = PlyData.read(ply_path)
    v = ply["vertex"].data
    return np.column_stack((v["x"], v["y"], v["z"]))


def compute_bbox(positions: np.ndarray):
    """Return (centroid, extent) where extent = max - min per axis."""
    mins = positions.min(axis=0)
    maxs = positions.max(axis=0)
    centroid = (mins + maxs) / 2.0
    extent = maxs - mins
    return centroid, extent


# ---------------------------------------------------------------------------
# Camera-pose construction (4x4 world-to-camera matrices)
# ---------------------------------------------------------------------------

def look_at(eye: np.ndarray, target: np.ndarray, up: np.ndarray = None):
    """
    Build a 4x4 camera-to-world matrix (OpenGL convention: -Z forward).
    """
    if up is None:
        up = np.array([0.0, 0.0, 1.0])

    forward = target - eye
    forward /= np.linalg.norm(forward)
    right = np.cross(forward, up)
    norm = np.linalg.norm(right)
    if norm < 1e-6:
        # forward is parallel to up -- pick an arbitrary perpendicular
        up = np.array([1.0, 0.0, 0.0])
        right = np.cross(forward, up)
        norm = np.linalg.norm(right)
    right /= norm
    new_up = np.cross(right, forward)

    c2w = np.eye(4)
    c2w[0, :3] = right
    c2w[1, :3] = new_up
    c2w[2, :3] = -forward  # OpenGL: camera looks along -Z
    c2w[:3, 3] = eye
    return c2w


def top_down_camera(centroid, extent):
    """Camera directly above the centroid looking straight down."""
    # Place camera well above the scene
    height = max(extent[0], extent[1], extent[2]) * 2.0 + 1.0
    eye = centroid + np.array([0.0, 0.0, height])
    target = centroid.copy()
    # Up direction in image plane -- pick +Y world
    up = np.array([0.0, 1.0, 0.0])
    return look_at(eye, target, up)


def angled_camera(centroid, extent):
    """Elevated corner camera (~45 deg above horizontal) looking at centroid."""
    diag = np.linalg.norm(extent) + 1.0
    offset = diag * 0.8
    eye = centroid + np.array([offset, offset, offset])
    up = np.array([0.0, 0.0, 1.0])
    return look_at(eye, centroid, up)


# ---------------------------------------------------------------------------
# nerfstudio camera-path JSON helpers
# ---------------------------------------------------------------------------

def c2w_to_camera_path_entry(c2w: np.ndarray, fov: float = 75.0, aspect: float = 1.0):
    """Build a single entry for a nerfstudio camera_path JSON."""
    return {
        "camera_to_world": c2w.flatten().tolist(),
        "fov": fov,
        "aspect": aspect,
    }


def write_camera_path_json(path: str, c2w: np.ndarray,
                           image_width: int = 1024, image_height: int = 1024):
    """Write a single-frame camera-path JSON understood by ``ns-render``."""
    fov = 75.0
    aspect = image_width / image_height
    payload = {
        "camera_type": "perspective",
        "render_height": image_height,
        "render_width": image_width,
        "camera_path": [c2w_to_camera_path_entry(c2w, fov=fov, aspect=aspect)],
        "fps": 1,
        "seconds": 1,
        "is_cycle": False,
        "smoothness_value": 0.0,
    }
    with open(path, "w") as f:
        json.dump(payload, f, indent=2)


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------

def render_preview(config_path: str, camera_json: str, output_path: str) -> int:
    """
    Invoke ``ns-render camera-path`` and move the rendered frame to
    *output_path*.  Returns the process exit code.
    """
    with tempfile.TemporaryDirectory() as tmpdir:
        cmd = [
            "ns-render", "camera-path",
            "--load-config", str(config_path),
            "--camera-path-filename", str(camera_json),
            "--output-path", str(Path(tmpdir) / "render.mp4"),
            "--image-format", "jpeg",
            "--jpeg-quality", "90",
            "--output-format", "images",
        ]
        result = subprocess.run(cmd, capture_output=True, text=True)
        if result.returncode != 0:
            print(f"ns-render failed (exit {result.returncode}):", file=sys.stderr)
            print(result.stderr, file=sys.stderr)
            return result.returncode

        # ns-render with --output-format images writes frames into a directory.
        # The rendered frame(s) land inside <tmpdir>/ with names like 00000.jpeg
        rendered_dir = Path(tmpdir)
        candidates = sorted(rendered_dir.rglob("*.jpeg")) + sorted(rendered_dir.rglob("*.jpg")) + sorted(rendered_dir.rglob("*.png"))
        if not candidates:
            print("No rendered frame found in output directory", file=sys.stderr)
            return 1

        # Copy the first (and only) frame to the desired output path.
        import shutil
        shutil.copy2(str(candidates[0]), str(output_path))
        print(f"Preview saved: {output_path}")
    return 0


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="Render top-down and angled preview images of a trained Gaussian Splat"
    )
    parser.add_argument("--ply_path", type=Path, required=True,
                        help="Path to the exported splat.ply")
    parser.add_argument("--config", type=Path, required=True,
                        help="Path to the splatfacto config.yml")
    parser.add_argument("--output_dir", type=Path, required=True,
                        help="Directory where preview_top.jpg and preview_angle.jpg will be written")
    args = parser.parse_args()

    # --- Compute scene bounds from the PLY ---------------------------------
    print(f"Reading PLY: {args.ply_path}")
    positions = load_ply_positions(str(args.ply_path))
    centroid, extent = compute_bbox(positions)
    print(f"  Centroid : {centroid}")
    print(f"  Extent   : {extent}")
    print(f"  N points : {len(positions)}")

    args.output_dir.mkdir(parents=True, exist_ok=True)

    # --- Top-down preview --------------------------------------------------
    c2w_top = top_down_camera(centroid, extent)
    top_json = str(args.output_dir / "_cam_top.json")
    write_camera_path_json(top_json, c2w_top)
    top_out = str(args.output_dir / "preview_top.jpg")
    print("Rendering top-down preview ...")
    rc = render_preview(str(args.config), top_json, top_out)
    if rc != 0:
        print(f"WARNING: top-down render failed (exit {rc})", file=sys.stderr)

    # --- Angled preview ----------------------------------------------------
    c2w_angle = angled_camera(centroid, extent)
    angle_json = str(args.output_dir / "_cam_angle.json")
    write_camera_path_json(angle_json, c2w_angle)
    angle_out = str(args.output_dir / "preview_angle.jpg")
    print("Rendering angled preview ...")
    rc2 = render_preview(str(args.config), angle_json, angle_out)
    if rc2 != 0:
        print(f"WARNING: angled render failed (exit {rc2})", file=sys.stderr)

    # Clean up temporary camera JSON files
    for p in [top_json, angle_json]:
        try:
            Path(p).unlink(missing_ok=True)
        except Exception:
            pass

    # Exit successfully even if rendering failed (best-effort).
    sys.exit(0)


if __name__ == "__main__":
    main()
