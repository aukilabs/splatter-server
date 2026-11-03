from pathlib import Path
import cv2
import argparse
import shutil
from copy import deepcopy
import open3d as o3d
import pycolmap
from filter_splats import compute_view_counts


def convertPoint(xyz, col):
    p2 = pycolmap.Point3D()
    p2.xyz = xyz
    p2.color = [int(col[0] * 255), int(col[1] * 255), int(col[2] * 255)]
    return p2


def set_colmap_points_from_pointcloud(rec, pointcloud):
    for i in list(rec.point3D_ids())[::-1]:
        rec.delete_point3D(i)
    for i, (xyz, color) in enumerate(zip(pointcloud.points, pointcloud.colors)):
        rec.points3D[i] = convertPoint(xyz, color)


def colmap_to_o3d_pointcloud(rec):
    pointcloud = o3d.geometry.PointCloud()
    for id in rec.point3D_ids():
        pointcloud.points.append(rec.points3D[id].xyz)
        pointcloud.colors.append(rec.points3D[id].color / 255)
    return pointcloud


def cleanup_rec_points(rec: pycolmap.Reconstruction) -> pycolmap.Reconstruction:
    pointcloud = colmap_to_o3d_pointcloud(rec)
    old_count = len(pointcloud.points)
    pointcloud, _ = pointcloud.remove_statistical_outlier(nb_neighbors=20, std_ratio=2.0)
    print(
        f"[preprocessing] Removed {old_count - len(pointcloud.points)} statistical "
        f"outlier points (remaining {len(pointcloud.points)})"
    )

    old_count = len(pointcloud.points)
    pointcloud, _ = pointcloud.remove_radius_outlier(nb_points=10, radius=0.2)
    print(
        f"[preprocessing] Removed {old_count - len(pointcloud.points)} radius "
        f"outlier points (remaining {len(pointcloud.points)})"
    )

    pointcloud = pointcloud.voxel_down_sample(0.02)
    print(f"[preprocessing] Voxel downsampled pointcloud to {len(pointcloud.points)} points")

    cleaned_rec = deepcopy(rec)
    set_colmap_points_from_pointcloud(cleaned_rec, pointcloud)
    return cleaned_rec


def cleanup_rec_cameras(
    rec: pycolmap.Reconstruction,
    min_3d_points_seen: int = 20,
    view_count_max_depth: float = 3.0,
    blur_threshold: float = 20.0,  # Laplacian variance below this = too blurry
    frames_dir: Path = None,
) -> pycolmap.Reconstruction:
    """Remove cameras with few close-up 3D points, and optionally blurry frames.

    Black occlusion rectangles are handled at train time via ``masks/`` sidecars
    (``black_pixel_masks``), not by dropping whole frames here.
    """

    _, point_counts_per_cam = compute_view_counts(rec, None, 0.1, view_count_max_depth)

    frames_to_remove = []
    for img_id, img in rec.images.items():
        cam_id = img.camera_id
        frame_id = img.frame_id if img.frame is not None else img_id
        if point_counts_per_cam.get(cam_id, 0) < min_3d_points_seen:
            frames_to_remove.append(frame_id)
            continue

        if frames_dir is None or blur_threshold <= 0:
            continue

        image = cv2.imread(str(frames_dir / img.name))
        small_image = cv2.resize(
            image,
            (image.shape[1] // 2, image.shape[0] // 2),
            interpolation=cv2.INTER_NEAREST,
        )
        gray = cv2.cvtColor(small_image, cv2.COLOR_BGR2GRAY)
        laplacian_var = cv2.Laplacian(gray, cv2.CV_64F).var()
        if laplacian_var < blur_threshold:
            print(
                f"[preprocessing] Removing blurry image {img.name} "
                f"(laplacian variance: {laplacian_var:.1f} < {blur_threshold})"
            )
            frames_to_remove.append(frame_id)

    for frame_id in reversed(frames_to_remove):
        rec.deregister_frame(frame_id)

    print(f"[preprocessing] Removed {len(frames_to_remove)} cameras (remaining {len(rec.images)})")
    return rec


def preprocess(
    colmap_dir: Path,
    processed_dir: Path,
    frames_dir: Path = None,
    camera_min_3d_points: int = 50,
    view_count_max_depth: float = 2.5,
):
    rec = pycolmap.Reconstruction()
    rec.read(str(colmap_dir))
    print(f"Loaded colmap reconstruction: {rec.summary()}")
    print(rec.summary())

    rec = cleanup_rec_cameras(
        rec, camera_min_3d_points, view_count_max_depth, frames_dir=frames_dir
    )
    rec = cleanup_rec_points(rec)

    rec.write(str(processed_dir))
    print(f"Cleaned colmap reconstruction written to: {processed_dir}")
    print(rec.summary())

def scan_main(job_root: Path, scan_id: str):
    print(f"Processing scan {scan_id}...")

    frames_dir = job_root / "datasets" / scan_id / "Frames"
    if not frames_dir.exists():
        raise FileNotFoundError(f"Frames directory not found: {frames_dir}")
    
    colmap_dir = job_root / "refined" / "local" / scan_id / "sfm"
    dense_dir = job_root / "refined" / "local" / scan_id / "dense"

    processed_dir = job_root / "refined" / "local" / scan_id / "processed"
    if not processed_dir.exists():
        processed_dir.mkdir(parents=True, exist_ok=True)
    
    output_dir = job_root / "refined" / "local" / scan_id / "splat"
    shutil.rmtree(dense_dir, ignore_errors=True)
    shutil.rmtree(output_dir, ignore_errors=True)
    if not output_dir.exists():
        output_dir.mkdir(parents=True, exist_ok=True)

    if not dense_dir.exists():
        dense_dir.mkdir(parents=True, exist_ok=True)

    preprocess(colmap_dir, processed_dir, frames_dir=frames_dir)
    pycolmap.undistort_images(
        output_path=str(dense_dir),
        input_path=str(processed_dir),
        image_path=str(frames_dir)
    )

def main(job_root: Path, scan_id: str = None):
    if scan_id is None:
        scan_ids = [f.name for f in (job_root / "datasets").iterdir() if f.is_dir()]
        print(f"--scan-ids not provided, processing all {len(scan_ids)} scans in the dataset directory")
        for scan_id in scan_ids:
            scan_main(job_root, scan_id)
    else:
        scan_main(job_root, scan_id)

if __name__ == "__main__":
    args = argparse.ArgumentParser()
    args.add_argument("--job-root", type=Path, required=True)
    args.add_argument("--scan-id", type=str, default=None)

    args = args.parse_args()

    main(args.job_root, args.scan_id)

