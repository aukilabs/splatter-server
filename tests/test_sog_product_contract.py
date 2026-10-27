"""Static contract checks for S6 / plan V4 (SOG-only local/global, 10m tiles)."""

import argparse
import unittest

import artifact_naming


class SogProductContractTest(unittest.TestCase):
    def test_global_partition_default_size(self):
        parser = argparse.ArgumentParser()
        parser.add_argument(
            "--partition_size",
            type=float,
            default=float(__import__("os").environ.get("SPLATTER_PARTITION_SIZE", "10")),
        )
        args = parser.parse_args([])
        self.assertEqual(args.partition_size, 10.0)

    def test_sog_partition_rename(self):
        name = artifact_naming.rename_for_domain_upload(
            "coarse_partition_10_2_-2.sog",
            manifest_timestamp="2026-09-17_14-55-56",
            lod_level="coarse",
        )
        self.assertEqual(
            name,
            "splat_partition_coarse_10_2_-2_2026-09-17_14-55-56.splat_partition_sog",
        )

    def test_splat_partition_not_renamed_without_sog_product(self):
        name = artifact_naming.rename_for_domain_upload(
            "coarse_partition_10_2_-2.splat",
            manifest_timestamp="2026-09-17_14-55-56",
            lod_level="coarse",
        )
        self.assertTrue(name.endswith(".splat_partition"))
        self.assertNotIn("_sog", name or "")


if __name__ == "__main__":
    unittest.main()
