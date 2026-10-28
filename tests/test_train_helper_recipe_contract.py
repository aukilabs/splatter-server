"""Static contract checks for v0.2 train recipe (max_cap 1M, POPSpa off by default)."""

import json
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
TRAIN_HELPER = REPO_ROOT / "scripts" / "train_helper.sh"
LFS_CONFIG = REPO_ROOT / "config" / "lichtfeld_optimization_params.json"


class TrainHelperRecipeContractTest(unittest.TestCase):
    def test_config_popspa_experiment_knobs(self):
        """Config still holds optional POPSpa phase params when sparsity is enabled."""
        data = json.loads(LFS_CONFIG.read_text(encoding="utf-8"))
        opt = data["optimization"]
        self.assertEqual(opt["popspa_target_count"], 750_000)
        self.assertEqual(opt["sparsify_steps"], 5000)
        self.assertEqual(opt["refine_steps"], 5000)
        # Product train uses random bg so holes don't learn "transparent = black".
        self.assertEqual(opt["bg_mode"], "random")
        self.assertFalse(opt["bg_modulation"])

    def test_train_helper_argv_recipe(self):
        text = TRAIN_HELPER.read_text(encoding="utf-8")
        self.assertIn("--max-cap 1000000", text)
        self.assertNotIn("--max-cap 1500000", text)
        self.assertIn('ENABLE_SPARSITY="${SPLATTER_ENABLE_SPARSITY:-0}"', text)
        self.assertIn("--enable-sparsity", text)
        self.assertIn("--popspa-target-count", text)
        # Stable export stem so POPSpa (+sparsify/refine iters) does not rename PLY.
        self.assertIn('--output-name "splat_${ITERATIONS}"', text)
        self.assertNotIn(
            "enable_sparsity ignored",
            Path(REPO_ROOT / "local_main.py").read_text(encoding="utf-8"),
        )


if __name__ == "__main__":
    unittest.main()
