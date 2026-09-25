#!/usr/bin/env bash
# train_helper.sh - preprocess + train with the locked geo recipe (s6_ld50 lineage).
#
# Vendored for splatter-server Docker. Synced from LichtFeld-Studio train_helper
# (locked 2026-09-25 after fair A/B + visual check):
#   FastGS / mrnf / max_cap=1M / exposure off (opt-in via --exposure)
#   NC=0.01 / flatten=0.5 / prior normals=0.05 / depth SSI=0.15 / Ld=50
#   opacity_lr=0.006 / opacity_reg=0.0015 / scale_reg=0.001
#   max_screen_share=0.2 / oversize_split=0.25 / normal_start=0.03
#   Config: config/lichtfeld_optimization_params.json
#   Auto crop: scripts/auto_fit_cropbox.py (optional; --no-crop to skip)
#
# Node note (2026-09-25): default skips L_d CLI until the image binary is rebuilt
# with --use-depth-distortion (LichtFeld >= 3693dfb73). Pass --ld 50 to enable.
#
# Usage:
#   train_helper.sh <scene> <out>
#   train_helper.sh -i 20000 -r 2 <scene> <out>
#   train_helper.sh --no-crop <scene> <out>
#   train_helper.sh --ld 0 <scene> <out>
#   train_helper.sh --ld 50 <scene> <out>
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
  echo "usage: $0 [-i iterations] [-r resize] [--exposure] [--no-crop] [--ld WEIGHT] <scene> <out>" >&2
  exit 2
}

ITERATIONS=30000
RESIZE_FACTOR=2
EXPOSURE=0
USE_CROP=1
# Empty / 0 = omit L_d CLI (compatible with pre-L_d binaries). Locked recipe is 50.
LD_WEIGHT="0"
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
    --no-crop)
      USE_CROP=0
      shift
      ;;
    --ld|--depth-distortion-weight)
      [[ $# -ge 2 ]] || usage
      LD_WEIGHT="$2"
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
CROP_PY="${HELPER_DIR}/auto_fit_cropbox.py"
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

if [[ ! -f "${CONFIG}" ]]; then
  echo "error: train config missing: ${CONFIG}" >&2
  echo "       set LICHTFELD_CONFIG or place lichtfeld_optimization_params.json under config/" >&2
  exit 1
fi

mkdir -p "${OUT}"

echo "==> preprocess (MoGe-2 depth + normals)"
echo "    scene=${SCENE}"
"${EXE}" preprocess "${SCENE}" --mode both

if [[ "${EXPOSURE}" -eq 1 ]]; then
  EXPOSURE_LABEL=on
else
  EXPOSURE_LABEL=off
fi

if [[ -z "${LD_WEIGHT}" || "${LD_WEIGHT}" == "0" || "${LD_WEIGHT}" == "0.0" ]]; then
  LD_LABEL="off (deferred until L_d binary)"
else
  LD_LABEL="${LD_WEIGHT}"
fi

echo "==> train geo recipe (s6_ld50 lineage)"
echo "    iters=${ITERATIONS}  resize=${RESIZE_FACTOR}  out=${OUT}"
echo "    NC=0.01 flatten=0.5 prior_normals=0.05 depth_ssi=0.15 Ld=${LD_LABEL} exposure=${EXPOSURE_LABEL}"
echo "    config=${CONFIG}"
echo "    export=ply,sog"

TRAIN_CMD=(
  "${EXE}"
  --headless
  --train
  --config "${CONFIG}"
  -d "${SCENE}"
  -o "${OUT}"
  --strategy mrnf
  --max-cap 1000000
  -i "${ITERATIONS}"
  -r "${RESIZE_FACTOR}"
  --eval
  --eval-steps "${ITERATIONS}"
  --export "ply,sog"
  --use-normal-loss
  --normal-loss-weight 0.05
  --normal-consistency-weight 0.01
  --normal-flatten-weight 0.5
  --normal-start-fraction 0.03
  --use-depth-loss
  --depth-loss-mode ssi
  --depth-loss-weight 0.15
  --max-screen-share 0.2
  --oversize-split-fraction 0.25
  --quiet
)

# Only emit L_d CLI when weight > 0 (requires binary with --use-depth-distortion).
if [[ -n "${LD_WEIGHT}" && "${LD_WEIGHT}" != "0" && "${LD_WEIGHT}" != "0.0" ]]; then
  TRAIN_CMD+=(--use-depth-distortion --depth-distortion-weight "${LD_WEIGHT}")
fi

if [[ "${EXPOSURE}" -eq 1 ]]; then
  TRAIN_CMD+=(--exposure-correction)
fi

if [[ "${USE_CROP}" -eq 1 ]]; then
  if [[ -f "${CROP_PY}" ]]; then
    TRAIN_CMD+=(--python-script "${CROP_PY}")
  else
    echo "warning: crop script missing (${CROP_PY}); training without auto crop" >&2
  fi
fi

"${TRAIN_CMD[@]}"

echo "==> results summary (PSNR / LEP)"
if [[ ! -f "${SUMMARY_PY}" ]]; then
  echo "warning: skipping results summary (missing ${SUMMARY_PY})" >&2
else
  # Soft-fail: product jobs must not die on missing cv2 / LEP tooling.
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
