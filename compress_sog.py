#!/usr/bin/env python3

import argparse
import os
import subprocess
import sys


def compress_to_sog(input_path, output_path):
    """
    Compress a Gaussian Splat PLY file to SOG format using 3dgsconverter.
    """
    if not os.path.exists(input_path):
        print(f"Input file not found: {input_path}", file=sys.stderr)
        return 1

    cmd = [
        "3dgsconverter",
        "-i", str(input_path),
        "-o", str(output_path),
        "-f", "sog",
    ]

    print(f"Compressing to SOG: {input_path} -> {output_path}")

    try:
        result = subprocess.run(cmd, capture_output=True, text=True)

        if result.stdout:
            print(result.stdout)
        if result.stderr:
            print(result.stderr, file=sys.stderr)

        if result.returncode != 0:
            print(f"3dgsconverter failed with exit code: {result.returncode}", file=sys.stderr)
            return result.returncode

        if not os.path.exists(output_path):
            print(f"Output file was not created: {output_path}", file=sys.stderr)
            return 1

        input_size = os.path.getsize(input_path)
        output_size = os.path.getsize(output_path)
        ratio = input_size / output_size if output_size > 0 else 0
        print(f"SOG compression complete: {input_size} bytes -> {output_size} bytes ({ratio:.1f}x reduction)")

        return 0

    except FileNotFoundError:
        print("3dgsconverter not found. Install with: pip install git+https://github.com/francescofugazzi/3dgsconverter.git@0.8", file=sys.stderr)
        return 1
    except Exception as e:
        print(f"SOG compression error: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Compress Gaussian Splat PLY to SOG format")
    parser.add_argument("--input", required=True, help="Input PLY file path")
    parser.add_argument("--output", required=True, help="Output SOG file path")
    args = parser.parse_args()

    sys.exit(compress_to_sog(args.input, args.output))
