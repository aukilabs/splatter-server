#!/usr/bin/env bash
# train_helper.sh - splatter train helper (no MoGe preprocess).
#
# Locked 2026-10-29 (LFS v0.5.4.2 pin; config-primary recipe):
#   Recipe knobs live in config/lichtfeld_optimization_params.json (mrnf, max_cap 1M, NC, bg_mode, …)
#   Helper passes scene/runtime only: -d/-o, -i/-r, export/eval, cropbox, masks, --quiet
#   Optional local experiments: --ld, --init, --no-exposure, --no-crop, --cropbox
#
# Usage:
#   train_helper.sh <scene> <out>
#   train_helper.sh -i 30000 -r 2 <scene> <out>
#   train_helper.sh --no-crop <scene> <out>
#   train_helper.sh --no-exposure <scene> <out>
#   train_helper.sh --ld 50 <scene> <out>
#   train_helper.sh --init dense.ply <scene> <out>
#   train_helper.sh --cropbox x0,y0,z0,x1,y1,z1 <scene> <out>
#
# Optional env:
#   LFS_EXE / LICHTFELD_BIN  path to LichtFeld-Studio binary
#   LFS_ROOT                 LichtFeld tree (default: /app/LichtFeld-Studio)
#   LFS_PYTHON               Python for optional results summary
#   LICHTFELD_CONFIG         train JSON (default: ../config/lichtfeld_optimization_params.json)

set -euo pipefail

HELPER_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LFS_ROOT="${LFS_ROOT:-/app/LichtFeld-Studio}"

usage() {
  echo "usage: $0 [-i iterations] [-r resize] [--init PLY] [--cropbox X0,Y0,Z0,X1,Y1,Z1] [--no-exposure] [--no-crop] [--ld WEIGHT] <scene> <out>" >&2
  exit 2
}

ITERATIONS=30000
RESIZE_FACTOR=2
EXPOSURE=1
USE_CROP=1
CROPBOX=""
LD_WEIGHT="0"
INIT_PLY=""
SCENE=""
OUT=""

while [[ $# -gt 0 ]]; do
  case "$1" in
    -i)
      [[ $# -ge 2 ]] || usage
      ITERATIONS="$2"
      shift 2
      ;;
    -r|--resize_factor)
      [[ $# -ge 2 ]] || usage
      RESIZE_FACTOR="$2"
      shift 2
      ;;
    --exposure|--exposure-correction)
      EXPOSURE=1
      shift
      ;;
    --no-exposure)
      EXPOSURE=0
      shift
      ;;
    --no-crop)
      USE_CROP=0
      shift
      ;;
    --cropbox)
      [[ $# -ge 2 ]] || usage
      CROPBOX="$2"
      shift 2
      ;;
    --ld|--depth-distortion-weight)
      [[ $# -ge 2 ]] || usage
      LD_WEIGHT="$2"
      shift 2
      ;;
    --init)
      [[ $# -ge 2 ]] || usage
      INIT_PLY="$2"
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

CONFIG="${LICHTFELD_CONFIG:-${HELPER_DIR}/../config/lichtfeld_optimization_params.json}"
SUMMARY_PY="${LFS_ROOT}/scripts/train_helper_summary.py"

resolve_exe() {
  if [[ -n "${LFS_EXE:-}" ]]; then
    printf '%s\n' "${LFS_EXE}"
    return
  fi
  if [[ -n "${LICHTFELD_BIN:-}" ]]; then
    printf '%s\n' "${LICHTFELD_BIN}"
    return
  fi
  local candidates=(
    "${LFS_ROOT}/build/LichtFeld-Studio.exe"
    "${LFS_ROOT}/build/LichtFeld-Studio"
    "${LFS_ROOT}/build/bin/LichtFeld-Studio"
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
  echo "       set LFS_EXE or LICHTFELD_BIN, or build the project first" >&2
  exit 1
}

resolve_python() {
  if [[ -n "${LFS_PYTHON:-}" ]]; then
    printf '%s\n' "${LFS_PYTHON}"
    return
  fi
  local candidates=(python3 python)
  local c
  for c in "${candidates[@]}"; do
    if command -v "${c}" >/dev/null 2>&1; then
      printf '%s\n' "${c}"
      return
    fi
  done
  echo "error: Python not found; set LFS_PYTHON for the results summary step" >&2
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
  echo "       set LICHTFELD_CONFIG or place lichtfeld_optimization_params.json under config/" >&2
  exit 1
fi

mkdir -p "${OUT}"

echo "==> skip MoGe preprocess (normals via LFS auto-generate when config enables priors)"
echo "    scene=${SCENE}"

if [[ "${EXPOSURE}" -eq 1 ]]; then
  EXPOSURE_LABEL=config
else
  EXPOSURE_LABEL=off
fi

if [[ -z "${LD_WEIGHT}" || "${LD_WEIGHT}" == "0" || "${LD_WEIGHT}" == "0.0" ]]; then
  LD_LABEL="off"
else
  LD_LABEL="${LD_WEIGHT}"
fi

echo "==> train (config-primary recipe)"
echo "    iters=${ITERATIONS}  resize=${RESIZE_FACTOR}  out=${OUT}"
echo "    Ld=${LD_LABEL} exposure=${EXPOSURE_LABEL}"
if [[ -n "${INIT_PLY}" ]]; then
  echo "    init=${INIT_PLY}"
fi
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

if [[ -n "${LD_WEIGHT}" && "${LD_WEIGHT}" != "0" && "${LD_WEIGHT}" != "0.0" ]]; then
  TRAIN_CMD+=(--use-depth-distortion --depth-distortion-weight "${LD_WEIGHT}")
fi

if [[ "${EXPOSURE}" -eq 0 ]]; then
  TRAIN_CMD+=(--no-exposure-correction)
fi

if [[ -n "${INIT_PLY}" ]]; then
  if [[ ! -f "${INIT_PLY}" ]]; then
    echo "error: init PLY missing: ${INIT_PLY}" >&2
    exit 1
  fi
  TRAIN_CMD+=(--init "${INIT_PLY}")
fi

if [[ "${USE_CROP}" -eq 1 ]]; then
  if [[ -n "${CROPBOX}" ]]; then
    echo "    cropbox=${CROPBOX}"
    TRAIN_CMD+=(--cropbox "${CROPBOX}")
  else
    echo "warning: --crop enabled but no --cropbox; training without crop" >&2
  fi
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
