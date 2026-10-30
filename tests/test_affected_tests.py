"""``scripts/affected_tests.py`` 的选择逻辑（不真跑 pytest）。

用合成小仓验证三条容易写错的规则：

- 直接 import 命中；
- 沿「谁 import 了它」上溯，但**不穿过枢纽**（被 6+ 个测试 import 的模块）；
- 共享面 / 纯文档 / 无依赖模块分别走全量、守卫、全量兜底。
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

_spec = importlib.util.spec_from_file_location(
    "affected_tests", ROOT / "scripts" / "affected_tests.py"
)
assert _spec and _spec.loader
at = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(at)


def _write(path: Path, text: str = "") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _synthetic(root: Path) -> None:
    _write(root / "myrtui" / "__init__.py")
    _write(root / "myrtui" / "leaf.py")
    _write(root / "myrtui" / "mid.py", "from myrtui.leaf import x\n")
    _write(root / "myrtui" / "hub.py", "from myrtui.mid import x\n")
    _write(root / "myrtui" / "orphan.py")
    _write(root / "tests" / "test_leaf.py", "from myrtui.leaf import x\n")
    _write(root / "tests" / "test_mid.py", "from myrtui.mid import x\n")
    for index in range(6):  # 让 hub 达到「枢纽」门槛
        _write(root / "tests" / f"test_hub{index}.py", "from myrtui.hub import x\n")


def test_leaf_change_reaches_mid_but_not_hub(tmp_path) -> None:
    _synthetic(tmp_path)
    mode, tests, _ = at.plan(["myrtui/leaf.py"], at.build_index(tmp_path))
    assert mode == "tests"
    assert "tests/test_leaf.py" in tests, "直接 import 必须命中"
    assert "tests/test_mid.py" in tests, "上溯一层要算进来"
    assert "tests/test_hub0.py" not in tests, "枢纽不穿过（否则等于全量）"


def test_hub_change_falls_back_to_full(tmp_path) -> None:
    _synthetic(tmp_path)
    mode, _, notes = at.plan(["myrtui/hub.py"], at.build_index(tmp_path))
    assert mode == "full"
    assert any("命中" in note or "共享面" in note for note in notes)


def test_core_and_package_init_force_full(tmp_path) -> None:
    _synthetic(tmp_path)
    index = at.build_index(tmp_path)
    for rel in ("tests/conftest.py", "myrtui/__init__.py", "myrtui/cli.py", "myrtui/i18n.py"):
        mode, _, _ = at.plan([rel], index)
        assert mode == "full", rel


def test_docs_only_runs_guards(tmp_path) -> None:
    _synthetic(tmp_path)
    mode, tests, _ = at.plan(["README.md", "pyproject.toml"], at.build_index(tmp_path))
    assert mode == "tests"
    assert set(tests) == set(at.GUARD_TESTS)


def test_unknown_module_falls_back_to_full(tmp_path) -> None:
    _synthetic(tmp_path)
    mode, _, notes = at.plan(["myrtui/orphan.py"], at.build_index(tmp_path))
    assert mode == "full", notes


def test_scripts_and_unknown_paths_are_skipped(tmp_path) -> None:
    _synthetic(tmp_path)
    mode, tests, notes = at.plan(["scripts/demo.tape", "LICENSE"], at.build_index(tmp_path))
    assert mode == "none" and tests == []
    assert len(notes) == 2


def test_real_repo_smoke() -> None:
    """真仓跑一遍索引：mdpreview → 少数几个，app → 全量。"""
    index = at.build_index(ROOT)
    mode, tests, _ = at.plan(["myrtui/client/mdpreview.py"], index)
    assert mode == "tests"
    assert "tests/test_v36.py" in tests
    assert len(tests) < len(index["direct"]) / 2

    mode, _, _ = at.plan(["myrtui/client/app.py"], index)
    assert mode == "full"
