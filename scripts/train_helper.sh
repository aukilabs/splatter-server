#!/usr/bin/env bash
# train_helper.sh - preprocess + train with the locked R2 geometry recipe.
#
# Vendored for splatter-server Docker (S1). Based on LichtFeld-Studio R2 helper.
#
# R2 (human-locked 2026-09-13):
#   FastGS / mrnf / max_cap=1M / exposure off (opt-in via --exposure)
#   NC=0.05 / flatten=0.1 / prior normals=0.05 / depth SSI=0.2
#
# Usage:
#   train_helper.sh <scene> <out>
#   train_helper.sh -i 20000 -r 2 <scene> <out>
#   train_helper.sh --exposure <scene> <out>
#
# Optional env:
#   LFS_EXE, LICHTFELD_BIN  path to LichtFeld-Studio binary
#   LFS_ROOT                LichtFeld source tree (default: /app/LichtFeld-Studio)
#   LFS_PYTHON              Python interpreter for optional results summary

set -euo pipefail

HELPER_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LFS_ROOT="${LFS_ROOT:-/app/LichtFeld-Studio}"

usage() {
  echo "usage: $0 [-i iterations] [-r resize_factor] [--exposure] <scene> <out>" >&2
  exit 2
}

ITERATIONS=30000
RESIZE_FACTOR=""
EXPOSURE=0
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

if [[ -n "${RESIZE_FACTOR}" ]] && ! [[ "${RESIZE_FACTOR}" =~ ^[0-9]+$ ]]; then
  echo "error: resize_factor must be a positive integer (got: ${RESIZE_FACTOR})" >&2
  exit 2
fi

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

mkdir -p "${OUT}"

echo "==> preprocess (MoGe-2 depth + normals)"
echo "    scene=${SCENE}"
"${EXE}" preprocess "${SCENE}" --mode both

if [[ "${EXPOSURE}" -eq 1 ]]; then
  EXPOSURE_LABEL=on
else
  EXPOSURE_LABEL=off
fi

echo "==> train R2 recipe"
echo "    iters=${ITERATIONS}  out=${OUT}"
if [[ -n "${RESIZE_FACTOR}" ]]; then
  echo "    resize_factor=${RESIZE_FACTOR}"
fi
echo "    NC=0.05 flatten=0.1 prior_normals=0.05 depth_ssi=0.2 exposure=${EXPOSURE_LABEL}"

TRAIN_CMD=(
  "${EXE}"
  --headless
  --train
  -d "${SCENE}"
  -o "${OUT}"
  --strategy mrnf
  --max-cap 1000000
  -i "${ITERATIONS}"
  --eval
  --eval-steps "${ITERATIONS}"
  --export ply
  --use-normal-loss
  --normal-loss-weight 0.05
  --normal-consistency-weight 0.05
  --normal-flatten-weight 0.1
  --use-depth-loss
  --depth-loss-mode ssi
  --depth-loss-weight 0.2
  --quiet
)

if [[ -n "${RESIZE_FACTOR}" ]]; then
  TRAIN_CMD+=(-r "${RESIZE_FACTOR}")
fi

if [[ "${EXPOSURE}" -eq 1 ]]; then
  TRAIN_CMD+=(--exposure-correction)
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
    --arm R2
  SUMMARY_RC=$?
  set -e
  if [[ "${SUMMARY_RC}" -ne 0 ]]; then
    echo "warning: results summary incomplete (exit ${SUMMARY_RC})" >&2
  fi
fi

echo "==> done"
echo "    output:  ${OUT}"
echo "    ply:     ${OUT}/splat_${ITERATIONS}.ply"
if [[ -f "${OUT}/results_summary.txt" ]]; then
  echo "    summary: ${OUT}/results_summary.txt"
  echo "             ${OUT}/results_summary.json"
fi
