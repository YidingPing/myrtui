#!/usr/bin/env python3
"""按「改了哪些文件」挑出该跑的测试，避免每次都全量。

用法::

    python scripts/affected_tests.py                  # 只打印要跑什么
    python scripts/affected_tests.py --run            # 直接跑（-n 4）
    python scripts/affected_tests.py --base main      # 指定比较基线（默认 main）
    python scripts/affected_tests.py --run myrtui/i18n.py   # 显式给文件

口径（为什么这样挑，见 tests/README.md「测试策略」）：

- 每个 ``tests/test_*.py`` **直接** import 的 ``myrtui`` 模块记为它的依赖；
- 再沿「谁 import 了它」**上溯两层**，把叶子模块（``cells``、``fileview`` 之类
  没有测试直接 import 的）也算进来；
- 上溯**不穿过枢纽**（被 ≥6 个测试直接 import 的 ``app`` / ``tmux_backend`` /
  ``pane_widget`` / ``client`` / ``file_tree`` / ``topbar``）：否则改任何一个
  app 依赖的模块都会命中 24/30 个测试，等于没筛；
- ``CORE``（共享面 / 包初始化 / ``conftest``）命中就全量；
- 筛完仍 ≥60% 的测试，直接建议全量（省不下时间，还容易误以为「只跑了相关部分」）；
- 纯文档 / 打包配置 → 只跑两条守卫（约 2 秒）；``scripts/`` 不被测试 import。

漏跑一批的代价是 CI 多一轮（CI 仍然全量），不是把坏代码合进 main——
所以这里的策略可以偏保守。
"""
from __future__ import annotations

import argparse
import ast
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

#: 改这些就必须全量（全局共享面 / 索引本身）
CORE = {
    "myrtui/cli.py",
    "myrtui/i18n.py",
    "myrtui/layout_state.py",
    "myrtui/tmux_backend.py",
    "tests/conftest.py",
}

#: 改这些只需要跑守卫（约 2 秒）；新语言/新文案也走这里
GUARD_TESTS = ("tests/test_docs_policy.py", "tests/test_i18n.py")

#: 被这么多个测试直接 import 的模块算「枢纽」，上溯不穿过它
HUB_MIN_TESTS = 6

#: 命中比例超过它就干脆全量（省不下时间）
FULL_RATIO = 0.6

DOC_SUFFIXES = (".md", ".toml")
DOC_PREFIXES = (".github/", ".gitea/")
PYTEST_N = "4"


def _module_name(rel: str) -> str:
    """``myrtui/client/app.py`` → ``myrtui.client.app``；``…/__init__.py`` → 包名。"""
    if rel.endswith("/__init__.py"):
        return rel[: -len("/__init__.py")].replace("/", ".")
    path = rel[: -len(".py")] if rel.endswith(".py") else rel
    return path.replace("/", ".")


def _imports(tree: ast.AST, module_hint: str = "") -> set[str]:
    """模块里 import 的 ``myrtui.*`` 名字。

    ``from myrtui import instances`` / ``from myrtui.client import cells`` 会把
    **子模块名**一并收进来——只记顶层 ``myrtui`` 会漏掉「改 instances 该跑谁」。
    """
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            if node.level:
                base = module_hint.rsplit(".", node.level)[0] if module_hint else ""
                module = f"{base}.{node.module}" if node.module else base
            else:
                module = node.module or ""
            if not module.startswith("myrtui"):
                continue
            found.add(module)
            for alias in node.names:
                found.add(f"{module}.{alias.name}")
        elif isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name.startswith("myrtui"):
                    found.add(alias.name)
    return found


def build_index(root: Path = ROOT) -> dict[str, object]:
    """建索引：测试直接依赖、模块反向依赖、枢纽集合。"""
    graph: dict[str, set[str]] = {}
    for path in sorted((root / "myrtui").rglob("*.py")):
        rel = path.relative_to(root).as_posix()
        name = _module_name(rel)
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except (OSError, SyntaxError):
            continue
        graph[name] = _imports(tree, name)

    direct: dict[str, set[str]] = {}
    for path in sorted((root / "tests").glob("test_*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        direct[path.relative_to(root).as_posix()] = _imports(tree, "tests")

    importers: dict[str, set[str]] = {}
    for module, deps in graph.items():
        for dep in deps:
            importers.setdefault(dep, set()).add(module)

    counts: dict[str, int] = {}
    for deps in direct.values():
        for module in deps:
            counts[module] = counts.get(module, 0) + 1
    hubs = {module for module, count in counts.items() if count >= HUB_MIN_TESTS}

    return {"graph": graph, "direct": direct, "importers": importers, "hubs": hubs}


def _tests_importing(index, module: str) -> set[str]:
    return {test for test, deps in index["direct"].items() if module in deps}


def plan(changed: list[str], index: dict[str, object]) -> tuple[str, list[str], list[str]]:
    """→ ``(mode, tests, notes)``；``mode`` ∈ ``full`` / ``tests`` / ``none``。

    纯函数（只读索引，不碰 git / 文件系统），方便单测。
    """
    importers = index["importers"]
    hubs = index["hubs"]
    tests: set[str] = set()
    notes: list[str] = []
    needs_full = False
    needs_guards = False

    for rel in changed:
        if rel in CORE or rel.startswith("tests/conftest") or rel.endswith("/__init__.py"):
            needs_full = True
            notes.append(f"{rel}: 共享面 / 包初始化 → 全量")
            continue
        if rel.startswith("tests/") and rel.endswith(".py"):
            tests.add(rel)
            continue
        if rel.startswith("myrtui/") and rel.endswith(".py"):
            module = _module_name(rel)
            hit = _tests_importing(index, module)
            frontier = {module}
            for _ in range(2):
                nxt: set[str] = set()
                for node in frontier:
                    for importer in importers.get(node, ()):
                        if importer in hubs:
                            continue
                        nxt.add(importer)
                        hit |= _tests_importing(index, importer)
                frontier = nxt
                if not frontier:
                    break
            if not hit:
                needs_full = True
                notes.append(f"{rel}: 没有任何测试依赖它 → 全量兜底")
            else:
                tests |= hit
            continue
        if rel.startswith("scripts/"):
            notes.append(f"{rel}: 脚本不被测试 import（跳过）")
            continue
        if rel.endswith(DOC_SUFFIXES) or rel.startswith(DOC_PREFIXES) or rel == ".gitignore":
            needs_guards = True
            continue
        notes.append(f"{rel}: 未分类（跳过）")

    total = len(index["direct"])
    if not needs_full and total and len(tests) / total >= FULL_RATIO:
        notes.append(f"{len(tests)}/{total} 个测试命中（≥{FULL_RATIO:.0%}）→ 全量更省事")
        needs_full = True

    if needs_full:
        return "full", [], notes
    if needs_guards:
        tests |= set(GUARD_TESTS)
    if not tests:
        return "none", [], notes
    return "tests", sorted(tests), notes


def changed_files(base: str, explicit: list[str] | None = None) -> list[str]:
    if explicit:
        return sorted(dict.fromkeys(explicit))

    def git(*args: str) -> list[str]:
        out = subprocess.run(
            ["git", *args], cwd=ROOT, capture_output=True, text=True
        ).stdout
        return [line.strip() for line in out.splitlines() if line.strip()]

    files = set(git("diff", "--name-only", f"{base}...HEAD"))
    files |= set(git("diff", "--name-only", "HEAD"))
    files |= set(git("diff", "--name-only", "--cached"))
    return sorted(files)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="只跑受影响的测试")
    parser.add_argument("files", nargs="*", help="显式指定改动文件（默认读 git diff）")
    parser.add_argument("--base", default="main", help="比较基线（默认 main）")
    parser.add_argument("--run", action="store_true", help="直接执行 pytest")
    args = parser.parse_args(argv)

    changed = changed_files(args.base, args.files or None)
    if not changed:
        print(f"没有改动（相对 {args.base}）")
        return 0

    mode, tests, notes = plan(changed, build_index())
    print(f"改动 {len(changed)} 个文件：")
    for rel in changed:
        print(f"  {rel}")
    for note in notes:
        print(f"  ! {note}")

    if mode == "full":
        print(f"\n→ 全量：python -m pytest -n {PYTEST_N}")
        command = [sys.executable, "-m", "pytest", "-q", "-n", PYTEST_N]
    elif mode == "none":
        print("\n→ 没有需要跑的测试（纯脚本/配置改动，CI 会兜底）")
        return 0
    else:
        print(f"\n→ {len(tests)} 个测试文件：")
        for rel in tests:
            print(f"  {rel}")
        command = [sys.executable, "-m", "pytest", "-q", "-n", PYTEST_N, *tests]

    if not args.run:
        return 0
    print("\n$ " + " ".join(command))
    return subprocess.run(command, cwd=ROOT).returncode


if __name__ == "__main__":
    raise SystemExit(main())
