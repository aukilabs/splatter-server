from pathlib import Path
import preprocessing
import filter_splats
import convert_splat
import argparse
import time
import os
import re
import subprocess
import crop_aabb
import black_pixel_masks

LICHTFELD_TRAIN_HELPER = os.environ.get(
    "LICHTFELD_TRAIN_HELPER", "/app/scripts/train_helper.sh"
)


_TRAINING_RE = re.compile(
    r"(\d+)/(\d+)"  # iter/total
    r".*?Loss:\s*([\d.]+)"  # loss value
)


def _stream_training_output(proc: subprocess.Popen) -> None:
    """Read binary stdout, split on \\r or \\n, and re-emit each fragment
    as a full line so the Rust reader (which splits on \\n) sees them
    individually."""
    buf = b""
    while True:
        chunk = proc.stdout.read(1)
        if not chunk:
            break
        if chunk in (b"\r", b"\n"):
            if buf:
                line = buf.decode("utf-8", errors="replace").strip()
                buf = b""
                if not line:
                    continue
                m = _TRAINING_RE.search(line)
                if m:
                    cur, total, loss = int(m.group(1)), int(m.group(2)), m.group(3)
                    pct = int(cur / total * 100) if total > 0 else 0
                    print(
                        f"[PROGRESS] stage=training pct={pct} "
                        f"detail={cur}/{total} loss={loss}",
                        flush=True,
                    )
                else:
                    print(line, flush=True)
        else:
            buf += chunk
    if buf:
        line = buf.decode("utf-8", errors="replace").strip()
        if line:
            print(line, flush=True)


def _prepare_helper_scene(colmap_dir: Path, images_dir: Path) -> Path:
    """Map COLMAP layout to helper scene root (requires ``<scene>/images``)."""
    colmap_dir = colmap_dir.resolve()
    images_dir = images_dir.resolve()

    # Standard undistort layout: dense/{sparse,images}
    if (
        colmap_dir.name == "sparse"
        and images_dir.name == "images"
        and colmap_dir.parent == images_dir.parent
    ):
        return colmap_dir.parent

    scene = colmap_dir
    if (scene / "images").exists():
        return scene

    images_link = scene / "images"
    if not images_link.exists():
        os.symlink(str(images_dir), str(images_link))
    return scene


def _run_training_cmd(cmd: list[str]) -> None:
    print(f"Running command: {' '.join(cmd)}", flush=True)
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    _stream_training_output(proc)
    ret = proc.wait()
    if ret != 0:
        raise subprocess.CalledProcessError(ret, cmd)


def train_splat(
    colmap_dir: Path,
    output_dir: Path,
    images_dir: Path = None,
    iterations: int = 30000,
    use_crop: bool = True,
) -> None:
    if not images_dir:
        images_dir = colmap_dir / "images"

    scene_dir = _prepare_helper_scene(colmap_dir, images_dir)
    black_pixel_masks.ensure_black_pixel_masks(scene_dir / "images", scene_dir=scene_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    cmd = [
        LICHTFELD_TRAIN_HELPER,
        "-i",
        str(iterations),
        "-r",
        "2",
        str(scene_dir),
        str(output_dir),
    ]
    if not use_crop:
        cmd.append("--no-crop")
    else:
        try:
            cropbox = crop_aabb.compute_training_cropbox(
                init_ply=None,
                colmap_dir=colmap_dir,
            )
            cmd.extend(["--cropbox", cropbox])
        except (FileNotFoundError, ValueError) as exc:
            print(
                f"[train_splat] crop AABB skipped ({exc}); training without --cropbox",
                flush=True,
            )
    _run_training_cmd(cmd)


def scan_main(
    job_root: Path,
    scan_id: str,
    iterations: int = 30000,
    reuse_trained: bool = False,
    do_convert_splat: bool = False,
    do_convert_sog: bool = False,
) -> None:
    start_time = time.time()

    splat_filename = f"splat_{iterations}"
    splat_dir = job_root / "refined" / "local" / scan_id / "splat"
    images_dir = job_root / "refined" / "local" / scan_id / "dense" / "images"
    colmap_dir = job_root / "refined" / "local" / scan_id / "dense" / "sparse"
    if reuse_trained and (splat_dir / f"{splat_filename}.ply").exists():
        print("Splat reuse enabled, skip ahead to filtering")
    else:
        preprocessing.main(job_root, scan_id)

        train_splat(colmap_dir, splat_dir, images_dir, iterations=iterations)

    filtered_ply = splat_dir / f"{splat_filename}.filtered.ply"
    filter_args = argparse.Namespace(
        input_ply=splat_dir / f"{splat_filename}.ply",
        output_ply=filtered_ply,
        colmap_path=colmap_dir,
        overwrite=True,
        min_opacity=-3.5,
        max_size=1.5,
        min_size=0.001,
        min_view_count=3,
        view_min_depth=0.1,
        view_max_depth=5.0,
        outlier_radius=None,
    )

    filter_splats.main(filter_args)

    if not filtered_ply.exists():
        raise FileNotFoundError(f"Filtered PLY was not produced: {filtered_ply}")

    if do_convert_splat:
        convert_splat.convert_ply_to_splat(filtered_ply)
    if do_convert_sog:
        convert_splat.convert_ply_to_sog(filtered_ply)

    end_time = time.time()
    print(f"Local splatting DONE in {end_time - start_time} seconds: {scan_id}")


def main(
    job_root: Path,
    scan_ids: list[str],
    iterations: int = 30000,
    reuse_trained: bool = False,
    do_convert_splat: bool = False,
    do_convert_sog: bool = False,
) -> None:
    for scan_id in scan_ids:
        scan_main(
            job_root,
            scan_id,
            iterations=iterations,
            reuse_trained=reuse_trained,
            do_convert_splat=do_convert_splat,
            do_convert_sog=do_convert_sog,
        )


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--job-root", type=Path, required=True)
    parser.add_argument("--scan-id", type=str, default=None)
    parser.add_argument("--all-scan-ids", action="store_true", default=False)
    parser.add_argument("--iterations", type=int, default=30000)
    parser.add_argument("--reuse-trained", action="store_true", default=False)
    parser.add_argument(
        "--convert-splat",
        action="store_true",
        default=False,
        help="Also output as .splat format",
    )
    parser.add_argument(
        "--convert-sog",
        action="store_true",
        default=False,
        help="Also output as .sog format",
    )

    args = parser.parse_args()
    print(f"args: {args}")

    if not args.scan_id and not args.all_scan_ids:
        parser.error("Either --scan-id or --all-scan-ids is required")

    if args.all_scan_ids:
        scan_ids = [f.name for f in (args.job_root / "datasets").iterdir() if f.is_dir()]
    else:
        scan_ids = [args.scan_id]
    main(
        job_root=args.job_root,
        scan_ids=scan_ids,
        iterations=args.iterations,
        reuse_trained=args.reuse_trained,
        do_convert_splat=args.convert_splat,
        do_convert_sog=args.convert_sog,
    )
