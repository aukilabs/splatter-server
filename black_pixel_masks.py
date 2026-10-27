"""Write LichtFeld sidecar masks for pure-black occlusion pixels (0=ignore, 255=keep)."""

from __future__ import annotations

from pathlib import Path

import cv2
import numpy as np

_IMAGE_SUFFIXES = {".jpg", ".jpeg", ".png", ".webp", ".bmp", ".tif", ".tiff"}


def _iter_scene_images(images_dir: Path) -> list[Path]:
    images_dir = images_dir.resolve()
    if not images_dir.is_dir():
        raise FileNotFoundError(f"images directory missing: {images_dir}")
    out: list[Path] = []
    for path in sorted(images_dir.rglob("*")):
        if path.is_file() and path.suffix.lower() in _IMAGE_SUFFIXES:
            out.append(path)
    return out


def mask_path_for_image(masks_dir: Path, image_path: Path, images_dir: Path) -> Path:
    rel = image_path.relative_to(images_dir)
    return masks_dir / rel.parent / f"{rel.stem}.png"


def black_pixel_mask_bgr(image_bgr: np.ndarray) -> np.ndarray | None:
    """Return uint8 mask (0=ignore, 255=keep) when any pure-black pixel exists."""
    if image_bgr.ndim != 3 or image_bgr.shape[2] < 3:
        return None
    black = np.all(image_bgr[:, :, :3] == 0, axis=2)
    if not np.any(black):
        return None
    return np.where(black, 0, 255).astype(np.uint8)


def ensure_black_pixel_masks(
    images_dir: Path,
    *,
    scene_dir: Path | None = None,
    masks_dir: Path | None = None,
) -> bool:
    """
    Scan ``images_dir``; write ``masks/`` sidecars for frames with pure-black pixels.

    Layout matches LichtFeld COLMAP sidecars: ``<scene>/masks/<same-relative-path>.png``.
    Returns True if at least one mask file was written.
    """
    images_dir = images_dir.resolve()
    if scene_dir is None:
        if images_dir.name != "images":
            raise ValueError(f"expected .../images, got {images_dir}")
        scene_dir = images_dir.parent
    else:
        scene_dir = scene_dir.resolve()

    if masks_dir is None:
        masks_dir = scene_dir / "masks"
    else:
        masks_dir = masks_dir.resolve()

    written = 0
    for image_path in _iter_scene_images(images_dir):
        image_bgr = cv2.imread(str(image_path), cv2.IMREAD_COLOR)
        if image_bgr is None:
            print(f"[black_pixel_masks] skip unreadable image: {image_path}", flush=True)
            continue

        mask = black_pixel_mask_bgr(image_bgr)
        if mask is None:
            continue

        out_path = mask_path_for_image(masks_dir, image_path, images_dir)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        if not cv2.imwrite(str(out_path), mask):
            raise OSError(f"failed to write mask: {out_path}")
        written += 1

    if written:
        print(
            f"[black_pixel_masks] wrote {written} mask(s) under {masks_dir}",
            flush=True,
        )
    else:
        print("[black_pixel_masks] no pure-black pixels; no masks written", flush=True)
    return written > 0


def main() -> None:
    import argparse

    parser = argparse.ArgumentParser(
        description="Generate LFS masks/ sidecars for pure-black pixels (0=ignore, 255=keep).",
    )
    parser.add_argument(
        "scene_or_images",
        type=Path,
        help="Scene root (with images/) or images/ directory",
    )
    args = parser.parse_args()
    path = args.scene_or_images.resolve()
    if path.name == "images":
        ensure_black_pixel_masks(path)
    else:
        ensure_black_pixel_masks(path / "images", scene_dir=path)


if __name__ == "__main__":
    main()
