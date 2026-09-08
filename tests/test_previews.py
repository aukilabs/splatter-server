"""Offline preview contract tests; these do not replace a real GPU render."""

import importlib.util
import itertools
import json
import math
from fractions import Fraction
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("splatter_pipeline", ROOT / "run.py")
pipeline = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(pipeline)
FFMPEG = shutil.which("ffmpeg")
FFPROBE = shutil.which("ffprobe")


def dot(a, b):
    return sum(x * y for x, y in zip(a, b))


def columns(matrix):
    return [[matrix[row][column] for row in range(3)] for column in range(3)]


class PreviewTests(unittest.TestCase):
    def test_camera_looks_toward_target_with_right_handed_basis(self):
        target = [3.0, -2.0, 5.0]
        for eye in ([3, -2, 20], [3, -2, -10], [15, 10, 13]):
            with self.subTest(eye=eye):
                matrix = pipeline._view_matrix(eye, target, [0, 0, 1])
                axes = columns(matrix)
                direction = [target[i] - eye[i] for i in range(3)]
                # Nerfstudio's camera looks down -Z; target must be in front.
                self.assertLess(dot(direction, axes[2]), 0)
                for i, axis in enumerate(axes):
                    self.assertAlmostEqual(dot(axis, axis), 1)
                    for other in axes[i + 1:]:
                        self.assertAlmostEqual(dot(axis, other), 0)
                self.assertAlmostEqual(dot(pipeline._cross(axes[0], axes[1]), axes[2]), 1)
                self.assertEqual([matrix[i][3] for i in range(3)], list(eye))

    def capture_render(self, exit_codes=None, ffmpeg=True, partial_on_failure=False,
                       stale=False, encode_exit_code=0, frame_count=None, real_video=False,
                       still_frame_count=1):
        calls = []
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        preview = root / "refined" / "splatter"
        (preview / "splatfacto").mkdir(parents=True)
        (preview / "splatfacto" / "config.yml").write_text("# synthetic fixture\n")
        if stale:
            for name in ("preview_top.jpg", "preview_angle.jpg", "preview.mp4", "preview.rendering.mp4"):
                (preview / name).write_bytes(b"earlier render")
            (preview / "preview_video_frames").mkdir()
            (preview / "preview_video_frames" / "00000.png").write_bytes(b"earlier frame")
            for name in ("preview_top", "preview_angle"):
                (preview / name).mkdir()
                (preview / name / "00000.jpg").write_bytes(b"earlier staged image")
        exit_codes = iter(exit_codes or [0, 0, 0])

        def render(exe, *args):
            if exe == "ffmpeg":
                # The upload name must remain absent until encoding finishes.
                self.assertFalse((preview / "preview.mp4").exists())
                output = Path(args[-1])
                self.assertEqual(output.name, "preview.rendering.mp4")
                if real_video:
                    result = subprocess.run([FFMPEG, *map(str, args)], capture_output=True, text=True)
                    self.assertEqual(result.returncode, 0, result.stderr)
                    return result.returncode
                if encode_exit_code == 0 or partial_on_failure:
                    output.write_bytes(b"synthetic encoded video")
                return encode_exit_code
            self.assertEqual(exe, "ns-render")
            self.assertEqual(args[0], "camera-path")
            self.assertEqual(len(args[1:]) % 2, 0)
            options = dict(zip(args[1::2], args[2::2]))
            self.assertNotIn("--renderer", options)
            self.assertIn(options["--image-format"], ("jpeg", "png"))
            self.assertIn(options["--output-format"], ("images", "video"))
            camera_path = json.loads(Path(options["--camera-path-filename"]).read_text())
            self.assertEqual(camera_path["camera_type"], "perspective")
            self.assertGreater(camera_path["render_height"], 0)
            self.assertGreater(camera_path["render_width"], 0)
            self.assertGreater(camera_path["seconds"], 0)
            self.assertNotIn("frames", camera_path)
            for camera in camera_path["camera_path"]:
                self.assertEqual(len(camera["camera_to_world"]), 16)
                self.assertTrue(all(math.isfinite(v) for v in camera["camera_to_world"]))
                self.assertGreater(camera["fov"], 0)
            calls.append((options, camera_path))
            code = next(exit_codes)
            if code == 0 or partial_on_failure:
                output = Path(options["--output-path"])
                is_orbit = len(camera_path["camera_path"]) > 1
                if real_video and is_orbit:
                    self.assertEqual(options["--output-format"], "images")
                    self.assertEqual(options["--image-format"], "png")
                if options["--output-format"] == "images":
                    output.mkdir(exist_ok=True)
                    count = len(camera_path["camera_path"]) if is_orbit else still_frame_count
                    if frame_count is not None:
                        count = min(count, frame_count)
                    if real_video and is_orbit:
                        width = int(camera_path["render_width"])
                        height = int(camera_path["render_height"])
                        source = f"testsrc2=size={width}x{height}:rate={camera_path['fps']}"
                        subprocess.run([
                            FFMPEG, "-hide_banner", "-loglevel", "error", "-nostdin",
                            "-f", "lavfi", "-i", source, "-frames:v", str(count),
                            "-threads", "1", "-start_number", "0", str(output / "%05d.png"),
                        ], check=True, capture_output=True)
                    else:
                        suffix = "png" if options["--image-format"] == "png" else "jpg"
                        for index in range(count):
                            (output / f"{index:05d}.{suffix}").write_bytes(b"synthetic renderer output")
                else:
                    output.write_bytes(b"synthetic renderer output")
            return code

        def which(exe):
            return None if exe == "ffmpeg" and not ffmpeg else "/synthetic/bin/" + exe

        with mock.patch.object(pipeline, "_load_ply_bounds", return_value=([3.0, -2.0, 5.0], 10.0)), \
             mock.patch.object(pipeline.shutil, "which", side_effect=which), \
             mock.patch.object(pipeline, "run_script", side_effect=render):
            pipeline._render_previews(root)
        return preview, calls

    def test_three_artifacts_use_renderer_camera_path_contract(self):
        preview, calls = self.capture_render()
        self.assertEqual(len(calls), 3)
        for name in ("preview_top.jpg", "preview_angle.jpg", "preview.mp4"):
            self.assertTrue((preview / name).is_file(), name)
        self.assertFalse((preview / "preview_top").exists())
        self.assertFalse((preview / "preview_angle").exists())
        self.assertFalse((preview / "preview_video_frames").exists())
        self.assertFalse((preview / "preview.rendering.mp4").exists())

    def test_preview_views_frame_bounds_and_orbit_stays_elevated(self):
        _, calls = self.capture_render()
        target = [3.0, -2.0, 5.0]
        corners = [[target[i] + offset[i] for i in range(3)]
                   for offset in itertools.product((-5.0, 5.0), repeat=3)]
        for _, payload in calls:
            for camera in payload["camera_path"]:
                flat = camera["camera_to_world"]
                matrix = [flat[i:i + 4] for i in range(0, 16, 4)]
                axes = columns(matrix)
                eye = [matrix[i][3] for i in range(3)]
                half_vertical = math.tan(math.radians(camera["fov"]) / 2)
                half_horizontal = half_vertical * payload["render_width"] / payload["render_height"]
                for corner in corners:
                    relative = [corner[i] - eye[i] for i in range(3)]
                    depth = -dot(relative, axes[2])
                    self.assertGreater(depth, 0, "scene is behind the preview camera")
                    self.assertLessEqual(abs(dot(relative, axes[0])) / depth, half_horizontal)
                    self.assertLessEqual(abs(dot(relative, axes[1])) / depth, half_vertical)

        _, video = calls[2]
        self.assertGreaterEqual(video["seconds"], 5)
        self.assertLessEqual(video["seconds"], 10)
        self.assertEqual(len(video["camera_path"]) / video["seconds"], 30)
        positions = [entry["camera_to_world"] for entry in video["camera_path"]]
        for flat in positions:
            delta = [flat[i * 4 + 3] - target[i] for i in range(3)]
            elevation = math.degrees(math.atan2(delta[2], math.hypot(delta[0], delta[1])))
            self.assertGreaterEqual(elevation, 30)
            self.assertLessEqual(elevation, 45)
        angles = [math.atan2(flat[7] - target[1], flat[3] - target[0]) for flat in positions]
        swept = sum((b - a) % (2 * math.pi) for a, b in zip(angles, angles[1:]))
        self.assertGreaterEqual(math.degrees(swept), 270)

    def test_render_failure_does_not_suppress_other_previews(self):
        preview, calls = self.capture_render(exit_codes=[2, 0, 0])
        self.assertEqual(len(calls), 3)
        self.assertFalse((preview / "preview_top.jpg").exists())
        self.assertTrue((preview / "preview_angle.jpg").exists())
        self.assertTrue((preview / "preview.mp4").exists())

    def test_image_promotion_failure_does_not_publish_partial_or_stop_later_previews(self):
        copy_image = shutil.copy2
        replace_image = Path.replace

        def fail_copy(source, destination, *args, **kwargs):
            if Path(destination).name == "preview_top.jpg":
                Path(destination).write_bytes(b"partial image")
                raise OSError("simulated interrupted image copy")
            return copy_image(source, destination, *args, **kwargs)

        def fail_replace(source, destination):
            if Path(destination).name == "preview_top.jpg":
                raise OSError("simulated image rename failure")
            return replace_image(source, destination)

        # Reproduce the original interrupted copy, and exercise the replacement
        # failure after switching publication to an atomic rename.
        with mock.patch.object(pipeline.shutil, "copy2", side_effect=fail_copy), \
             mock.patch.object(Path, "replace", new=fail_replace):
            preview, calls = self.capture_render()
        self.assertEqual(len(calls), 3)
        self.assertFalse((preview / "preview_top.jpg").exists())
        self.assertFalse((preview / "preview_top").exists())
        self.assertTrue((preview / "preview_angle.jpg").is_file())
        self.assertTrue((preview / "preview.mp4").is_file())

    def test_retry_does_not_publish_old_staged_images_when_renderer_produces_none(self):
        preview, calls = self.capture_render(stale=True, still_frame_count=0)
        self.assertEqual(len(calls), 3)
        for name in ("preview_top", "preview_angle"):
            self.assertFalse((preview / name).exists())
            self.assertFalse((preview / f"{name}.jpg").exists())
        self.assertTrue((preview / "preview.mp4").is_file())

    def test_images_still_render_without_ffmpeg(self):
        preview, calls = self.capture_render(ffmpeg=False)
        self.assertEqual(len(calls), 2)
        self.assertTrue((preview / "preview_top.jpg").exists())
        self.assertTrue((preview / "preview_angle.jpg").exists())
        self.assertFalse((preview / "preview.mp4").exists())

    def test_failed_video_does_not_leave_a_partial_upload_candidate(self):
        preview, calls = self.capture_render(exit_codes=[0, 0, 2], partial_on_failure=True)
        self.assertEqual(len(calls), 3)
        self.assertTrue((preview / "preview_top.jpg").is_file())
        self.assertTrue((preview / "preview_angle.jpg").is_file())
        self.assertFalse((preview / "preview.mp4").exists())
        self.assertFalse((preview / "preview_video_frames").exists())
        self.assertFalse((preview / "preview.rendering.mp4").exists())

    def test_encoder_failure_keeps_images_and_removes_partial_video(self):
        preview, calls = self.capture_render(encode_exit_code=2, partial_on_failure=True)
        self.assertEqual(len(calls), 3)
        self.assertTrue((preview / "preview_top.jpg").is_file())
        self.assertTrue((preview / "preview_angle.jpg").is_file())
        self.assertFalse((preview / "preview.mp4").exists())
        self.assertFalse((preview / "preview_video_frames").exists())
        self.assertFalse((preview / "preview.rendering.mp4").exists())

    def test_incomplete_orbit_does_not_publish_a_short_video(self):
        preview, calls = self.capture_render(frame_count=149)
        self.assertEqual(len(calls), 3)
        self.assertFalse((preview / "preview.mp4").exists())
        self.assertFalse((preview / "preview_video_frames").exists())

    @unittest.skipUnless(FFMPEG and FFPROBE, "requires ffmpeg and ffprobe for real MP4 validation")
    def test_real_encoder_produces_five_second_h264_mp4_with_fast_start(self):
        # Only ns-render is simulated: FFmpeg makes synthetic PNGs and the
        # production encoding command makes the MP4, which ffprobe decodes.
        preview, calls = self.capture_render(real_video=True)
        video = preview / "preview.mp4"
        result = subprocess.run([
            FFPROBE, "-v", "error", "-count_frames", "-show_streams",
            "-show_format", "-of", "json", str(video),
        ], check=True, capture_output=True, text=True)
        metadata = json.loads(result.stdout)
        self.assertEqual(len(metadata["streams"]), 1)
        stream = metadata["streams"][0]
        self.assertEqual(stream["codec_name"], "h264")
        self.assertEqual(stream["pix_fmt"], "yuv420p")
        self.assertEqual((stream["width"], stream["height"]), (1280, 720))
        self.assertEqual(Fraction(stream["avg_frame_rate"]), 30)
        self.assertEqual(int(stream["nb_read_frames"]), 150)
        self.assertAlmostEqual(float(metadata["format"]["duration"]), 5.0, places=3)
        self.assertIn("mp4", metadata["format"]["format_name"].split(","))

        data = video.read_bytes()
        atoms = []
        offset = 0
        while offset < len(data):
            self.assertGreaterEqual(len(data) - offset, 8)
            size = int.from_bytes(data[offset:offset + 4], "big")
            atoms.append(data[offset + 4:offset + 8])
            header_size = 8
            if size == 1:
                header_size = 16
                size = int.from_bytes(data[offset + 8:offset + 16], "big")
            elif size == 0:
                size = len(data) - offset
            self.assertGreaterEqual(size, header_size)
            self.assertLessEqual(offset + size, len(data))
            offset += size
        self.assertEqual(atoms.count(b"moov"), 1)
        self.assertEqual(atoms.count(b"mdat"), 1)
        self.assertLess(atoms.index(b"moov"), atoms.index(b"mdat"))
        self.assertEqual(len(calls), 3)
        self.assertFalse((preview / "preview_video_frames").exists())
        self.assertFalse((preview / "preview.rendering.mp4").exists())

    def test_retry_does_not_reuse_stale_previews_after_failure_or_skip(self):
        preview, calls = self.capture_render(exit_codes=[2, 0], ffmpeg=False, stale=True)
        self.assertEqual(len(calls), 2)
        self.assertFalse((preview / "preview_top.jpg").exists())
        self.assertNotEqual((preview / "preview_angle.jpg").read_bytes(), b"earlier render")
        self.assertFalse((preview / "preview.mp4").exists())
        self.assertFalse((preview / "preview.rendering.mp4").exists())
        self.assertFalse((preview / "preview_video_frames").exists())

    def test_docker_packages_the_pipeline_and_ply_reader(self):
        dockerfile = (ROOT / "Dockerfile").read_text()
        script_copy = next(line for line in dockerfile.splitlines()
                           if line.startswith("COPY run.py "))
        self.assertIn("run.py", script_copy.split()[1:-1])
        for script in script_copy.split()[1:-1]:
            self.assertTrue((ROOT / script).is_file(), script)
        pip_line = next(line for line in dockerfile.splitlines()
                        if line.startswith("RUN python3 -m pip install "))
        self.assertIn("plyfile", pip_line.split())
        self.assertIn("apt-get install -y --no-install-recommends ffmpeg", dockerfile)


if __name__ == "__main__":
    unittest.main()
