#!/usr/bin/env python3
"""
convert_splat.py

Convert Gaussian splat files between formats:
  - PLY -> .splat (using ply2splat package)
  - PLY -> .sog (using LichtFeld-Studio convert)

Usage:
    # Convert PLY to .splat
    python convert_splat.py input.ply --format splat
    
    # Convert PLY to .sog
    python convert_splat.py input.ply --format sog
"""

import argparse
import os
import subprocess
import sys
from pathlib import Path

import ply2splat

# Same default as local_main training binary; override with LICHTFELD_BIN.
LICHTFELD_BIN = os.environ.get(
    "LICHTFELD_BIN",
    "/app/LichtFeld-Studio/build/LichtFeld-Studio",
)


def convert_ply_to_splat(
    input_path: Path,
    output_path: Path = None
) -> Path:
    """
    Convert a PLY file to .splat format using ply2splat.
    
    Args:
        input_path: Path to input PLY file
        output_path: Path for output .splat file (default: same name with .splat extension)
    
    Returns:
        Path to the created .splat file
    """
    input_path = Path(input_path)
    output_path = Path(output_path) if output_path else input_path.with_suffix(".splat")
    print(f"Converting PLY to splat: {input_path.name} -> {output_path.name}")
    ply2splat.convert(str(input_path), str(output_path))
    
    return output_path


def convert_ply_to_sog(input_path: Path, output_path: Path = None) -> Path:
    """
    Convert a .ply file to .sog format using LichtFeld-Studio convert.
    
    Args:
        input_path: Path to input .ply file
        output_path: Path for output .sog file (default: same name with .sog extension)
    
    Returns:
        Path to the created .sog file
    
    Raises:
        RuntimeError: If conversion fails
        FileNotFoundError: If LichtFeld-Studio binary is not found
    """
    input_path = Path(input_path)
    output_path = Path(output_path) if output_path else input_path.with_suffix(".sog")
    
    cmd = [
        LICHTFELD_BIN,
        "convert",
        str(input_path),
        str(output_path),
        "-y",
    ]
    
    print(f"Converting PLY to SOG (LichtFeld): {input_path.name} -> {output_path.name}")
    try:
        subprocess.run(cmd, check=True, capture_output=True, text=True)
        if not output_path.exists() or output_path.stat().st_size == 0:
            raise RuntimeError(
                f"SOG conversion produced empty/missing output: {output_path}"
            )
        return output_path
    except subprocess.CalledProcessError as e:
        detail = (e.stderr or e.stdout or "").strip()
        raise RuntimeError(f"SOG conversion failed: {detail or e}")
    except FileNotFoundError:
        raise FileNotFoundError(
            f"{LICHTFELD_BIN} not found. Set LICHTFELD_BIN or build LichtFeld-Studio."
        )


def convert_sog_to_ply(input_path: Path, output_path: Path = None) -> Path:
    """Convert a .sog file to PLY using LichtFeld-Studio convert (for combine/import)."""
    input_path = Path(input_path)
    output_path = (
        Path(output_path)
        if output_path
        else input_path.with_name(f"{input_path.stem}_import.ply")
    )

    cmd = [
        LICHTFELD_BIN,
        "convert",
        str(input_path),
        str(output_path),
        "-y",
    ]

    print(f"Converting SOG to PLY (LichtFeld): {input_path.name} -> {output_path.name}")
    try:
        subprocess.run(cmd, check=True, capture_output=True, text=True)
        if not output_path.exists() or output_path.stat().st_size == 0:
            raise RuntimeError(
                f"SOG→PLY conversion produced empty/missing output: {output_path}"
            )
        return output_path
    except subprocess.CalledProcessError as e:
        detail = (e.stderr or e.stdout or "").strip()
        raise RuntimeError(f"SOG→PLY conversion failed: {detail or e}")
    except FileNotFoundError:
        raise FileNotFoundError(
            f"{LICHTFELD_BIN} not found. Set LICHTFELD_BIN or build LichtFeld-Studio."
        )


def convert_file(input_path: Path, output_format: str) -> list[Path]:
    """
    Convert a splat file to the specified format(s).
    
    Args:
        input_path: Path to input file (.ply or .splat)
        output_format: Target format - "splat" or "sog"
    
    Returns:
        List of created output file paths
    """
    input_path = Path(input_path)
    input_ext = input_path.suffix.lower()
    outputs = []
    
    if input_ext != ".ply":
        raise ValueError(f"Input file must be a PLY file: {input_path}")

    if output_format == "splat":
        outputs.append(convert_ply_to_splat(input_path))
    elif output_format == "sog":
        outputs.append(convert_ply_to_sog(input_path))
    else:
        raise ValueError(f"Unsupported output format: {output_format}. Use 'splat' or 'sog'")
    
    return outputs


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Convert Gaussian splat files between PLY, .splat, and .sog formats."
    )
    parser.add_argument(
        "input",
        type=Path,
        help="Input file (.ply or .splat)"
    )
    parser.add_argument(
        "--format", "-f",
        choices=["splat", "sog"],
        default="splat",
        help="Output format. Default: splat"
    )
    parser.add_argument(
        "--output", "-o",
        type=Path,
        default=None,
        help="Output file path. Default: same directory with appropriate extension"
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    
    if not args.input.exists():
        print(f"Error: Input file not found: {args.input}", file=sys.stderr)
        sys.exit(1)
    
    try:
        outputs = convert_file(args.input, args.format)
        print(f"Created: {', '.join(str(p) for p in outputs)}")
    except (RuntimeError, FileNotFoundError, ValueError) as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
