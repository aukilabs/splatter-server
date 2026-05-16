#!/usr/bin/env python3

import subprocess
import logging
import sys
import os
import time
import json
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

    # Generate preview images (best effort - don't fail the job if this fails)
    logger.info("Generating Preview Images")
    try:
        exit_code = run_python_script("generate_preview_images.py",
                                    "--config", args.job_root_path / "refined/splatter/splatfacto/config.yml",
                                    "--ply", args.job_root_path / "refined/splatter/splat.ply",
                                    "--output", args.job_root_path / "refined/splatter")
        if exit_code == 0:
            logger.info("Preview images generated successfully")
        else:
            logger.warning("Preview image generation failed, continuing anyway")
    except Exception as e:
        logger.warning(f"Preview image generation failed: {e}, continuing anyway")

    # Generate preview video (best effort - don't fail the job if this fails)
    logger.info("Generating Preview Video")
    try:
        exit_code = run_python_script("generate_preview_video.py",
                                    "--config", args.job_root_path / "refined/splatter/splatfacto/config.yml",
                                    "--ply", args.job_root_path / "refined/splatter/splat.ply",
                                    "--output", args.job_root_path / "refined/splatter",
                                    "--frames", "180")
        if exit_code == 0:
            logger.info("Preview video generated successfully")
        else:
            logger.warning("Preview video generation failed, continuing anyway")
    except Exception as e:
        logger.warning(f"Preview video generation failed: {e}, continuing anyway")
    
    sys.exit(0)