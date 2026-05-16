#!/usr/bin/env python3
"""
Generate preview images of trained Gaussian Splat.

This script renders two preview images from a trained splat:
1. Top-down view - camera directly above centroid looking down
2. Angled view - camera at elevated corner (~45°) looking at centroid
"""

import numpy as np
import subprocess
import json
import tempfile
import os
import sys
from pathlib import Path
from plyfile import PlyData

def load_ply_points(ply_path):
    """Load point cloud data from PLY file and compute bounding box."""
    plydata = PlyData.read(ply_path)
    vertices = plydata['vertex'].data
    
    # Extract positions
    positions = np.vstack((vertices['x'], vertices['y'], vertices['z'])).T
    
    # Compute bounding box and centroid
    min_bounds = np.min(positions, axis=0)
    max_bounds = np.max(positions, axis=0)
    centroid = (min_bounds + max_bounds) / 2
    extent = max_bounds - min_bounds
    
    return {
        'positions': positions,
        'centroid': centroid,
        'min_bounds': min_bounds,
        'max_bounds': max_bounds,
        'extent': extent
    }

def create_camera_json(camera_poses, output_path):
    """Create nerfstudio camera JSON file."""
    cameras = {
        "camera_type": 0,  # perspective
        "image_height": 1080,
        "image_width": 1080,
        "fl_x": 1000.0,
        "fl_y": 1000.0,
        "cx": 540.0,
        "cy": 540.0,
        "w": 1080,
        "h": 1080,
        "frames": []
    }
    
    for i, pose in enumerate(camera_poses):
        frame = {
            "file_path": f"frame_{i:05d}",
            "transform_matrix": pose.tolist()
        }
        cameras["frames"].append(frame)
    
    with open(output_path, 'w') as f:
        json.dump(cameras, f, indent=2)

def create_top_down_camera(centroid, extent):
    """Create top-down camera looking straight down at centroid."""
    # Calculate camera distance to frame the entire scene
    max_extent = np.max(extent[:2])  # XZ plane
    distance = max_extent * 0.8  # Add some margin
    
    # Position camera directly above centroid
    camera_pos = centroid + np.array([0, distance, 0])
    
    # Look direction (down)
    look_dir = centroid - camera_pos
    look_dir = look_dir / np.linalg.norm(look_dir)
    
    # Up vector (Z-axis)
    up = np.array([0, 0, 1])
    
    # Right vector
    right = np.cross(look_dir, up)
    right = right / np.linalg.norm(right)
    
    # Recalculate up to ensure orthogonality
    up = np.cross(right, look_dir)
    
    # Create 4x4 transformation matrix
    transform = np.eye(4)
    transform[:3, 0] = right
    transform[:3, 1] = up
    transform[:3, 2] = -look_dir  # Negative because camera looks down -Z
    transform[:3, 3] = camera_pos
    
    return transform

def create_angled_camera(centroid, extent):
    """Create angled camera at ~45° elevation looking at centroid."""
    # Calculate camera distance
    max_extent = np.max(extent)
    distance = max_extent * 1.2  # Add margin
    
    # Position camera at elevated corner
    angle = np.pi / 4  # 45 degrees
    elevation = np.pi / 6  # 30 degrees above horizontal
    
    camera_pos = centroid + distance * np.array([
        np.cos(angle) * np.cos(elevation),
        np.sin(angle) * np.cos(elevation),
        np.sin(elevation)
    ])
    
    # Look direction (towards centroid)
    look_dir = centroid - camera_pos
    look_dir = look_dir / np.linalg.norm(look_dir)
    
    # Up vector (Z-axis)
    up = np.array([0, 0, 1])
    
    # Right vector
    right = np.cross(look_dir, up)
    right = right / np.linalg.norm(right)
    
    # Recalculate up to ensure orthogonality
    up = np.cross(right, look_dir)
    
    # Create 4x4 transformation matrix
    transform = np.eye(4)
    transform[:3, 0] = right
    transform[:3, 1] = up
    transform[:3, 2] = -look_dir  # Negative because camera looks down -Z
    transform[:3, 3] = camera_pos
    
    return transform

def render_preview_images(config_path, ply_path, output_dir):
    """Render preview images using nerfstudio."""
    try:
        # Load point cloud and compute geometry
        geo_data = load_ply_points(ply_path)
        centroid = geo_data['centroid']
        extent = geo_data['extent']
        
        # Create camera poses
        top_down_pose = create_top_down_camera(centroid, extent)
        angled_pose = create_angled_camera(centroid, extent)
        
        # Create temporary directory for camera JSON files
        with tempfile.TemporaryDirectory() as temp_dir:
            # Top-down view
            top_down_cameras = create_camera_json([top_down_pose], f"{temp_dir}/top_down_cameras.json")
            top_down_output = Path(output_dir) / "preview_top"
            
            subprocess.run([
                "ns-render", "camera-path",
                "--load-config", str(config_path),
                "--camera-path-filename", f"{temp_dir}/top_down_cameras.json",
                "--output-path", str(top_down_output),
                "--image-format", "jpg",
                "--renderer", "splatfacto"
            ], check=True, capture_output=True)
            
            # Angled view
            angled_cameras = create_camera_json([angled_pose], f"{temp_dir}/angled_cameras.json")
            angled_output = Path(output_dir) / "preview_angle"
            
            subprocess.run([
                "ns-render", "camera-path",
                "--load-config", str(config_path),
                "--camera-path-filename", f"{temp_dir}/angled_cameras.json",
                "--output-path", str(angled_output),
                "--image-format", "jpg",
                "--renderer", "splatfacto"
            ], check=True, capture_output=True)
            
        # Rename output files to expected names
        top_down_file = Path(output_dir) / "preview_top.jpg"
        angled_file = Path(output_dir) / "preview_angle.jpg"
        
        # Find and rename the actual output files
        for file in Path(output_dir).glob("preview_top*/*.jpg"):
            file.rename(top_down_file)
            break
            
        for file in Path(output_dir).glob("preview_angle*/*.jpg"):
            file.rename(angled_file)
            break
            
        # Clean up temporary directories
        for dir_name in ["preview_top*", "preview_angle*"]:
            for dir_path in Path(output_dir).glob(dir_name):
                if dir_path.is_dir():
                    import shutil
                    shutil.rmtree(dir_path)
        
        return True, "Preview images generated successfully"
        
    except Exception as e:
        return False, f"Failed to generate preview images: {str(e)}"

if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Generate preview images from trained Gaussian Splat")
    parser.add_argument("--config", required=True, help="Path to nerfstudio config.yml")
    parser.add_argument("--ply", required=True, help="Path to input splat.ply file")
    parser.add_argument("--output", required=True, help="Output directory for preview images")
    args = parser.parse_args()
    
    success, message = render_preview_images(args.config, args.ply, args.output)
    if success:
        print(f"SUCCESS: {message}")
        sys.exit(0)
    else:
        print(f"ERROR: {message}")
        sys.exit(1)