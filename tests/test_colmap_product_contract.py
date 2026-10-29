"""Domain upload naming for /splatter/colmap/v1 refined .splat."""

import unittest

import artifact_naming


class ColmapProductContractTest(unittest.TestCase):
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
