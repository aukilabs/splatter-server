#!/usr/bin/env python3
"""Auto-fit + enable a crop box on the training model (LichtFeld --python-script).

Uses the built-in Fit Cropbox to Scene (trimmed / percentile) UI command —
same path as the toolbar — instead of a custom means AABB.

Reusable from train_helper / headless --python-script / editor.
"""

from __future__ import annotations

import os
from pathlib import Path

import lichtfeld as lf

# True = "Fit to Scene (Trimmed)" (percentile bounds); False = full AABB.
_USE_PERCENTILE = True


def _log(msg: str) -> None:
    try:
        lf.log.info(msg)
    except Exception:
        pass
    try:
        print(f"[auto_fit_cropbox] {msg}", flush=True)
    except Exception:
        pass
    marker = os.environ.get("LFS_CROP_FIT_LOG")
    if not marker:
        return
    try:
        path = Path(marker)
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as f:
            f.write(msg + "\n")
    except Exception:
        pass


def _pick_target(scene):
    name = getattr(scene, "training_model_node_name", None)
    if callable(name):
        name = name()
    if name:
        node = scene.get_node(name)
        if node is not None:
            return node

    target = None
    for n in scene.get_nodes():
        t = str(n.type)
        if "POINTCLOUD" in t or "SPLAT" in t:
            if target is None or n.id > target.id:
                target = n
    return target


def _fit_via_ui(scene, target) -> bool:
    crop_id = scene.get_or_create_cropbox_for_splat(target.id)
    if crop_id is None or int(crop_id) < 0:
        _log(f"get_or_create_cropbox_for_splat failed for {target.name}")
        return False

    crop_node = scene.get_node_by_id(int(crop_id))
    if crop_node is None:
        _log(f"crop node missing id={crop_id}")
        return False

    select = getattr(lf, "select_node", None)
    if not callable(select):
        _log("lf.select_node missing")
        return False
    select(crop_node.name)

    fit = None
    ui = getattr(lf, "ui", None)
    if ui is not None:
        fit = getattr(ui, "fit_cropbox_to_scene", None) or getattr(ui, "fit_crop_tool", None)
    if not callable(fit):
        _log("lf.ui.fit_cropbox_to_scene missing")
        return False

    fit(_USE_PERCENTILE)

    cb = crop_node.cropbox()
    if cb is None:
        _log(f"cropbox() None after fit on {crop_node.name}")
        return False
    cb.enabled = True

    _log(
        f"ui-fit percentile={_USE_PERCENTILE} {crop_node.name} "
        f"parent={target.name} min={cb.min} max={cb.max} enabled={cb.enabled}"
    )
    return True


def _enable_all_cropboxes(scene) -> None:
    for n in scene.get_nodes():
        cb = n.cropbox()
        if cb is None:
            continue
        cb.enabled = True
        _log(f"enabled {n.name} min={cb.min} max={cb.max}")


def auto_fit_cropbox() -> None:
    scene = lf.get_scene()
    if scene is None:
        _log("no scene")
        return

    target = _pick_target(scene)
    if target is None:
        _log("no SPLAT/POINTCLOUD target")
        return

    if _fit_via_ui(scene, target):
        _enable_all_cropboxes(scene)
        return

    _log("auto-fit failed")


@lf.on_training_start
def _on_training_start(_hook) -> None:
    auto_fit_cropbox()


auto_fit_cropbox()
