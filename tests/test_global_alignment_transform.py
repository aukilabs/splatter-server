"""Global combination must apply only the manifest COLMAP Sim3."""

import ast
import pathlib
import unittest


SOURCE = pathlib.Path(__file__).resolve().parents[1] / "combine_splats.py"


class GlobalAlignmentTransformTest(unittest.TestCase):
    def test_global_combine_routes_disable_fixed_pre_rotation(self):
        tree = ast.parse(SOURCE.read_text())
        expected_routes = {"combine_splats_from_job_root", "combine_splats_explicit"}
        found_routes = set()

        for node in tree.body:
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            if node.name not in expected_routes:
                continue
            calls = [
                child for child in ast.walk(node)
                if isinstance(child, ast.Call)
                and isinstance(child.func, ast.Name)
                and child.func.id == "load_transform"
            ]
            self.assertTrue(calls, f"{node.name} must load its alignment Sim3")
            for call in calls:
                arg = next(
                    (kw.value for kw in call.keywords if kw.arg == "apply_pre_rotation"),
                    None,
                )
                self.assertIsInstance(arg, ast.Constant, f"{node.name} must explicitly set pre-rotation")
                self.assertIs(arg.value, False, f"{node.name} must not add a fixed pre-rotation")
            found_routes.add(node.name)

        self.assertEqual(found_routes, expected_routes)


if __name__ == "__main__":
    unittest.main()
