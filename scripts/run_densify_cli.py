#!/usr/bin/env python3
"""Headless entry for Lichtfeld-Densification-Plugin ``densify.py`` (RoMa v2)."""

from __future__ import annotations

import os
import sys
import types
from pathlib import Path


def _install_lichtfeld_stub() -> None:
    if "lichtfeld" in sys.modules:
        return
    lf = types.ModuleType("lichtfeld")

    class _Log:
        def info(self, msg: str, *args: object) -> None:
            if args:
                msg = msg % args if "%" in msg else f"{msg} {' '.join(map(str, args))}"
            print(f"[densify] {msg}", flush=True)

        def warn(self, msg: str, *args: object) -> None:
            if args:
                msg = msg % args if "%" in msg else f"{msg} {' '.join(map(str, args))}"
            print(f"[densify][warn] {msg}", flush=True)

    lf.log = _Log()
    sys.modules["lichtfeld"] = lf


def _ensure_plugin_importable(plugin_root: Path) -> None:
    root = plugin_root.resolve()
    if not (root / "densify.py").is_file():
        raise FileNotFoundError(f"expected densify.py under {root}")
    if str(root) not in sys.path:
        sys.path.insert(0, str(root))


def _register_densification_package(plugin_root: Path) -> None:
    import importlib.util

    pkg_name = "densification"
    if pkg_name in sys.modules:
        return
    init_py = plugin_root / "__init__.py"
    if not init_py.is_file():
        raise FileNotFoundError(f"expected __init__.py under {plugin_root}")
    spec = importlib.util.spec_from_file_location(
        pkg_name,
        init_py,
        submodule_search_locations=[str(plugin_root)],
    )
    if spec is None or spec.loader is None:
        raise ImportError(f"cannot load densification package from {plugin_root}")
    pkg = importlib.util.module_from_spec(spec)
    sys.modules[pkg_name] = pkg
    spec.loader.exec_module(pkg)


def _load_densify_api(plugin_root: Path):
    _ensure_plugin_importable(plugin_root)
    try:
        from densification.densify import build_argparser, dense_init  # type: ignore

        return build_argparser, dense_init
    except ImportError:
        _register_densification_package(plugin_root)
        from densification.densify import build_argparser, dense_init  # type: ignore

        return build_argparser, dense_init


def main() -> int:
    _install_lichtfeld_stub()
    plugin_root = Path(os.environ.get("DENSIFY_PLUGIN_ROOT", "/app/Lichtfeld-Densification-Plugin"))
    build_argparser, dense_init = _load_densify_api(plugin_root)
    cli_args = build_argparser().parse_args()
    return int(dense_init(cli_args))


if __name__ == "__main__":
    raise SystemExit(main())
