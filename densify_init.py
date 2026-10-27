"""RoMa densification plugin hook (Lichtfeld-Densification-Plugin) before train."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from argparse import Namespace
from pathlib import Path

_DENSIFY_CLI = Path(__file__).resolve().parent / "scripts" / "run_densify_cli.py"


def _bool_env(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def densify_enabled() -> bool:
    return _bool_env("SPLATTER_DENSIFY", True)


def densify_python() -> str:
    """Python for RoMa densify (separate venv in product image; falls back to train venv locally)."""
    return os.environ.get("DENSIFY_PYTHON", sys.executable)


def _is_colmap_model(path: Path) -> bool:
    return (path / "cameras.bin").is_file() or (path / "cameras.txt").is_file()


def ensure_colmap_sparse0(scene_dir: Path) -> Path:
    """Densify CLI expects ``<scene>/sparse/0/``; undistort may write flat ``sparse/``."""
    scene_dir = scene_dir.resolve()
    sparse = scene_dir / "sparse"
    sparse0 = sparse / "0"
    if _is_colmap_model(sparse0):
        return sparse0
    if _is_colmap_model(sparse):
        sparse0.mkdir(parents=True, exist_ok=True)
        for item in sparse.iterdir():
            if item.is_file():
                dest = sparse0 / item.name
                if not dest.exists():
                    shutil.copy2(item, dest)
        return sparse0
    raise FileNotFoundError(f"no COLMAP model under {sparse} or {sparse0}")


def default_dense_ply_path(scene_dir: Path) -> Path:
    out_name = os.environ.get("DENSIFY_OUT_NAME", "points3D_dense.ply")
    return ensure_colmap_sparse0(scene_dir) / out_name


def build_densify_cli_args(scene_dir: Path) -> Namespace:
    """Build CLI namespace matching ``densification.densify`` (for tests / dry-run)."""
    roma = os.environ.get("DENSIFY_ROMA_SETTING", "turbo")
    num_refs = float(os.environ.get("DENSIFY_NUM_REFS", "0.6"))
    images_subdir = os.environ.get("DENSIFY_IMAGES_SUBDIR", "images")
    out_name = os.environ.get("DENSIFY_OUT_NAME", "points3D_dense.ply")
    return Namespace(
        scene_root=str(scene_dir.resolve()),
        images_subdir=images_subdir,
        out_name=out_name,
        roma_setting=roma,
        roma_model="outdoor",
        num_refs=num_refs,
        nns_per_ref=int(os.environ.get("DENSIFY_NNS_PER_REF", "4")),
        matches_per_ref=int(os.environ.get("DENSIFY_MATCHES_PER_REF", "12000")),
        certainty_thresh=float(os.environ.get("DENSIFY_CERTAINTY_THRESH", "0.20")),
        reproj_thresh=float(os.environ.get("DENSIFY_REPROJ_THRESH", "1.5")),
        sampson_thresh=float(os.environ.get("DENSIFY_SAMPSON_THRESH", "5.0")),
        min_parallax_deg=float(os.environ.get("DENSIFY_MIN_PARALLAX_DEG", "0.5")),
        no_filter=_bool_env("DENSIFY_NO_FILTER", False),
        max_points=int(os.environ.get("DENSIFY_MAX_POINTS", "0")),
        chunked_batches=int(os.environ.get("DENSIFY_CHUNKED_BATCHES", "1")),
        max_points_per_batch=int(os.environ.get("DENSIFY_MAX_POINTS_PER_BATCH", "0")),
        resume_chunks=_bool_env("DENSIFY_RESUME_CHUNKS", False),
        min_track_length=int(os.environ.get("DENSIFY_MIN_TRACK_LENGTH", "1")),
        prefetch_packages=int(os.environ.get("DENSIFY_PREFETCH_PACKAGES", "8")),
        pack_workers=int(os.environ.get("DENSIFY_PACK_WORKERS", "4")),
        seed=int(os.environ.get("DENSIFY_SEED", "0")),
    )


def _cli_argv(scene_dir: Path) -> list[str]:
    args = build_densify_cli_args(scene_dir)
    argv = [
        densify_python(),
        str(_DENSIFY_CLI),
        "--scene_root",
        args.scene_root,
        "--images_subdir",
        args.images_subdir,
        "--out_name",
        args.out_name,
        "--roma_setting",
        args.roma_setting,
        "--num_refs",
        str(args.num_refs),
    ]
    if args.no_filter:
        argv.append("--no_filter")
    return argv


def run_densify(scene_dir: Path) -> Path:
    """
    Run RoMa densify on a COLMAP scene root (``images/`` + ``sparse/``).

    Returns path to dense PLY for ``train_helper.sh --init``. Does not replace sparse points3D.
    """
    scene_dir = scene_dir.resolve()
    if not scene_dir.is_dir():
        raise FileNotFoundError(f"densify scene_dir missing: {scene_dir}")

    ensure_colmap_sparse0(scene_dir)
    out_ply = default_dense_ply_path(scene_dir)

    plugin_root = os.environ.get("DENSIFY_PLUGIN_ROOT", "/app/Lichtfeld-Densification-Plugin")
    cli_script = os.environ.get("DENSIFY_CLI_SCRIPT", str(_DENSIFY_CLI))
    if not Path(cli_script).is_file():
        raise FileNotFoundError(
            f"densify CLI wrapper missing: {cli_script} (set DENSIFY_CLI_SCRIPT or bake plugin image)"
        )
    if not Path(plugin_root).is_dir():
        raise FileNotFoundError(
            f"densify plugin root missing: {plugin_root} "
            "(clone shadygm/Lichtfeld-Densification-Plugin; set DENSIFY_PLUGIN_ROOT)"
        )

    roma = os.environ.get("DENSIFY_ROMA_SETTING", "turbo")
    num_refs = os.environ.get("DENSIFY_NUM_REFS", "0.6")
    print(
        f"[densify] scene={scene_dir} roma={roma} num_refs={num_refs} plugin={plugin_root}",
        flush=True,
    )
    print("[PROGRESS] stage=densify pct=5 detail=starting RoMa densification", flush=True)

    env = os.environ.copy()
    env.setdefault("DENSIFY_PLUGIN_ROOT", plugin_root)
    cmd = _cli_argv(scene_dir)
    cmd[1] = cli_script

    proc = subprocess.run(cmd, env=env, check=False)
    if proc.returncode != 0:
        raise subprocess.CalledProcessError(proc.returncode, cmd)

    if not out_ply.is_file():
        raise FileNotFoundError(f"densify finished but PLY missing: {out_ply}")

    stable = scene_dir / "dense_init.ply"
    if stable.resolve() != out_ply.resolve():
        shutil.copy2(out_ply, stable)

    print(
        f"[PROGRESS] stage=densify pct=15 detail=dense PLY ready points_file={stable.name}",
        flush=True,
    )
    return stable
