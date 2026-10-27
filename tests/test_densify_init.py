"""Unit tests for densify staging helpers (no GPU / plugin required)."""

from __future__ import annotations

import shutil
import tempfile
import unittest
from pathlib import Path

import densify_init


class TestEnsureColmapSparse0(unittest.TestCase):
    def test_flat_sparse_copied_to_sparse0(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            scene = Path(tmp)
            sparse = scene / "sparse"
            sparse.mkdir()
            (sparse / "cameras.bin").write_bytes(b"cam")
            (sparse / "images.bin").write_bytes(b"img")
            out = densify_init.ensure_colmap_sparse0(scene)
            self.assertEqual(out, sparse / "0")
            self.assertTrue((out / "cameras.bin").is_file())

    def test_existing_sparse0_unchanged(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            scene = Path(tmp)
            sparse0 = scene / "sparse" / "0"
            sparse0.mkdir(parents=True)
            (sparse0 / "cameras.txt").write_text("# stub", encoding="utf-8")
            out = densify_init.ensure_colmap_sparse0(scene)
            self.assertEqual(out, sparse0)


class TestBuildDensifyCliArgs(unittest.TestCase):
    def test_defaults_turbo_and_refs(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            scene = Path(tmp)
            sparse0 = scene / "sparse" / "0"
            sparse0.mkdir(parents=True)
            (sparse0 / "cameras.bin").write_bytes(b"x")
            args = densify_init.build_densify_cli_args(scene)
            self.assertEqual(args.roma_setting, "turbo")
            self.assertAlmostEqual(args.num_refs, 0.6)


if __name__ == "__main__":
    unittest.main()
