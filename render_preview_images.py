#!/usr/bin/env python3
"""
Render preview images of the trained Gaussian Splat.

This script generates two preview images:
1. Top-down view: camera directly above centroid, looking straight down
2. Angled view: camera at elevated corner (~45°), looking at centroid

Usage:
    python render_preview_images.py --splat_ply <path> --config_yml <path> --output_dir <path>
"""

import argparse
import json
import logging
import math
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Tuple

import numpy as np

logger = logging.getLogger("render_preview_images")


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
    
    # Read PLY file header and vertices
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
                properties.append(parts[-1])  # property name
        
        logger.info(f"PLY format: {format_line}, vertices: {num_vertices}")
        
        # Find x, y, z property indices
        x_idx = properties.index('x') if 'x' in properties else 0
        y_idx = properties.index('y') if 'y' in properties else 1
        z_idx = properties.index('z') if 'z' in properties else 2
        
        # Read vertex data
        if 'binary' in format_line:
            # Binary format
            import struct
            # Determine bytes per vertex based on properties
            float_size = 4  # assume float32
            bytes_per_vertex = len(properties) * float_size
            
            for _ in range(num_vertices):
                data = f.read(bytes_per_vertex)
                values = struct.unpack(f'{len(properties)}f', data)
                vertices.append([values[x_idx], values[y_idx], values[z_idx]])
        else:
            # ASCII format
            for _ in range(num_vertices):
                line = f.readline().decode('ascii').strip()
                values = [float(v) for v in line.split()]
                vertices.append([values[x_idx], values[y_idx], values[z_idx]])
    
    vertices = np.array(vertices)
    
    # Compute bounding box
    min_coords = np.min(vertices, axis=0)
    max_coords = np.max(vertices, axis=0)
    centroid = (min_coords + max_coords) / 2
    extent = max_coords - min_coords
    
    logger.info(f"Bounding box: min={min_coords}, max={max_coords}")
    logger.info(f"Centroid: {centroid}")
    logger.info(f"Extent: {extent}")
    
    return centroid, extent


def generate_camera_path_topdown(
    centroid: np.ndarray,
    extent: np.ndarray,
    output_path: Path,
    fov: float = 60.0,
    num_frames: int = 1
) -> dict:
    """
    Generate camera path JSON for top-down view.
    
    Camera placed directly above centroid, looking straight down.
    """
    # Height sufficient to frame the full XZ extent
    max_horizontal = max(extent[0], extent[2])
    height = max_horizontal * 1.5  # Add some margin
    
    camera_path = {
        "keyframes": [],
        "fov": fov,
        "aspect_ratio": 1.0,  # Square output
    }
    
    # Single frame for top-down
    camera_path["keyframes"].append({
        "matrix": [
            [1.0, 0.0, 0.0, 0.0],
            [0.0, 0.0, 1.0, 0.0],  # Y and Z swapped for looking down
            [0.0, -1.0, 0.0, 0.0],
            [centroid[0], centroid[1] + height, centroid[2], 1.0]
        ],
        "fov": fov,
        "aspect": 1.0
    })
    
    with open(output_path, 'w') as f:
        json.dump(camera_path, f, indent=2)
    
    logger.info(f"Generated top-down camera path: {output_path}")
    return camera_path


def generate_camera_path_angle(
    centroid: np.ndarray,
    extent: np.ndarray,
    output_path: Path,
    fov: float = 60.0,
    num_frames: int = 1
) -> dict:
    """
    Generate camera path JSON for angled 3/4 view.
    
    Camera placed at elevated corner (~45° above horizontal), looking at centroid.
    """
    # Diagonal distance for corner position
    max_extent = max(extent)
    diagonal = np.sqrt(extent[0]**2 + extent[2]**2)
    
    # Camera position at corner with elevation
    elevation_angle = math.radians(45)  # 45 degrees above horizontal
    distance = diagonal * 1.5  # Add margin
    
    # Position at one corner
    camera_x = centroid[0] + distance * math.cos(elevation_angle) / math.sqrt(2)
    camera_y = centroid[1] + distance * math.sin(elevation_angle)
    camera_z = centroid[2] + distance * math.cos(elevation_angle) / math.sqrt(2)
    
    camera_path = {
        "keyframes": [],
        "fov": fov,
        "aspect_ratio": 1.0,
    }
    
    camera_path["keyframes"].append({
        "matrix": [
            [0.707, -0.408, 0.577, 0.0],  # Approximate rotation matrix
            [0.0, 0.816, 0.577, 0.0],
            [-0.707, -0.408, 0.577, 0.0],
            [camera_x, camera_y, camera_z, 1.0]
        ],
        "fov": fov,
        "aspect": 1.0
    })
    
    with open(output_path, 'w') as f:
        json.dump(camera_path, f, indent=2)
    
    logger.info(f"Generated angled camera path: {output_path}")
    return camera_path


def render_image(
    config_path: Path,
    camera_path_path: Path,
    output_dir: Path,
    output_name: str
) -> int:
    """
    Render an image using nerfstudio's ns-render.
    
    Returns:
        Exit code (0 for success)
    """
    output_path = output_dir / output_name
    
    cmd = [
        "ns-render",
        "gaussian-splat",
        "--load-config", str(config_path),
        "--camera-path-filename", str(camera_path_path),
        "--output-path", str(output_path),
    ]
    
    logger.info(f"Running ns-render: {' '.join(cmd)}")
    
    result = subprocess.run(cmd, capture_output=True, text=True)
    
    if result.returncode != 0:
        logger.error(f"ns-render failed: {result.stderr}")
    else:
        logger.info(f"Rendered image: {output_path}")
    
    return result.returncode


def main():
    parser = argparse.ArgumentParser(description="Render preview images of Gaussian Splat")
    parser.add_argument("--splat_ply", type=Path, required=True, help="Path to splat.ply file")
    parser.add_argument("--config_yml", type=Path, required=True, help="Path to nerfstudio config.yml")
    parser.add_argument("--output_dir", type=Path, required=True, help="Output directory for preview images")
    parser.add_argument("--log_level", type=str, default="INFO", help="Log level")
    
    args = parser.parse_args()
    setup_logger(args.log_level)
    
    # Ensure output directory exists
    args.output_dir.mkdir(parents=True, exist_ok=True)
    
    try:
        # Step 1: Compute bounding box from PLY
        centroid, extent = read_ply_bounding_box(args.splat_ply)
        
        # Step 2: Generate camera paths
        with tempfile.TemporaryDirectory() as tmpdir:
            tmpdir = Path(tmpdir)
            
            topdown_camera_path = tmpdir / "camera_topdown.json"
            angle_camera_path = tmpdir / "camera_angle.json"
            
            generate_camera_path_topdown(centroid, extent, topdown_camera_path)
            generate_camera_path_angle(centroid, extent, angle_camera_path)
            
            # Step 3: Render both views
            # Top-down preview
            exit_code = render_image(
                args.config_yml,
                topdown_camera_path,
                args.output_dir,
                "preview_top.png"
            )
            
            if exit_code != 0:
                logger.warning("Failed to render top-down preview, continuing...")
            
            # Angled preview
            exit_code = render_image(
                args.config_yml,
                angle_camera_path,
                args.output_dir,
                "preview_angle.png"
            )
            
            if exit_code != 0:
                logger.warning("Failed to render angled preview, continuing...")
        
        # Check if at least one image was rendered
        top_exists = (args.output_dir / "preview_top.png").exists()
        angle_exists = (args.output_dir / "preview_angle.png").exists()
        
        if top_exists:
            logger.info(f"Top-down preview saved: {args.output_dir / 'preview_top.png'}")
        if angle_exists:
            logger.info(f"Angled preview saved: {args.output_dir / 'preview_angle.png'}")
        
        if not top_exists and not angle_exists:
            logger.error("No preview images were rendered successfully")
            return 1
        
        return 0
        
    except Exception as e:
        logger.exception(f"Failed to render preview images: {e}")
        return 1


if __name__ == "__main__":
    sys.exit(main())