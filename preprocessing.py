from pathlib import Path
import cv2
import argparse
import shutil
import os
from subprocess import run
from copy import deepcopy
import open3d as o3d
import numpy as np
import pycolmap
from filter_splats import compute_view_counts

def convertPoint(xyz, col):
    p2 = pycolmap.Point3D()
    p2.xyz = xyz
    p2.color = [int(col[0]*255), int(col[1]*255), int(col[2]*255)]
    return p2

def set_colmap_points_from_pointcloud(rec, pointcloud):
    # Delete old points
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


def cleanup_rec_points(
    rec: pycolmap.Reconstruction,
    min_view_count: int = 2,
    view_count_max_depth: float = 3.0
) -> pycolmap.Reconstruction:
    pointcloud = colmap_to_o3d_pointcloud(rec)
    old_count = len(pointcloud.points)
    pointcloud, _ = pointcloud.remove_statistical_outlier(nb_neighbors=20, std_ratio=2.0)
    print(f"[preprocessing] Removed {old_count - len(pointcloud.points)} statistical outlier points (remaining {len(pointcloud.points)})")

    old_count = len(pointcloud.points)
    pointcloud, _ = pointcloud.remove_radius_outlier(nb_points=10, radius=0.2)
    print(f"[preprocessing] Removed {old_count - len(pointcloud.points)} radius outlier points (remaining {len(pointcloud.points)})")
    
    # Only keep points seen close-up by several cameras (similar as we do in post-processing too)
    """
    xyz = np.column_stack([p.xyz for p in pointcloud.points])
    view_counts, _ = compute_view_counts(rec, xyz, 0.1, view_count_max_depth)
    view_mask = view_counts >= min_view_count
    xyz_filtered = xyz[view_mask]
    print(f"[preprocessing] Removed {len(xyz) - len(xyz_filtered)} points with too few close-range views (remaining {len(xyz_filtered)})")
    pointcloud.points.clear()
    for p in xyz_filtered:
        pointcloud.points.append(p)
    """

    pointcloud = pointcloud.voxel_down_sample(0.02)
    print(f"[preprocessing] Voxel downsampled pointcloud to {len(pointcloud.points)} points")

    cleaned_rec = deepcopy(rec)
    set_colmap_points_from_pointcloud(cleaned_rec, pointcloud)

    return cleaned_rec


def cleanup_rec_cameras(
    rec: pycolmap.Reconstruction,
    min_3d_points_seen: int = 20,
    view_count_max_depth: float = 3.0,
    black_mask_threshold: float = -1.0,  # disabled: black rects use train masks/ sidecars (v0.2)
    blur_threshold: float = 20.0, # Laplacian variance below this = too blurry
    frames_dir: Path = None
) -> pycolmap.Reconstruction:
    """Remove cameras with few 3D points seen close-up"""

    _, point_counts_per_cam = compute_view_counts(rec, None, 0.1, view_count_max_depth)
    
    # Find images to remove (cameras with too few points seen)
    frames_to_remove = []
    for img_id, img in rec.images.items():
        cam_id = img.camera_id
        frame_id = img.frame_id if img.frame is not None else img_id
        if point_counts_per_cam.get(cam_id, 0) < min_3d_points_seen:
            frames_to_remove.append(frame_id)
        elif frames_dir is not None:
            if black_mask_threshold < 0 and blur_threshold < 0:
                continue
            
            image = cv2.imread(str(frames_dir / img.name))
            small_image = cv2.resize(image, (image.shape[1] // 2, image.shape[0] // 2), interpolation=cv2.INTER_NEAREST)

            if black_mask_threshold > 0:
                mask = small_image == [0, 0, 0]
                if np.mean(mask) > black_mask_threshold:
                    print(f"[preprocessing] Removing image {img.name} with {100*np.mean(mask):.1f}% black pixels")
                    frames_to_remove.append(frame_id)
                    continue

            if blur_threshold > 0:
                gray = cv2.cvtColor(small_image, cv2.COLOR_BGR2GRAY)
                laplacian_var = cv2.Laplacian(gray, cv2.CV_64F).var()
                if laplacian_var < blur_threshold:
                    print(f"[preprocessing] Removing blurry image {img.name} (laplacian variance: {laplacian_var:.1f} < {blur_threshold})")
                    frames_to_remove.append(frame_id)
    
    # Deregister images in reverse order to avoid invalidating iterators
    for frame_id in reversed(frames_to_remove):
        rec.deregister_frame(frame_id)
    
    print(f"[preprocessing] Removed {len(frames_to_remove)} cameras (remaining {len(rec.images)})")
    return rec

def voxelgrid_to_pointcloud(vg: o3d.geometry.VoxelGrid) -> o3d.geometry.PointCloud:
    """Convert voxel grid to point cloud"""
    voxels = vg.get_voxels()
    if len(voxels) == 0:
        return o3d.geometry.PointCloud()

    pts = np.empty((len(voxels), 3), dtype=np.float64)
    cols = np.empty((len(voxels), 3), dtype=np.float64)

    for i, v in enumerate(voxels):
        # center coordinate in world space
        pts[i] = vg.get_voxel_center_coordinate(v.grid_index)
        cols[i] = v.color  # (r,g,b) in [0,1] if colors exist; otherwise often (0,0,0)

    pcd = o3d.geometry.PointCloud()
    pcd.points = o3d.utility.Vector3dVector(pts)

    # Only assign colors if they look meaningful
    if np.any(cols != 0):
        pcd.colors = o3d.utility.Vector3dVector(cols)

    return pcd


def snap_mesh_to_pointcloud(mesh: o3d.geometry.TriangleMesh, pointcloud: o3d.geometry.PointCloud, tree: o3d.geometry.KDTreeFlann, strength: float = 1.0, knn: int = 10) -> o3d.geometry.TriangleMesh:
    """Snap mesh to pointcloud"""
    verts = np.asarray(mesh.vertices)
    points = np.asarray(pointcloud.points)
    for i, vert in enumerate(verts):
        [k, idx, _] = tree.search_knn_vector_3d(vert, knn)
        if k > 0:
            mean_neighbor = np.mean(np.array(points[idx[1:]]), axis=0)
            vert = vert + strength * (mean_neighbor - vert)
            mesh.vertices[i] = vert
    return mesh

def rec_points_from_alphashape(rec: pycolmap.Reconstruction) -> pycolmap.Reconstruction:
    pointcloud = colmap_to_o3d_pointcloud(rec)
    #pointcloud, _ = pointcloud.remove_statistical_outlier(nb_neighbors=20, std_ratio=1.5)
    
    # mesh using alphashape
    # big enough alpha to close gaps between points
    mesh = o3d.geometry.TriangleMesh.create_from_point_cloud_alpha_shape(pointcloud, alpha=0.2)
    mesh = mesh.filter_smooth_taubin(number_of_iterations=1)
    #mesh = o3d.geometry.TriangleMesh.create_from_point_cloud_ball_pivoting(pointcloud, radii=o3d.utility.DoubleVector([0.05, 0.1, 0.2, 0.4]))
    #save to disk for debugging
    o3d.io.write_triangle_mesh("alphashape.ply", mesh)
    print(f"Alphashape mesh saved to: alphashape.ply")

    tree = o3d.geometry.KDTreeFlann(pointcloud)
    # increase triangle count and snap to original point cloud to recover fine details
    # mesh to voxelized points
    mesh = snap_mesh_to_pointcloud(mesh, pointcloud, tree, strength=0.5, knn=10)
    o3d.io.write_triangle_mesh("alphashape_snap.ply", mesh)
    mesh = mesh.subdivide_midpoint()
    o3d.io.write_triangle_mesh("alphashape_snap_subdivide.ply", mesh)
    print(f"Alphashape snap subdivide mesh saved to: alphashape_snap_subdivide.ply")
    mesh = snap_mesh_to_pointcloud(mesh, pointcloud, tree, strength=0.5, knn=10)
    o3d.io.write_triangle_mesh("alphashape_snap_subdivide_snap.ply", mesh)
    print(f"Alphashape snap subdivide snap mesh saved to: alphashape_snap_subdivide_snap.ply")

    voxel_grid = o3d.geometry.VoxelGrid.create_from_triangle_mesh(mesh, voxel_size=0.05)
    print(f"number of voxels: {len(voxel_grid.get_voxels())}")
    new_pointcloud = voxelgrid_to_pointcloud(voxel_grid)

    # For new point cloud, pick color from nearest points in original point cloud
    # also "snap" point to be near original point (since alphashape has lower res and loses some fine details)
    new_pointcloud.colors = o3d.utility.Vector3dVector([[0, 0, 0] for _ in range(len(new_pointcloud.points))])
    for i, point in enumerate(new_pointcloud.points):
        [k, idx, _] = tree.search_knn_vector_3d(point, 1)
        if k > 0:
            new_pointcloud.colors[i] = pointcloud.colors[idx[0]]
        else:
            new_pointcloud.colors[i] = [0, 0, 0]

    print(f"number of points NEW: {len(new_pointcloud.points)}")
    print(f"number of colors NEW: {len(new_pointcloud.colors)}")

    #replace points
    new_rec = deepcopy(rec)
    set_colmap_points_from_pointcloud(new_rec, new_pointcloud)
    print(f"New reconstruction points: {len(new_rec.points3D)}")

    return new_rec

def remove_masked_images(rec: pycolmap.Reconstruction) -> pycolmap.Reconstruction:
    """Remove images with masked pixels"""
    for img_id in rec.images:
        img = rec.images[img_id]
        if img.mask is not None:
            rec.deregister_frame(img_id)
    return rec


def run_bundle_adjustment(rec: pycolmap.Reconstruction, refine_camera_poses: bool = True) -> pycolmap.Reconstruction:
    """Run bundle adjustment with fixed poses, only refining intrinsics (focal length and radial distortion)."""
    # Convert cameras from SIMPLE_PINHOLE/PINHOLE to SIMPLE_RADIAL/RADIAL to allow distortion refinement
    converted_counts = {}
    for cam_id in rec.cameras:
        cam = rec.cameras[cam_id]
        old_model = cam.model
        if old_model == pycolmap.CameraModelId.SIMPLE_RADIAL or old_model == pycolmap.CameraModelId.RADIAL:
            print(f"Camera {cam_id} is already in SIMPLE_RADIAL model, skipping")
            continue
        
        if old_model == pycolmap.CameraModelId.SIMPLE_PINHOLE:
            f, cx, cy = cam.params
            new_model = pycolmap.CameraModelId.SIMPLE_RADIAL
            params = [f, cx, cy, 0.0]
        elif old_model == pycolmap.CameraModelId.PINHOLE:
            fx, fy, cx, cy = cam.params
            new_model = pycolmap.CameraModelId.RADIAL
            params = [fx, fy, cx, cy, 0.0]
        else:
            raise ValueError(f"Unsupported camera model: {old_model}")
        new_cam = pycolmap.Camera(
            model=new_model,
            width=cam.width,
            height=cam.height,
            params=params,
            camera_id=cam_id
        )
        rec.cameras[cam_id] = new_cam

        if (old_model, new_model) not in converted_counts:
            converted_counts[(old_model, new_model)] = 0
        converted_counts[(old_model, new_model)] += 1
    
    for (old_model, new_model), count in converted_counts.items():
        print(f"Converted {count} cameras from {old_model} to {new_model}")

    ba_options = pycolmap.BundleAdjustmentOptions()
    ba_options.refine_focal_length = True
    ba_options.refine_principal_point = False  # Keep cx, cy fixed
    ba_options.refine_extra_params = True  # Refine radial distortion k
    ba_options.refine_rig_from_world = refine_camera_poses
    ba_options.solver_options.max_num_iterations = 20  # Few iterations, stay close to original
    
    pycolmap.bundle_adjustment(rec, ba_options)
    print(f"Bundle adjustment complete: {rec.summary()}")
    return rec


def preprocess(
    colmap_dir: Path, processed_dir: Path, frames_dir: Path = None, bundle_adjust: bool = False,
    camera_min_3d_points: int = 50, # Remove images/cams which see few points (after outlier filtering)
    point_min_view_count: int = 4,
    view_count_max_depth: float = 2.5, # Used for both point and camera filtering
):
    rec = pycolmap.Reconstruction()
    rec.read(str(colmap_dir))
    print(f"Loaded colmap reconstruction: {rec.summary()}")
    print(rec.summary())

    if bundle_adjust:
        rec = run_bundle_adjustment(rec, refine_camera_poses=False)

    rec = cleanup_rec_cameras(rec, camera_min_3d_points, view_count_max_depth, frames_dir=frames_dir)
    rec = cleanup_rec_points(rec, point_min_view_count, view_count_max_depth)
    #rec = rec_points_from_alphashape(rec)

    rec.write(str(processed_dir))
    print(f"Cleaned colmap reconstruction written to: {processed_dir}")
    print(rec.summary())

def undistort_local_scan(job_root: Path, scan_id: str) -> None:
    """Undistort refined SFM into ``dense/`` without sparse point outlier cleanup (densify path)."""
    print(f"Undistort-only staging for scan {scan_id} (densify path)...")
    frames_dir = job_root / "datasets" / scan_id / "Frames"
    if not frames_dir.exists():
        raise FileNotFoundError(f"Frames directory not found: {frames_dir}")

    colmap_dir = job_root / "refined" / "local" / scan_id / "sfm"
    dense_dir = job_root / "refined" / "local" / scan_id / "dense"
    processed_dir = job_root / "refined" / "local" / scan_id / "processed"
    processed_dir.mkdir(parents=True, exist_ok=True)
    shutil.rmtree(dense_dir, ignore_errors=True)
    dense_dir.mkdir(parents=True, exist_ok=True)

    rec = pycolmap.Reconstruction()
    rec.read(str(colmap_dir))
    rec = cleanup_rec_cameras(rec, min_3d_points_seen=20, view_count_max_depth=3.0, frames_dir=frames_dir)
    rec.write(str(processed_dir))
    pycolmap.undistort_images(
        output_path=str(dense_dir),
        input_path=str(processed_dir),
        image_path=str(frames_dir),
    )


def undistort_merged_colmap(
    colmap_dir: Path,
    processed_dir: Path,
    frames_dir: Path,
    dense_dir: Path,
) -> None:
    """Undistort merged global COLMAP for colmap/v1 without sparse point cleanup."""
    processed_dir.mkdir(parents=True, exist_ok=True)
    shutil.rmtree(dense_dir, ignore_errors=True)
    dense_dir.mkdir(parents=True, exist_ok=True)

    rec = pycolmap.Reconstruction()
    rec.read(str(colmap_dir))
    rec = cleanup_rec_cameras(rec, min_3d_points_seen=20, view_count_max_depth=3.0, frames_dir=frames_dir)
    rec.write(str(processed_dir))
    pycolmap.undistort_images(
        output_path=str(dense_dir),
        input_path=str(processed_dir),
        image_path=str(frames_dir),
    )


def scan_main(job_root: Path, scan_id: str, bundle_adjust: bool = False):
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

    preprocess(colmap_dir, processed_dir, frames_dir=frames_dir, bundle_adjust=bundle_adjust)
    pycolmap.undistort_images(
        output_path=str(dense_dir),
        input_path=str(processed_dir),
        image_path=str(frames_dir)
    )

def main(job_root: Path, scan_id: str = None, bundle_adjust: bool = False):
    if scan_id is None:
        scan_ids = [f.name for f in (job_root / "datasets").iterdir() if f.is_dir()]
        print(f"--scan-ids not provided, processing all {len(scan_ids)} scans in the dataset directory")
        for scan_id in scan_ids:
            scan_main(job_root, scan_id, bundle_adjust=bundle_adjust)
    else:
        scan_main(job_root, scan_id, bundle_adjust=bundle_adjust)

if __name__ == "__main__":
    args = argparse.ArgumentParser()
    args.add_argument("--job-root", type=Path, required=True)
    args.add_argument("--scan-id", type=str, default=None)

    args = args.parse_args()

    main(args.job_root, args.scan_id)

