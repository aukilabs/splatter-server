#!/usr/bin/env python3

import subprocess
import logging
import sys
import os
import time
import json
import math
import shutil
from pathlib import Path

logger = logging.getLogger("splatter-node")

def setup_logger(name=None, level="INFO"):
    """To setup as many loggers as you want"""

    logger = logging.getLogger(name)
    logger.setLevel(getattr(logging, level.upper()),)

    # Clear existing handlers (if reusing same name)
    if logger.hasHandlers():
        logger.handlers.clear()

    formatter = logging.Formatter(
        '%(levelname)s - %(message)s'
    )

    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.addFilter(lambda record: record.levelno <= logging.WARN)  # ≤ WARN
    console_handler.setFormatter(formatter)
    logger.addHandler(console_handler)

    console_err_handler = logging.StreamHandler(sys.stderr)
    console_err_handler.setLevel(logging.ERROR)
    console_err_handler.setFormatter(formatter)
    logger.addHandler(console_err_handler)

    return logger


def run_python_script(script_path: str, *args, python_exe: str = None) -> int:
    """
    Run a Python script and log its output in real-time.
    
    Args:
        script_path: Path to the Python script to run
        *args: Additional arguments to pass to the script
        python_exe: Optional specific Python interpreter (defaults to current)
    
    Returns:
        Exit code of the subprocess
    """
    script_path = Path(script_path).resolve()
    
    if not script_path.exists():
        logger.error(f"Script not found: {script_path}")
        return 1
    
    if not script_path.suffix == '.py':
        logger.warning(f"File doesn't have .py extension: {script_path}")
    
    python_exe = python_exe or sys.executable
    cmd = [python_exe, str(script_path)] + list(args)
    return run_cmd(cmd)

def run_script(exe: str, *args) -> int:
    """
    Run a Python script and log its output in real-time.
    
    Args:
        exec: executable
        *args: Additional arguments to pass to the script
    
    Returns:
        Exit code of the subprocess
    """
    cmd = [exe] + list(args)
    return run_cmd(cmd)

def run_cmd(cmd: list):
    """
    Run a cmd and log its output in real-time.
    
    Args:
        cmd: list of command to run
    
    Returns:
        Exit code of the subprocess
    """
    logger.info(f"Starting script: {cmd}")
    logger.info(f"Working directory: {os.getcwd()}")
    
    try:
        # Start the subprocess
        process = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,  # Merge stderr into stdout
            text=True,
            bufsize=1,
            universal_newlines=True
        )
        
        # Stream output line by line with nice prefix
        logger.info("-" * 60)
        
        for line in process.stdout:
            line = line.rstrip('\n')
            if line:
                # You can customize formatting based on content
                if line.startswith('ERROR') or 'error' in line.lower():
                    logger.error(f"    {line}")
                elif line.startswith('WARNING') or 'warning' in line.lower():
                    logger.warning(f"    {line}")
                else:
                    logger.info(f"    {line}")
        
        # Wait for completion
        process.wait()
        
        logger.info("-" * 60)
        if process.returncode == 0:
            logger.info(f"Script completed successfully (exit code: {process.returncode})")
        else:
            logger.error(f"Script failed with exit code: {process.returncode}")
            
        return process.returncode
        
    except Exception as e:
        logger.exception(f"Unexpected error while running script: {e}")
        return 1

def _normalize(vec):
    norm = math.sqrt(sum(v * v for v in vec))
    if norm <= 1e-8:
        return [0.0, 0.0, 0.0]
    return [v / norm for v in vec]

def _dot(a, b):
    return a[0] * b[0] + a[1] * b[1] + a[2] * b[2]

def _cross(a, b):
    return [
        a[1] * b[2] - a[2] * b[1],
        a[2] * b[0] - a[0] * b[2],
        a[0] * b[1] - a[1] * b[0],
    ]

def _view_matrix(eye, target, up_hint):
    lookat = [target[i] - eye[i] for i in range(3)]
    vec2 = _normalize(lookat)
    up = up_hint[:]
    if abs(_dot(vec2, up)) > 0.99:
        up = [0.0, 1.0, 0.0]
        if abs(_dot(vec2, up)) > 0.99:
            up = [1.0, 0.0, 0.0]
    vec0 = _normalize(_cross(up, vec2))
    vec1 = _normalize(_cross(vec2, vec0))
    mat = [
        [vec0[0], vec1[0], vec2[0], eye[0]],
        [vec0[1], vec1[1], vec2[1], eye[1]],
        [vec0[2], vec1[2], vec2[2], eye[2]],
        [0.0, 0.0, 0.0, 1.0],
    ]
    return mat

def _flatten_matrix(mat):
    return [mat[r][c] for r in range(4) for c in range(4)]

def _load_ply_bounds(ply_path: Path):
    try:
        from plyfile import PlyData
        import numpy as np
    except Exception as exc:
        logger.warning(f"preview render skipped: missing plyfile/numpy ({exc})")
        return None

    if not ply_path.exists():
        logger.warning(f"preview render skipped: missing ply file {ply_path}")
        return None

    try:
        ply = PlyData.read(str(ply_path))
        verts = ply["vertex"]
        xs = np.asarray(verts["x"], dtype=float)
        ys = np.asarray(verts["y"], dtype=float)
        zs = np.asarray(verts["z"], dtype=float)
        min_x, max_x = float(xs.min()), float(xs.max())
        min_y, max_y = float(ys.min()), float(ys.max())
        min_z, max_z = float(zs.min()), float(zs.max())
    except Exception as exc:
        logger.warning(f"preview render skipped: failed to read ply ({exc})")
        return None

    center = [
        (min_x + max_x) / 2.0,
        (min_y + max_y) / 2.0,
        (min_z + max_z) / 2.0,
    ]
    span = [
        max_x - min_x,
        max_y - min_y,
        max_z - min_z,
    ]
    size = max(span + [1.0])
    return center, size

def _write_camera_path(path: Path, camera_path_entries, render_width, render_height, seconds, fps):
    payload = {
        "camera_type": "perspective",
        "render_height": float(render_height),
        "render_width": float(render_width),
        "seconds": float(seconds),
        "fps": float(fps),
        "is_cycle": False,
        "smoothness_value": 0.0,
        "camera_path": camera_path_entries,
    }
    path.write_text(json.dumps(payload, indent=2))

def _render_previews(job_root_path: Path):
    preview_dir = job_root_path / "refined" / "splatter"
    config_path = preview_dir / "splatfacto" / "config.yml"
    if not config_path.exists():
        logger.warning(f"preview render skipped: missing config {config_path}")
        return

    if shutil.which("ns-render") is None:
        logger.warning("preview render skipped: ns-render not found")
        return
    ffmpeg_available = shutil.which("ffmpeg") is not None
    if not ffmpeg_available:
        logger.warning("preview video skipped: ffmpeg not found")

    ply_path = preview_dir / "splat.ply"
    bounds = _load_ply_bounds(ply_path)
    if bounds is None:
        return

    center, size = bounds
    render_width = 1280
    render_height = 720
    aspect = render_width / render_height
    fov = 60.0
    up = [0.0, 0.0, 1.0]

    camera_paths_dir = preview_dir / "camera_paths"
    camera_paths_dir.mkdir(parents=True, exist_ok=True)

    def build_entry(eye, target, render_time):
        mat = _view_matrix(eye, target, up)
        return {
            "camera_to_world": _flatten_matrix(mat),
            "fov": fov,
            "aspect": aspect,
            "render_time": float(render_time),
        }

    # Top-down preview image
    top_eye = [center[0], center[1], center[2] + size * 1.5]
    top_target = center
    top_entry = build_entry(top_eye, top_target, 0.0)
    top_json = camera_paths_dir / "preview_top.json"
    _write_camera_path(top_json, [top_entry], render_width, render_height, seconds=1.0, fps=1.0)
    top_out = preview_dir / "preview_top"
    exit_code = run_script(
        "ns-render",
        "camera-path",
        "--load-config",
        config_path,
        "--camera-path-filename",
        top_json,
        "--output-path",
        top_out,
        "--output-format",
        "images",
        "--image-format",
        "jpeg",
        "--jpeg-quality",
        "90",
    )
    if exit_code == 0:
        top_img_dir = top_out
        top_img = top_img_dir / "00000.jpg"
        if top_img.exists():
            shutil.copy2(top_img, preview_dir / "preview_top.jpg")
            shutil.rmtree(top_img_dir, ignore_errors=True)
    else:
        logger.warning("preview render failed: top-down image")

    # Angled preview image
    angle_eye = [
        center[0] + size * 1.2,
        center[1] + size * 1.2,
        center[2] + size * 0.8,
    ]
    angle_target = center
    angle_entry = build_entry(angle_eye, angle_target, 0.0)
    angle_json = camera_paths_dir / "preview_angle.json"
    _write_camera_path(angle_json, [angle_entry], render_width, render_height, seconds=1.0, fps=1.0)
    angle_out = preview_dir / "preview_angle"
    exit_code = run_script(
        "ns-render",
        "camera-path",
        "--load-config",
        config_path,
        "--camera-path-filename",
        angle_json,
        "--output-path",
        angle_out,
        "--output-format",
        "images",
        "--image-format",
        "jpeg",
        "--jpeg-quality",
        "90",
    )
    if exit_code == 0:
        angle_img_dir = angle_out
        angle_img = angle_img_dir / "00000.jpg"
        if angle_img.exists():
            shutil.copy2(angle_img, preview_dir / "preview_angle.jpg")
            shutil.rmtree(angle_img_dir, ignore_errors=True)
    else:
        logger.warning("preview render failed: angled image")

    # Preview video
    if ffmpeg_available:
        frames = 150
        seconds = 5.0
        orbit_entries = []
        radius = size * 1.5
        height = size * 0.6
        for i in range(frames):
            theta = 2.0 * math.pi * (i / frames)
            eye = [
                center[0] + radius * math.cos(theta),
                center[1] + radius * math.sin(theta),
                center[2] + height,
            ]
            render_time = (i / max(1, frames - 1)) * seconds
            orbit_entries.append(build_entry(eye, center, render_time))

        video_json = camera_paths_dir / "preview_video.json"
        _write_camera_path(video_json, orbit_entries, render_width, render_height, seconds=seconds, fps=30.0)
        preview_video = preview_dir / "preview.mp4"
        exit_code = run_script(
            "ns-render",
            "camera-path",
            "--load-config",
            config_path,
            "--camera-path-filename",
            video_json,
            "--output-path",
            preview_video,
            "--output-format",
            "video",
            "--image-format",
            "jpeg",
            "--jpeg-quality",
            "90",
        )
        if exit_code != 0:
            logger.warning("preview render failed: video")

# Example usage
if __name__ == "__main__":

    import argparse
    parser = argparse.ArgumentParser(description="Copy images")
    parser.add_argument("--domain_id", default="00000000-0000-0000-0000-000000000000", help="Input folder containing images")
    parser.add_argument("--job_id", default="00000000-0000-0000-0000-000000000000", help="Input folder containing images")
    parser.add_argument("--job_root_path", type=Path, required=True, help="Input folder containing images")
    parser.add_argument("--log_level", type=str, default="INFO", help="Path for output")
    args = parser.parse_args()

    setup_logger("splatter-node", args.log_level)

    logger.info("Preparing Dataset")   

    datasets = [p for p in (args.job_root_path / 'datasets').iterdir() if p.is_dir()]

    for dataset in datasets:
        exit_code = run_python_script("extract_mp4.py", 
                                      "--dataset_path", dataset,
                                      "--output_path", args.job_root_path / 'Frames')
        if exit_code != 0:
            logger.error(f"failed to extract mp4: {dataset}")
            sys.exit(exit_code)
   
    exit_code = run_script("ns-process-data", "images", 
                           "--data", args.job_root_path / "Frames", 
                           "--output-dir",  args.job_root_path / "refined/nerfstudio-data", 
                           "--skip-colmap", "--colmap-model-path", "../global/refined_sfm_combined/")
    if exit_code != 0:
        logger.error("failed to convert colmap data to nerfstudio data")
        sys.exit(exit_code)

    logger.info("Training Gaussian Splat")  
    exit_code = run_script("ns-train", "splatfacto", 
                           "--vis", "tensorboard",
                           "--logging.local-writer.max-log-size", "0",
                           "--logging.steps-per-log", "1000",
                           "--data", args.job_root_path / "refined/nerfstudio-data",
                           "--experiment-name", "splatter", # this is for naming folder
                           "--method-name", "splatfacto",   # this is for naming folder
                           "--timestamp", "/",              # this is for naming folder
                           "--output-dir", args.job_root_path / "refined",
                           "nerfstudio-data",
                           "--center-method", "none",
                           "--orientation-method", "none",
                           "--auto-scale-poses", "False")
    if exit_code != 0:
        logger.error("failed to train gaussian splat")
        sys.exit(exit_code)

    logger.info("Exporting Splat")
    exit_code = run_script("ns-export", "gaussian-splat", 
                           "--load-config", args.job_root_path / "refined/splatter/splatfacto/config.yml",
                           "--output-dir", args.job_root_path / "refined/splatter")
    if exit_code != 0:
        logger.error("failed to export gaussian splat")
        sys.exit(exit_code)

    logger.info("Rendering Previews")
    try:
        _render_previews(args.job_root_path)
    except Exception as exc:
        logger.warning(f"preview render failed: {exc}")

    logger.info("Rotating Splat")
    exit_code = run_python_script("rotate_ply.py", 
                                  "--input", args.job_root_path / "refined/splatter/splat.ply",
                                  "--output", args.job_root_path / "refined/splatter/splat_rot.ply")
    if exit_code != 0:
        logger.error("failed to transform splat")
        sys.exit(exit_code)

    logger.info("Converting Splat")
    exit_code = run_python_script("convert_ply2splat.py", 
                                "--input", args.job_root_path / "refined/splatter/splat_rot.ply",
                                "--output", args.job_root_path / "refined/splatter/splat_rot.splat")
    if exit_code != 0:
        logger.error("failed to convert splat .ply to .splat")
        sys.exit(exit_code)
    
    sys.exit(exit_code)
