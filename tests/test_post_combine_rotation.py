"""Regression coverage for the single post-combine domain rotation."""

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import numpy as np
from plyfile import PlyData, PlyElement

import global_main


class PostCombineRotationTest(unittest.TestCase):
    def test_main_rotates_combined_ply_by_minus_90_z_before_lod(self):
        dtype = np.dtype([
            ("x", "f4"), ("y", "f4"), ("z", "f4"),
            ("scale_0", "f4"), ("scale_1", "f4"), ("scale_2", "f4"),
            ("rot_0", "f4"), ("rot_1", "f4"), ("rot_2", "f4"), ("rot_3", "f4"),
        ])
        vertices = np.array(
            [(1.0, 0.0, 2.0, -2.0, -1.0, -0.5, 1.0, 0.0, 0.0, 0.0)],
            dtype=dtype,
        )

        with tempfile.TemporaryDirectory() as tmp:
            combined_path = Path(tmp) / "combined_splat.ply"
            PlyData([PlyElement.describe(vertices, "vertex")], text=False).write(str(combined_path))
            observed_at_lod = []
            stage_order = []

            def combine_stub(*args, **kwargs):
                stage_order.append("combine")
                return combined_path

            def split_stub(path):
                stage_order.append("lod")
                observed_at_lod.append(PlyData.read(str(path))["vertex"].data.copy())
                return Path(tmp) / "coarse.ply", Path(tmp) / "fine.ply"

            def convert_stub(*args, **kwargs):
                stage_order.append("convert")

            with patch.object(global_main, "combine", side_effect=combine_stub), \
                 patch.object(global_main, "split_coarse_fine", side_effect=split_stub), \
                 patch.object(global_main, "convert_to_sog", side_effect=convert_stub):
                global_main.main(Path(tmp), ["scan"], do_partition=False)

        self.assertEqual(stage_order, ["combine", "lod", "convert"])
        self.assertEqual(len(observed_at_lod), 1)
        out = observed_at_lod[0][0]
        self.assertAlmostEqual(float(out["x"]), 0.0, places=5)
        self.assertAlmostEqual(float(out["y"]), -1.0, places=5)
        self.assertAlmostEqual(float(out["z"]), 2.0, places=5)
        self.assertAlmostEqual(float(out["rot_0"]), 2**-0.5, places=5)
        self.assertAlmostEqual(float(out["rot_1"]), 0.0, places=5)
        self.assertAlmostEqual(float(out["rot_2"]), 0.0, places=5)
        self.assertAlmostEqual(float(out["rot_3"]), -(2**-0.5), places=5)
        for field, expected in (("scale_0", -2.0), ("scale_1", -1.0), ("scale_2", -0.5)):
            self.assertAlmostEqual(float(out[field]), expected, places=5)


if __name__ == "__main__":
    unittest.main()
