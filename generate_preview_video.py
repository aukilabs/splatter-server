#!/usr/bin/env python3
"""
Generate preview video of trained Gaussian Splat.

This script renders an orbital camera path around the trained splat
and encodes it as an MP4 video.
"""

import numpy as np
import subprocess
import json
import tempfile
import os
import sys
import shutil
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

def create_orbital_camera_path(centroid, extent, num_frames=180):
    """Create orbital camera path around the centroid."""
    # Calculate camera distance to frame the entire scene
    max_extent = np.max(extent)
    distance = max_extent * 1.5  # Add margin
    
    # Elevation angle (30-45 degrees above horizontal)
    elevation = np.pi / 6  # 30 degrees
    
    camera_poses = []
    
    # Create 300-degree arc (5/6 of full circle)
    start_angle = -np.pi * 5/12  # Start 75 degrees back
    end_angle = np.pi * 17/12    # End 75 degrees past
    
    for i in range(num_frames):
        # Interpolate angle along the arc
        t = i / (num_frames - 1)
        angle = start_angle + t * (end_angle - start_angle)
        
        # Calculate camera position
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
        
        camera_poses.append(transform)
    
    return camera_poses

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

def render_preview_video(config_path, ply_path, output_dir, num_frames=180):
    """Render preview video using nerfstudio and ffmpeg."""
    try:
        # Load point cloud and compute geometry
        geo_data = load_ply_points(ply_path)
        centroid = geo_data['centroid']
        extent = geo_data['extent']
        
        # Create orbital camera path
        camera_poses = create_orbital_camera_path(centroid, extent, num_frames)
        
        # Create temporary directory for rendering
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_dir = Path(temp_dir)
            
            # Create camera JSON file
            camera_file = temp_dir / "orbital_cameras.json"
            create_camera_json(camera_poses, camera_file)
            
            # Render frames using nerfstudio
            frames_dir = temp_dir / "frames"
            frames_dir.mkdir()
            
            subprocess.run([
                "ns-render", "camera-path",
                "--load-config", str(config_path),
                "--camera-path-filename", str(camera_file),
                "--output-path", str(frames_dir),
                "--image-format", "png",
                "--renderer", "splatfacto"
            ], check=True, capture_output=True)
            
            # Encode frames to MP4 using ffmpeg
            output_video = Path(output_dir) / "preview.mp4"
            
            subprocess.run([
                "ffmpeg", "-y",
                "-framerate", "30",
                "-i", f"{frames_dir}/frame_%05d.png",
                "-c:v", "libx264",
                "-pix_fmt", "yuv420p",
                "-crf", "23",
                "-movflags", "+faststart",
                str(output_video)
            ], check=True, capture_output=True)
            
        return True, f"Preview video generated successfully: {output_video}"
        
    except Exception as e:
        return False, f"Failed to generate preview video: {str(e)}"

if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Generate preview video from trained Gaussian Splat")
    parser.add_argument("--config", required=True, help="Path to nerfstudio config.yml")
    parser.add_argument("--ply", required=True, help="Path to input splat.ply file")
    parser.add_argument("--output", required=True, help="Output directory for preview video")
    parser.add_argument("--frames", type=int, default=180, help="Number of frames to render (default: 180)")
    args = parser.parse_args()
    
    success, message = render_preview_video(args.config, args.ply, args.output, args.frames)
    if success:
        print(f"SUCCESS: {message}")
        sys.exit(0)
    else:
        print(f"ERROR: {message}")
        sys.exit(1)