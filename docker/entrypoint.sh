#!/bin/bash
set -euo pipefail

if ! command -v nvidia-smi >/dev/null 2>&1; then
  echo "ERROR: nvidia-smi not found inside this container." >&2
  echo "Ensure Docker was started with --gpus all, verify the host GPU with nvidia-smi," >&2
  echo "and see docs/deployment.md (Troubleshooting: GPU not detected)." >&2
  exit 1
fi

if ! nvidia-smi -L >/dev/null 2>&1; then
  echo "ERROR: CUDA GPU is not available inside this container." >&2
  echo "Ensure Docker was started with --gpus all, verify the host GPU with nvidia-smi," >&2
  echo "and see docs/deployment.md (Troubleshooting: GPU not detected)." >&2
  exit 1
fi

exec /app/splatter "$@"
