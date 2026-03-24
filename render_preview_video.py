#!/usr/bin/env python3
"""
Render a preview video of the trained Gaussian Splat.

This script generates a 360° orbital video of the trained scene:
- Computes bounding box from splat.ply
- Generates an orbital camera path around the centroid
- Renders frames using nerfstudio's ns-render
- Encodes to MP4 using ffmpeg

Usage:
    python render_preview_video.py --splat_ply <path> --config_yml <path> --output_dir <path>
"""

import argparse
import json
import logging
import math
import subprocess
import sys
import tempfile
import shutil
from pathlib import Path
from typing import Tuple

import numpy as np

logger = logging.getLogger("render_preview_video")


def setup_logger(level: str = "INFO"):
    """Setup logging."""
    logging.basicConfig(
        level=getattr(logging, level.upper()),
        format="%(levelname)s - %(message)s",
        handlers=[logging.StreamHandler(sys.stdout)]
    )


def read_ply_bounding_box(ply_path: Path) -> Tuple[np.ndarray, np.ndarray]:
    """
    Read PLY file and compute bounding box.
    
    Returns:
        Tuple of (centroid, extent) where extent is the full size along each axis.
    """
    logger.info(f"Reading PLY file: {ply_path}")
    
    vertices = []
    with open(ply_path, 'rb') as f:
        # Read header
        line = f.readline().decode('ascii').strip()
        if line != 'ply':
            raise ValueError(f"Invalid PLY file: expected 'ply', got '{line}'")
        
        format_line = None
        num_vertices = 0
        properties = []
        
        while True:
            line = f.readline().decode('ascii').strip()
            if line == 'end_header':
                break
            
            parts = line.split()
            if parts[0] == 'format':
                format_line = line
            elif parts[0] == 'element' and parts[1] == 'vertex':
                num_vertices = int(parts[2])
            elif parts[0] == 'property':
                properties.append(parts[-1])
        
        logger.info(f"PLY format: {format_line}, vertices: {num_vertices}")
        
        # Find x, y, z property indices
        x_idx = properties.index('x') if 'x' in properties else 0
        y_idx = properties.index('y') if 'y' in properties else 1
        z_idx = properties.index('z') if 'z' in properties else 2
        
        # Read vertex data
        if 'binary' in format_line:
            import struct
            float_size = 4
            bytes_per_vertex = len(properties) * float_size
            
            for _ in range(num_vertices):
                data = f.read(bytes_per_vertex)
                values = struct.unpack(f'{len(properties)}f', data)
                vertices.append([values[x_idx], values[y_idx], values[z_idx]])
        else:
            for _ in range(num_vertices):
                line = f.readline().decode('ascii').strip()
                values = [float(v) for v in line.split()]
                vertices.append([values[x_idx], values[y_idx], values[z_idx]])
    
    vertices = np.array(vertices)
    
    min_coords = np.min(vertices, axis=0)
    max_coords = np.max(vertices, axis=0)
    centroid = (min_coords + max_coords) / 2
    extent = max_coords - min_coords
    
    logger.info(f"Bounding box: min={min_coords}, max={max_coords}")
    logger.info(f"Centroid: {centroid}")
    logger.info(f"Extent: {extent}")
    
    return centroid, extent


def generate_orbital_camera_path(
    centroid: np.ndarray,
    extent: np.ndarray,
    output_path: Path,
    num_frames: int = 150,
    fov: float = 60.0,
    elevation_degrees: float = 35.0,
    orbit_degrees: float = 360.0,
    resolution: Tuple[int, int] = (1920, 1080)
) -> dict:
    """
    Generate an orbital camera path JSON for ns-render.
    
    Args:
        centroid: Scene centroid
        extent: Scene extent (bounding box size)
        output_path: Path to write camera path JSON
        num_frames: Number of frames (determines video length at 30fps)
        fov: Field of view in degrees
        elevation_degrees: Camera elevation above horizontal (30-45° recommended)
        orbit_degrees: Total orbit angle (270-360°)
        resolution: Output resolution (width, height)
    
    Returns:
        Camera path dictionary
    """
    # Calculate camera distance to fit the scene
    max_extent = max(extent[0], extent[2])  # Use XZ plane extent
    diagonal = np.sqrt(extent[0]**2 + extent[2]**2)
    
    # Camera distance based on FOV and scene size
    # Distance = (extent / 2) / tan(fov/2)
    fov_rad = math.radians(fov)
    distance_factor = 1.5  # Add margin
    base_distance = (diagonal / 2) / math.tan(fov_rad / 2) * distance_factor
    
    # Account for elevation
    elevation_rad = math.radians(elevation_degrees)
    horizontal_distance = base_distance * math.cos(elevation_rad)
    vertical_offset = base_distance * math.sin(elevation_rad)
    
    logger.info(f"Camera distance: {base_distance:.2f}, horizontal: {horizontal_distance:.2f}")
    logger.info(f"Elevation: {elevation_degrees}°, vertical offset: {vertical_offset:.2f}")
    
    # Generate camera path
    keyframes = []
    
    for i in range(num_frames):
        # Interpolate angle from 0 to orbit_degrees
        t = i / max(num_frames - 1, 1)
        angle_deg = t * orbit_degrees
        angle_rad = math.radians(angle_deg)
        
        # Camera position on orbit
        cam_x = centroid[0] + horizontal_distance * math.cos(angle_rad)
        cam_z = centroid[2] + horizontal_distance * math.sin(angle_rad)
        cam_y = centroid[1] + vertical_offset
        
        # Look direction (camera looks at centroid)
        look_dir = centroid - np.array([cam_x, cam_y, cam_z])
        look_dir = look_dir / np.linalg.norm(look_dir)
        
        # Right vector (perpendicular to look direction, in horizontal plane)
        world_up = np.array([0.0, 1.0, 0.0])
        right = np.cross(look_dir, world_up)
        right = right / np.linalg.norm(right)
        
        # True up vector
        up = np.cross(right, look_dir)
        up = up / np.linalg.norm(up)
        
        # Construct camera-to-world matrix
        # Column 0: right, Column 1: up, Column 2: -look_dir, Column 3: position
        matrix = np.eye(4)
        matrix[0, :3] = right
        matrix[1, :3] = up
        matrix[2, :3] = -look_dir
        matrix[:3, 3] = [cam_x, cam_y, cam_z]
        
        keyframes.append({
            "matrix": matrix.tolist(),
            "fov": fov,
            "aspect": resolution[0] / resolution[1]
        })
    
    camera_path = {
        "keyframes": keyframes,
        "fov": fov,
        "aspect_ratio": resolution[0] / resolution[1],
        "seconds": num_frames / 30.0,  # Assuming 30 fps
    }
    
    with open(output_path, 'w') as f:
        json.dump(camera_path, f, indent=2)
    
    logger.info(f"Generated orbital camera path with {num_frames} frames: {output_path}")
    return camera_path


def render_video(
    config_path: Path,
    camera_path_path: Path,
    output_dir: Path,
    frames_dir: Path,
    output_name: str = "preview.mp4",
    resolution: Tuple[int, int] = (1920, 1080),
    fps: int = 30
) -> int:
    """
    Render a video using nerfstudio's ns-render and ffmpeg.
    
    Returns:
        Exit code (0 for success)
    """
    # Step 1: Render frames using ns-render
    frames_dir.mkdir(parents=True, exist_ok=True)
    
    render_cmd = [
        "ns-render",
        "gaussian-splat",
        "--load-config", str(config_path),
        "--camera-path-filename", str(camera_path_path),
        "--output-path", str(frames_dir),
    ]
    
    logger.info(f"Running ns-render: {' '.join(render_cmd)}")
    
    render_result = subprocess.run(render_cmd, capture_output=True, text=True)
    
    if render_result.returncode != 0:
        logger.error(f"ns-render failed: {render_result.stderr}")
        return render_result.returncode
    
    logger.info(f"Rendered frames to: {frames_dir}")
    
    # Step 2: Encode frames to MP4 using ffmpeg
    output_path = output_dir / output_name
    
    ffmpeg_cmd = [
        "ffmpeg",
        "-y",  # Overwrite output
        "-framerate", str(fps),
        "-i", str(frames_dir / "%05d.png"),
        "-c:v", "libx264",
        "-pix_fmt", "yuv420p",
        "-crf", "23",
        "-movflags", "+faststart",
        str(output_path)
    ]
    
    logger.info(f"Running ffmpeg: {' '.join(ffmpeg_cmd)}")
    
    ffmpeg_result = subprocess.run(ffmpeg_cmd, capture_output=True, text=True)
    
    if ffmpeg_result.returncode != 0:
        logger.error(f"ffmpeg failed: {ffmpeg_result.stderr}")
        return ffmpeg_result.returncode
    
    logger.info(f"Encoded video: {output_path}")
    
    return 0


def main():
    parser = argparse.ArgumentParser(description="Render preview video of Gaussian Splat")
    parser.add_argument("--splat_ply", type=Path, required=True, help="Path to splat.ply file")
    parser.add_argument("--config_yml", type=Path, required=True, help="Path to nerfstudio config.yml")
    parser.add_argument("--output_dir", type=Path, required=True, help="Output directory for preview video")
    parser.add_argument("--num_frames", type=int, default=150, help="Number of frames (default: 150 = 5 sec at 30fps)")
    parser.add_argument("--elevation", type=float, default=35.0, help="Camera elevation in degrees (default: 35)")
    parser.add_argument("--orbit_degrees", type=float, default=360.0, help="Orbit angle in degrees (default: 360)")
    parser.add_argument("--resolution", type=str, default="1920x1080", help="Resolution WxH (default: 1920x1080)")
    parser.add_argument("--log_level", type=str, default="INFO", help="Log level")
    
    args = parser.parse_args()
    setup_logger(args.log_level)
    
    # Parse resolution
    try:
        width, height = map(int, args.resolution.lower().split('x'))
        resolution = (width, height)
    except:
        logger.warning(f"Invalid resolution '{args.resolution}', using 1920x1080")
        resolution = (1920, 1080)
    
    # Ensure output directory exists
    args.output_dir.mkdir(parents=True, exist_ok=True)
    
    try:
        # Step 1: Compute bounding box from PLY
        centroid, extent = read_ply_bounding_box(args.splat_ply)
        
        # Step 2: Render video
        with tempfile.TemporaryDirectory() as tmpdir:
            tmpdir = Path(tmpdir)
            camera_path_file = tmpdir / "camera_path.json"
            frames_dir = tmpdir / "frames"
            
            # Generate orbital camera path
            generate_orbital_camera_path(
                centroid, extent, camera_path_file,
                num_frames=args.num_frames,
                elevation_degrees=args.elevation,
                orbit_degrees=args.orbit_degrees,
                resolution=resolution
            )
            
            # Render and encode video
            exit_code = render_video(
                args.config_yml,
                camera_path_file,
                args.output_dir,
                frames_dir,
                output_name="preview.mp4",
                resolution=resolution
            )
            
            if exit_code != 0:
                logger.warning("Failed to render preview video")
                return exit_code
        
        # Verify output
        output_video = args.output_dir / "preview.mp4"
        if output_video.exists():
            size_mb = output_video.stat().st_size / (1024 * 1024)
            logger.info(f"Preview video saved: {output_video} ({size_mb:.1f} MB)")
            return 0
        else:
            logger.error("Preview video was not created")
            return 1
            
    except Exception as e:
        logger.exception(f"Failed to render preview video: {e}")
        return 1


if __name__ == "__main__":
    sys.exit(main())