"""Static contract checks for v0.2 config-primary train recipe."""

import json
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
TRAIN_HELPER = REPO_ROOT / "scripts" / "train_helper.sh"
LFS_CONFIG = REPO_ROOT / "config" / "lichtfeld_optimization_params.json"

SPARSITY_MARKERS = (
    "SPLATTER_ENABLE_SPARSITY",
    "ENABLE_SPARSITY",
    "--enable-sparsity",
    "popspa",
    "POPSPA",
)


class TrainHelperRecipeContractTest(unittest.TestCase):
    def test_config_recipe_source_of_truth(self):
        data = json.loads(LFS_CONFIG.read_text(encoding="utf-8"))
        opt = data["optimization"]
        self.assertEqual(opt["strategy"], "mrnf")
        self.assertEqual(opt["max_cap"], 1_000_000)
        self.assertEqual(opt["bg_mode"], "random")
        self.assertFalse(opt["bg_modulation"])
        self.assertFalse(opt["use_depth_loss"])
        self.assertTrue(opt["use_normal_loss"])
        self.assertEqual(opt["normal_loss_weight"], 0.05)
        self.assertEqual(opt["normal_consistency_weight"], 0.01)
        self.assertEqual(opt["normal_flatten_weight"], 0.1)
        self.assertTrue(opt["normal_auto_generate"])
        self.assertEqual(opt["max_screen_share"], 0.2)
        self.assertEqual(opt["oversize_split_fraction"], 0.25)
        self.assertEqual(opt["min_opacity"], 0.01)
        for key in (
            "enable_sparsity",
            "popspa_target_count",
            "sparsify_steps",
            "refine_steps",
        ):
            self.assertNotIn(key, opt)

    def test_train_helper_config_primary_argv(self):
        text = TRAIN_HELPER.read_text(encoding="utf-8")
        self.assertIn('--config "${CONFIG}"', text)
        self.assertIn('--output-name "splat_${ITERATIONS}"', text)
        self.assertNotIn("--max-cap", text)
        self.assertNotIn("--strategy", text)
        for marker in SPARSITY_MARKERS:
            self.assertNotIn(marker, text, msg=f"sparsity marker {marker!r} in train_helper")

    def test_product_paths_no_sparsity_wiring(self):
        for rel in ("local_main.py", "splatter_pipeline.py", "global_main.py"):
            text = (REPO_ROOT / rel).read_text(encoding="utf-8")
            for marker in (
                "SPLATTER_ENABLE_SPARSITY",
                "enable_sparsity",
                "--enable-sparsity",
                "popspa",
                "sparsify_steps",
                "--sparsify-steps",
            ):
                self.assertNotIn(
                    marker,
                    text,
                    msg=f"{marker!r} found in {rel}",
                )


if __name__ == "__main__":
    unittest.main()
