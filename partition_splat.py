#!/usr/bin/env python3
"""
partition_splat.py

Partition a Gaussian splat PLY file into separate tiles along x/z axes.
Each gaussian is assigned to a tile based on its position.

Usage:
    python partition_splat.py input.ply --partition-size 5.0 --output-dir ./partitions

Output files are named: <base>_partition_<size>_<x>_<z>.ply
where x and z are tile indices. The rendering app can translate each tile
by (x, z) * partition_size to reconstruct the original positions.
"""

import argparse
import sys
from pathlib import Path
from typing import Optional

import numpy as np
from plyfile import PlyData, PlyElement


def partition_splat_by_xz(vertices: np.ndarray, partition_size: float, recenter: bool = True) -> dict:
    """
    Partition gaussians into tiles based on their x/z position.
    
    Args:
        vertices: Vertex array from PLY file
        partition_size: Size of each partition in meters
        recenter: Transforms all vertices to make each partition centered around (x=0, z=0). Y is unaffected. 
    
    Returns:
        Dict mapping (x_idx, z_idx) -> vertex array for that partition
    """
    xyz_x = vertices["x"].astype(np.float64)
    xyz_z = vertices["z"].astype(np.float64)
    
    # Compute partition indices for each gaussian
    x_indices = np.floor(xyz_x / partition_size).astype(int)
    z_indices = np.floor(xyz_z / partition_size).astype(int)
    
    # Group gaussians by partition using vectorized operations
    partitions = {}
    unique_keys = set(zip(x_indices, z_indices))
    
    for x_idx, z_idx in unique_keys:
        mask = (x_indices == x_idx) & (z_indices == z_idx)
        partitions[(x_idx, z_idx)] = vertices[mask]
        if recenter:
            partitions[(x_idx, z_idx)]["x"] -= (x_idx + 0.5) * partition_size
            partitions[(x_idx, z_idx)]["z"] -= (z_idx + 0.5) * partition_size
    
    return partitions


def partition_splat(
    input_path: Path,
    partition_size: float,
    output_dir: Optional[Path] = None,
    base_name: Optional[str] = None,
) -> list[Path]:
    """
    Partition a splat PLY file into tiles.
    
    Args:
        input_path: Path to input PLY file
        partition_size: Size of each partition in meters
        output_dir: Directory to write output files (default: same as input)
        base_name: Base name for output files (default: input stem)
    
    Returns:
        List of paths to created partition files
    """
    input_path = Path(input_path)
    output_dir = Path(output_dir) if output_dir else input_path.parent
    base_name = base_name or input_path.stem
    
    output_dir.mkdir(parents=True, exist_ok=True)
    
    print(f"Loading: {input_path}")
    ply = PlyData.read(str(input_path))
    
    if "vertex" not in ply:
        raise ValueError(f"No vertex element in {input_path}")
    
    vertices = ply["vertex"].data
    print(f"  {len(vertices)} gaussians")
    
    print(f"Partitioning into tiles of size {partition_size}m...")
    partitions = partition_splat_by_xz(vertices, partition_size, recenter=True)
    
    # Format chunk size for filename (max 3 decimals, strip trailing zeros)
    chunk_size_rounded = round(partition_size, 3)
    chunk_size_str = f"{chunk_size_rounded:.3f}".rstrip('0').rstrip('.')
    
    output_paths = []
    print(f"Writing {len(partitions)} partition files to {output_dir}")
    
    for (x_idx, z_idx), vert in sorted(partitions.items()):
        z_idx *= -1 # Colmap to openGL (consistent with domain coordinates!)
        partition_filename = f"{base_name}_partition_{chunk_size_str}_{x_idx}_{z_idx}.ply"
        partition_path = output_dir / partition_filename
        
        partition_el = PlyElement.describe(vert, "vertex")
        partition_ply = PlyData([partition_el], text=False)
        partition_ply.write(str(partition_path))
        
        print(f"  {partition_filename}: {len(vert)} gaussians")
        output_paths.append(partition_path)
    
    print(f"Done. Created {len(output_paths)} partition files.")
    return output_paths


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Partition a Gaussian splat PLY file into tiles along x/z axes."
    )
    parser.add_argument(
        "input",
        type=Path,
        help="Input Gaussian splat PLY file"
    )
    parser.add_argument(
        "--partition-size",
        type=float,
        default=10.0,
        help="Size of each partition tile in meters. Default: 10.0"
    )
    parser.add_argument(
        "--output-dir", "-o",
        type=Path,
        default=None,
        help="Output directory for partition files. Default: same as input file"
    )
    parser.add_argument(
        "--base-name",
        default=None,
        help="Base name for output files. Default: input file stem"
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    
    if not args.input.exists():
        print(f"Error: Input file not found: {args.input}", file=sys.stderr)
        sys.exit(1)
    
    partition_splat(
        input_path=args.input,
        partition_size=args.partition_size,
        output_dir=args.output_dir,
        base_name=args.base_name,
    )


if __name__ == "__main__":
    main()
