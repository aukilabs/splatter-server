#!/usr/bin/env bash
# train_helper.sh - product train wrapper (no MoGe preprocess).
#
# Recipe knobs: config/lichtfeld_optimization_params.json.
# Runtime: -i/-r, optional --cropbox, sidecar masks/, --quiet.
#
# Usage:
#   train_helper.sh <scene> <out>
#   train_helper.sh -i 30000 -r 2 <scene> <out>
#   train_helper.sh --cropbox x0,y0,z0,x1,y1,z1 <scene> <out>

set -euo pipefail

HELPER_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LFS_ROOT="/app/LichtFeld-Studio"
CONFIG="${HELPER_DIR}/../config/lichtfeld_optimization_params.json"
SUMMARY_PY="${LFS_ROOT}/scripts/train_helper_summary.py"

usage() {
  echo "usage: $0 [-i iterations] [-r resize] [--cropbox X0,Y0,Z0,X1,Y1,Z1] <scene> <out>" >&2
  exit 2
}

ITERATIONS=30000
RESIZE_FACTOR=2
CROPBOX=""
SCENE=""
OUT=""

while [[ $# -gt 0 ]]; do
  case "$1" in
    -i)
      [[ $# -ge 2 ]] || usage
      ITERATIONS="$2"
      shift 2
      ;;
    -r)
      [[ $# -ge 2 ]] || usage
      RESIZE_FACTOR="$2"
      shift 2
      ;;
    --cropbox)
      [[ $# -ge 2 ]] || usage
      CROPBOX="$2"
      shift 2
      ;;
    -h|--help)
      usage
      ;;
    -*)
      echo "error: unknown option: $1" >&2
      usage
      ;;
    *)
      if [[ -z "${SCENE}" ]]; then
        SCENE="$1"
      elif [[ -z "${OUT}" ]]; then
        OUT="$1"
      else
        usage
      fi
      shift
      ;;
  esac
done

[[ -n "${SCENE}" && -n "${OUT}" ]] || usage

if ! [[ "${ITERATIONS}" =~ ^[0-9]+$ ]] || [[ "${ITERATIONS}" -le 0 ]]; then
  echo "error: iterations must be a positive integer (got: ${ITERATIONS})" >&2
  exit 2
fi

if ! [[ "${RESIZE_FACTOR}" =~ ^[0-9]+$ ]]; then
  echo "error: resize_factor must be a positive integer (got: ${RESIZE_FACTOR})" >&2
  exit 2
fi

resolve_exe() {
  local candidates=(
    "${LFS_ROOT}/build/LichtFeld-Studio"
    "${LFS_ROOT}/build/bin/LichtFeld-Studio"
    "${LFS_ROOT}/build/LichtFeld-Studio.exe"
    "${LFS_ROOT}/build/Release/LichtFeld-Studio.exe"
  )
  local c
  for c in "${candidates[@]}"; do
    if [[ -f "${c}" ]]; then
      printf '%s\n' "${c}"
      return
    fi
  done
  echo "error: LichtFeld-Studio binary not found under ${LFS_ROOT}/build" >&2
  exit 1
}

resolve_python() {
  local c
  for c in python3 python; do
    if command -v "${c}" >/dev/null 2>&1; then
      printf '%s\n' "${c}"
      return
    fi
  done
  echo "error: Python not found for results summary" >&2
  exit 1
}

EXE="$(resolve_exe)"
PYTHON="$(resolve_python)"

if [[ ! -d "${SCENE}/images" ]]; then
  echo "error: scene images missing: ${SCENE}/images" >&2
  exit 1
fi

scene_has_sidecar_masks() {
  local masks_dir="${SCENE}/masks"
  [[ -d "${masks_dir}" ]] || return 1
  find "${masks_dir}" -type f \( \
    -iname '*.png' -o -iname '*.jpg' -o -iname '*.jpeg' -o \
    -iname '*.webp' -o -iname '*.bmp' -o -iname '*.tif' -o -iname '*.tiff' \
  \) -print -quit | grep -q .
}

USE_SIDECAR_MASKS=0
if scene_has_sidecar_masks; then
  USE_SIDECAR_MASKS=1
fi

if [[ ! -f "${CONFIG}" ]]; then
  echo "error: train config missing: ${CONFIG}" >&2
  exit 1
fi

mkdir -p "${OUT}"

echo "==> train"
echo "    scene=${SCENE}"
echo "    iters=${ITERATIONS}  resize=${RESIZE_FACTOR}  out=${OUT}"
if [[ "${USE_SIDECAR_MASKS}" -eq 1 ]]; then
  echo "    masks=${SCENE}/masks (mask-mode=ignore, no-alpha-as-mask)"
fi
echo "    config=${CONFIG}"
echo "    export=ply,sog"

TRAIN_CMD=(
  "${EXE}"
  --headless
  --train
  --config "${CONFIG}"
  -d "${SCENE}"
  -o "${OUT}"
  -i "${ITERATIONS}"
  -r "${RESIZE_FACTOR}"
  --output-name "splat_${ITERATIONS}"
  --eval
  --eval-steps "${ITERATIONS}"
  --export "ply,sog"
  --quiet
)

if [[ -n "${CROPBOX}" ]]; then
  echo "    cropbox=${CROPBOX}"
  TRAIN_CMD+=(--cropbox "${CROPBOX}")
fi

if [[ "${USE_SIDECAR_MASKS}" -eq 1 ]]; then
  TRAIN_CMD+=(--mask-mode ignore --no-alpha-as-mask)
fi

"${TRAIN_CMD[@]}"

echo "==> results summary (PSNR / LEP)"
if [[ ! -f "${SUMMARY_PY}" ]]; then
  echo "warning: skipping results summary (missing ${SUMMARY_PY})" >&2
else
  set +e
  "${PYTHON}" "${SUMMARY_PY}" \
    --out-dir "${OUT}" \
    --iterations "${ITERATIONS}" \
    --executable "${EXE}" \
    --arm geo
  SUMMARY_RC=$?
  set -e
  if [[ "${SUMMARY_RC}" -ne 0 ]]; then
    echo "warning: results summary incomplete (exit ${SUMMARY_RC})" >&2
  fi
fi

echo "==> done"
echo "    output:  ${OUT}"
echo "    ply:     ${OUT}/splat_${ITERATIONS}.ply"
echo "    sog:     ${OUT}/splat_${ITERATIONS}.sog"
if [[ -f "${OUT}/results_summary.txt" ]]; then
  echo "    summary: ${OUT}/results_summary.txt"
  echo "             ${OUT}/results_summary.json"
fi
