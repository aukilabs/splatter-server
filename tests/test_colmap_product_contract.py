"""Static contract checks for S7 / colmap .splat product path (V4 colmap)."""

import unittest
from pathlib import Path

import artifact_naming


COLMAP_PRODUCT_SPLAT = Path("refined") / "splatter" / "splat_rot.splat"


class ColmapProductContractTest(unittest.TestCase):
    def test_colmap_upload_splat_relative_path(self):
        self.assertEqual(COLMAP_PRODUCT_SPLAT.name, "splat_rot.splat")
        self.assertEqual(
            COLMAP_PRODUCT_SPLAT.as_posix(),
            "refined/splatter/splat_rot.splat",
        )

    def test_refined_splat_upload_name_with_suffix(self):
        self.assertEqual(
            artifact_naming.colmap_refined_splat_upload_name("2026-09-17_14-55-56"),
            "refined_splat_2026-09-17_14-55-56",
        )

    def test_refined_splat_upload_name_with_underscore_prefix(self):
        self.assertEqual(
            artifact_naming.colmap_refined_splat_upload_name("_2026-09-17_14-55-56"),
            "refined_splat_2026-09-17_14-55-56",
        )

    def test_refined_splat_upload_name_missing_suffix(self):
        self.assertEqual(
            artifact_naming.colmap_refined_splat_upload_name(None),
            "refined_splat",
        )
        self.assertEqual(artifact_naming.colmap_refined_splat_upload_name(""), "refined_splat")


if __name__ == "__main__":
    unittest.main()
