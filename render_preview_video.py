#!/usr/bin/env python3
"""
Render a short preview video (5-10 seconds, 30 fps) showing an orbital camera
path around a trained Gaussian Splat model.

This script reads the exported splat.ply to compute the scene centroid and
bounding box, generates a smooth orbital camera-path JSON, invokes
``ns-render camera-path`` to produce individual frames, and encodes them into
an H.264 MP4 via ffmpeg.

Usage:
    python3 render_preview_video.py \
        --ply_path  <job_root>/refined/splatter/splat.ply \
        --config    <job_root>/refined/splatter/splatfacto/config.yml \
        --output_dir <job_root>/refined/splatter
"""

import argparse
import json
import math
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import numpy as np
from plyfile import PlyData


# ---------------------------------------------------------------------------
# Geometry helpers (reused from render_previews.py)
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
# Camera-pose construction
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


def generate_orbital_path(centroid, extent, num_frames=210,
                          arc_degrees=270.0, elevation_deg=45.0):
    """
    Generate a list of 4x4 camera-to-world matrices tracing an orbital arc
    around *centroid* at the given elevation angle.

    Parameters
    ----------
    centroid : np.ndarray (3,)
        Point the camera always looks at.
    extent : np.ndarray (3,)
        Bounding-box extent used to compute orbit radius.
    num_frames : int
        Total frames to generate (default 210 = 7 s at 30 fps).
    arc_degrees : float
        How many degrees of orbit to cover (default 270).
    elevation_deg : float
        Elevation above the horizontal plane in degrees (default 45).

    Returns
    -------
    list of np.ndarray
        Each element is a (4, 4) camera-to-world matrix.
    """
    diag = np.linalg.norm(extent) + 1.0
    radius = diag * 1.0  # comfortable framing distance

    elev_rad = math.radians(elevation_deg)
    arc_rad = math.radians(arc_degrees)

    poses = []
    for i in range(num_frames):
        t = i / max(num_frames - 1, 1)
        theta = t * arc_rad  # azimuth angle

        # Camera position on orbital arc
        x = centroid[0] + radius * math.cos(elev_rad) * math.cos(theta)
        y = centroid[1] + radius * math.cos(elev_rad) * math.sin(theta)
        z = centroid[2] + radius * math.sin(elev_rad)
        eye = np.array([x, y, z])

        c2w = look_at(eye, centroid)
        poses.append(c2w)

    return poses


# ---------------------------------------------------------------------------
# nerfstudio camera-path JSON
# ---------------------------------------------------------------------------

def c2w_to_camera_path_entry(c2w: np.ndarray, fov: float = 75.0,
                              aspect: float = 16.0 / 9.0):
    """Build a single entry for a nerfstudio camera_path JSON."""
    return {
        "camera_to_world": c2w.flatten().tolist(),
        "fov": fov,
        "aspect": aspect,
    }


def write_camera_path_json(path: str, poses: list,
                            image_width: int = 1280,
                            image_height: int = 720,
                            fps: int = 30):
    """Write a multi-frame camera-path JSON understood by ``ns-render``."""
    fov = 75.0
    aspect = image_width / image_height
    seconds = len(poses) / fps

    payload = {
        "camera_type": "perspective",
        "render_height": image_height,
        "render_width": image_width,
        "camera_path": [
            c2w_to_camera_path_entry(c2w, fov=fov, aspect=aspect)
            for c2w in poses
        ],
        "fps": fps,
        "seconds": seconds,
        "is_cycle": False,
        "smoothness_value": 0.0,
    }
    with open(path, "w") as f:
        json.dump(payload, f, indent=2)


# ---------------------------------------------------------------------------
# Rendering and encoding
# ---------------------------------------------------------------------------

def render_frames(config_path: str, camera_json: str,
                  frames_dir: str) -> int:
    """
    Invoke ``ns-render camera-path`` to render frames into *frames_dir*.
    Returns the process exit code.
    """
    cmd = [
        "ns-render", "camera-path",
        "--load-config", str(config_path),
        "--camera-path-filename", str(camera_json),
        "--output-path", str(Path(frames_dir) / "render.mp4"),
        "--image-format", "png",
        "--output-format", "images",
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        print(f"ns-render failed (exit {result.returncode}):", file=sys.stderr)
        print(result.stderr, file=sys.stderr)
    return result.returncode


def encode_video(frames_dir: str, output_path: str, fps: int = 30) -> int:
    """
    Encode rendered PNG frames into an H.264 MP4 using ffmpeg.
    Returns the process exit code.
    """
    # Find frames — ns-render names them like 00000.png, 00001.png, ...
    frames = sorted(Path(frames_dir).rglob("*.png"))
    if not frames:
        print("No rendered frames found for video encoding", file=sys.stderr)
        return 1

    # Determine the pattern (frames may be nested in a subdirectory)
    first_frame = frames[0]
    pattern_dir = str(first_frame.parent)
    # Detect naming: %05d.png
    pattern = str(first_frame.parent / "%05d.png")

    cmd = [
        "ffmpeg", "-y",
        "-framerate", str(fps),
        "-i", pattern,
        "-c:v", "libx264",
        "-pix_fmt", "yuv420p",
        "-crf", "23",
        "-movflags", "+faststart",
        str(output_path),
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        print(f"ffmpeg failed (exit {result.returncode}):", file=sys.stderr)
        print(result.stderr, file=sys.stderr)
    return result.returncode


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="Render a preview video of a trained Gaussian Splat"
    )
    parser.add_argument("--ply_path", type=Path, required=True,
                        help="Path to the exported splat.ply")
    parser.add_argument("--config", type=Path, required=True,
                        help="Path to the splatfacto config.yml")
    parser.add_argument("--output_dir", type=Path, required=True,
                        help="Directory where preview.mp4 will be written")
    parser.add_argument("--num_frames", type=int, default=210,
                        help="Number of frames to render (default: 210 = 7s at 30fps)")
    parser.add_argument("--fps", type=int, default=30,
                        help="Frames per second (default: 30)")
    parser.add_argument("--arc_degrees", type=float, default=270.0,
                        help="Orbital arc in degrees (default: 270)")
    parser.add_argument("--elevation_deg", type=float, default=45.0,
                        help="Camera elevation angle in degrees (default: 45)")
    args = parser.parse_args()

    # --- Compute scene bounds from the PLY ---------------------------------
    print(f"Reading PLY: {args.ply_path}")
    positions = load_ply_positions(str(args.ply_path))
    centroid, extent = compute_bbox(positions)
    print(f"  Centroid : {centroid}")
    print(f"  Extent   : {extent}")
    print(f"  N points : {len(positions)}")

    args.output_dir.mkdir(parents=True, exist_ok=True)

    # --- Generate orbital camera path --------------------------------------
    print(f"Generating orbital camera path ({args.num_frames} frames, "
          f"{args.arc_degrees} deg arc, {args.elevation_deg} deg elevation)")
    poses = generate_orbital_path(
        centroid, extent,
        num_frames=args.num_frames,
        arc_degrees=args.arc_degrees,
        elevation_deg=args.elevation_deg,
    )

    cam_json_path = str(args.output_dir / "_cam_video.json")
    write_camera_path_json(cam_json_path, poses, fps=args.fps)

    # --- Render frames -----------------------------------------------------
    with tempfile.TemporaryDirectory() as tmpdir:
        print("Rendering frames with ns-render ...")
        rc = render_frames(str(args.config), cam_json_path, tmpdir)
        if rc != 0:
            print(f"WARNING: ns-render failed (exit {rc}); skipping video",
                  file=sys.stderr)
            _cleanup(cam_json_path)
            sys.exit(0)

        # --- Encode to MP4 ------------------------------------------------
        output_mp4 = str(args.output_dir / "preview.mp4")
        print("Encoding video with ffmpeg ...")
        rc2 = encode_video(tmpdir, output_mp4, fps=args.fps)
        if rc2 != 0:
            print(f"WARNING: ffmpeg encoding failed (exit {rc2})",
                  file=sys.stderr)
            _cleanup(cam_json_path)
            sys.exit(0)

        print(f"Preview video saved: {output_mp4}")

    # --- Cleanup -----------------------------------------------------------
    _cleanup(cam_json_path)

    # Exit successfully (best-effort).
    sys.exit(0)


def _cleanup(cam_json_path: str):
    """Remove temporary camera-path JSON."""
    try:
        Path(cam_json_path).unlink(missing_ok=True)
    except Exception:
        pass


if __name__ == "__main__":
    main()
