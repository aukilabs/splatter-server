#!/usr/bin/env python3
"""
Render preview images from a trained Gaussian Splatting model.
Generates top-down and angled (¾-view) preview images.
"""

import argparse
import numpy as np
from pathlib import Path
from plyfile import PlyData
import subprocess
import sys
import json


def compute_splat_bounding_box(ply_path: Path):
    """Read the splat.ply file and compute centroid and bounding box."""
    plydata = PlyData.read(ply_path)
    vertices = plydata['vertex'].data
    
    positions = np.vstack((vertices['x'], vertices['y'], vertices['z'])).T
    
    centroid = positions.mean(axis=0)
    min_bound = positions.min(axis=0)
    max_bound = positions.max(axis=0)
    extent = max_bound - min_bound
    
    return centroid, min_bound, max_bound, extent


def generate_camera_json(camera_type: str, centroid: np.ndarray, extent: np.ndarray, output_path: Path):
    """
    Generate a camera JSON file for ns-render.
    
    camera_type: 'top_down' or 'angled'
    """
    # Add some margin to the bounding box
    margin = 1.2
    
    if camera_type == 'top_down':
        # Top-down: camera directly above centroid, looking down
        # Assuming Y is up in the splat coordinate system
        camera_height = centroid[1] + extent[1] * margin
        camera_position = [centroid[0], camera_height, centroid[2]]
        camera_target = centroid.tolist()
        camera_up = [0, 0, -1]  # Orient so that +Z in image is -Z in world (north)
        
        # Calculate FOV to fit the entire XZ extent
        fov = 90  # degrees, wide angle for top-down view
        
    elif camera_type == 'angled':
        # Angled ¾-view: camera at corner, elevated ~45 degrees
        # Place at one of the upper corners of the bounding box
        corner_x = centroid[0] + extent[0] * 0.5 * margin
        corner_z = centroid[2] + extent[2] * 0.5 * margin
        corner_y = centroid[1] + extent[1] * 0.8 * margin
        
        camera_position = [corner_x, corner_y, corner_z]
        camera_target = centroid.tolist()
        camera_up = [0, 1, 0]  # Y is up
        
        # Wide FOV for angled view
        fov = 60
        
    else:
        raise ValueError(f"Unknown camera type: {camera_type}")
    
    # Calculate basis vectors
    forward = np.array(camera_target) - np.array(camera_position)
    forward = forward / np.linalg.norm(forward)
    
    right = np.cross(forward, np.array(camera_up))
    if np.linalg.norm(right) < 1e-6:
        # Fallback if forward is parallel to up
        right = np.cross(forward, [1, 0, 0])
    right = right / np.linalg.norm(right)
    
    up = np.cross(right, forward)
    
    # Build the camera JSON
    camera_json = {
        "camera_type": "perspective",
        "fx": 1.0,
        "fy": 1.0,
        "cx": 0.5,
        "cy": 0.5,
        "width": 1920,
        "height": 1080,
        "position": camera_position,
        "rotation": [right.tolist(), up.tolist(), (-forward).tolist()],
        "fy_scale": 1.0
    }
    
    # Write camera JSON
    with open(output_path, 'w') as f:
        json.dump(camera_json, f, indent=2)
    
    return camera_json


def render_preview(config_path: Path, camera_json_path: Path, output_path: Path, output_name: str):
    """Render a preview image using ns-render."""
    cmd = [
        "ns-render",
        "--load-config", str(config_path),
        "--output-path", str(output_path),
        "--camera-path", str(camera_json_path),
        "--output-format", "png"
    ]
    
    print(f"Rendering {output_name}: {' '.join(cmd)}")
    
    result = subprocess.run(cmd, capture_output=True, text=True)
    
    if result.returncode != 0:
        print(f"Error rendering {output_name}:")
        print(result.stderr)
        return False
    
    print(f"Saved {output_name} to {output_path}")
    return True


def main():
    parser = argparse.ArgumentParser(description="Render preview images from Gaussian Splat")
    parser.add_argument("--splat_ply", type=Path, required=True, 
                        help="Path to splat.ply file")
    parser.add_argument("--config", type=Path, required=True,
                        help="Path to splatfacto config.yml")
    parser.add_argument("--output_dir", type=Path, required=True,
                        help="Output directory for preview images")
    parser.add_argument("--output_top", type=Path, default=None,
                        help="Output path for top-down preview")
    parser.add_argument("--output_angle", type=Path, default=None,
                        help="Output path for angled preview")
    
    args = parser.parse_args()
    
    # Create output directory
    args.output_dir.mkdir(parents=True, exist_ok=True)
    
    # Compute bounding box from splat.ply
    print(f"Computing bounding box from {args.splat_ply}")
    centroid, min_bound, max_bound, extent = compute_splat_bounding_box(args.splat_ply)
    print(f"Centroid: {centroid}")
    print(f"Extent: {extent}")
    
    # Default output paths
    output_top = args.output_top or args.output_dir / "preview_top.png"
    output_angle = args.output_angle or args.output_dir / "preview_angle.png"
    
    # Generate camera JSON for top-down view
    top_camera_json = args.output_dir / "camera_top.json"
    generate_camera_json('top_down', centroid, extent, top_camera_json)
    
    # Generate camera JSON for angled view
    angle_camera_json = args.output_dir / "camera_angle.json"
    generate_camera_json('angled', centroid, extent, angle_camera_json)
    
    # Render top-down preview
    print("\nRendering top-down preview...")
    success_top = render_preview(args.config, top_camera_json, output_top, "top-down")
    
    # Render angled preview
    print("\nRendering angled preview...")
    success_angle = render_preview(args.config, angle_camera_json, output_angle, "angled")
    
    # Summary
    print("\n" + "=" * 50)
    if success_top:
        print(f"✓ Top-down preview: {output_top}")
    else:
        print(f"✗ Top-down preview failed")
        
    if success_angle:
        print(f"✓ Angled preview: {output_angle}")
    else:
        print(f"✗ Angled preview failed")
    
    # Clean up camera JSON files
    top_camera_json.unlink(missing_ok=True)
    angle_camera_json.unlink(missing_ok=True)
    
    if not success_top and not success_angle:
        print("\nWarning: Both renders failed. Exiting with error.")
        sys.exit(1)
    
    print("\nPreview rendering complete!")


if __name__ == "__main__":
    main()
