"""公开仓的「文档边界」守卫。

公开仓的 ``docs/`` 是**用户向文档**，会被 GitHub 与 PyPI sdist 同时发布；
设计/方案/验收记录住在私有仓 ``myrtui-internal``。这条边界靠人记不住，所以
用测试锁死（见 ``docs/README.md`` / ``AGENTS.md``）：

- ``docs/`` 下只允许白名单文件（新增必须先改白名单，等于一次显式决策）；
- 任何 ``*-PLAN.md`` / ``*-ACCEPTANCE.md`` / ``*-REVIEW.md`` / ``DESIGN.md``
  一类内部文档出现在仓库任何位置都算回流；
- 已跟踪文件里不得出现 RFC1918 私网地址（内网 Gitea 曾散落在 README、
  install.sh 与 release 脚本里）；
- ``pyproject.toml`` 的 sdist 白名单只能包含 ``docs/`` 下同样被允许的文件。
"""
from __future__ import annotations

import re
import subprocess
from pathlib import Path

try:  # Python >= 3.11
    import tomllib
except ModuleNotFoundError:  # 3.10：dev extra 里的 tomli（见 pyproject）
    import tomli as tomllib

import pytest

ROOT = Path(__file__).resolve().parents[1]

# 允许出现在公开仓 docs/ 下的路径（相对仓库根）
ALLOWED_DOCS = {
    "docs/README.md",
    "docs/USAGE.md",
    "docs/USAGE.zh-CN.md",
    "docs/ARCHITECTURE.md",
}
ALLOWED_DOCS_GLOBS = (
    "docs/demo.gif",
    "docs/assets/*",
)

# 内部文档的命名特征：出现在公开仓任何位置都视为回流
INTERNAL_DOC_NAME = re.compile(
    r"(?:^|/)(?:DESIGN|DESIGN-REVIEW|RELEASE)(?:\.md)?$"
    r"|(?:^|/)[^/]+-(?:PLAN|ACCEPTANCE|REVIEW)(?:-REVIEW)?\.md$"
    r"|(?:^|/)releases/[^/]+\.md$"
)

PRIVATE_RANGE = re.compile(
    r"\b(?:10\.\d{1,3}\.\d{1,3}\.\d{1,3}"
    r"|192\.168\.\d{1,3}\.\d{1,3}"
    r"|172\.(?:1[6-9]|2\d|3[01])\.\d{1,3}\.\d{1,3})\b"
)

# 这些文件必须提到 token 名（守卫自身的断言），不算泄漏
TOKEN_MENTION_ALLOWED = {"tests/test_install_script.py"}

# 标记拆开写：否则本文件自己就会被 test_no_release_tokens_in_public_tree 抓到。
_TOKEN_MARKERS = ("GIT" + "EA_TOKEN", "MYRTUI" + "_TOKEN")


def _tracked_files() -> list[str]:
    if not (ROOT / ".git").exists():
        pytest.skip("not a git checkout（sdist 安装场景），跳过文档边界守卫")
    out = subprocess.run(
        ["git", "ls-files", "-z"],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    return [p for p in out.split("\0") if p]


def _is_allowed_doc(path: str) -> bool:
    if path in ALLOWED_DOCS:
        return True
    return any(Path(path).match(pattern) for pattern in ALLOWED_DOCS_GLOBS)


def test_docs_dir_is_public_only() -> None:
    offenders = [
        p
        for p in _tracked_files()
        if p.startswith("docs/") and not _is_allowed_doc(p)
    ]
    assert not offenders, (
        "docs/ 只放用户向文档（白名单见 tests/test_docs_policy.py）。"
        "设计/验收/评审记录请写到私有仓 myrtui-internal：\n  "
        + "\n  ".join(offenders)
    )


def test_no_internal_docs_anywhere() -> None:
    offenders = [p for p in _tracked_files() if INTERNAL_DOC_NAME.search(p)]
    assert not offenders, (
        "公开仓不允许出现内部文档（PLAN / ACCEPTANCE / REVIEW / DESIGN / "
        "RELEASE / releases/*.md）：\n  " + "\n  ".join(offenders)
    )


def test_no_private_network_addresses() -> None:
    offenders: list[str] = []
    for rel in _tracked_files():
        p = ROOT / rel
        try:
            text = p.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        for num, line in enumerate(text.splitlines(), 1):
            if PRIVATE_RANGE.search(line):
                offenders.append(f"{rel}:{num}: {line.strip()[:80]}")
    assert not offenders, "公开仓不得包含私网地址：\n  " + "\n  ".join(offenders)


def test_no_release_tokens_in_public_tree() -> None:
    offenders: list[str] = []
    for rel in _tracked_files():
        if rel in TOKEN_MENTION_ALLOWED:
            continue
        p = ROOT / rel
        try:
            text = p.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        for marker in _TOKEN_MARKERS:
            if marker in text:
                offenders.append(f"{rel}: {marker}")
    assert not offenders, "公开仓不得残留内网发布 token 名称：\n  " + "\n  ".join(offenders)


def test_sdist_whitelist_covers_only_public_docs() -> None:
    data = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    include = data["tool"]["hatch"]["build"]["targets"]["sdist"]["include"]
    docs_entries = [e.lstrip("/") for e in include if e.startswith("/docs/")]
    assert docs_entries, "sdist 白名单里必须有 docs/ 条目（否则用户文档不会随包发布）"

    unexpected = [e for e in docs_entries if not _is_allowed_doc(e)]
    assert not unexpected, (
        "sdist 只能打包 docs/ 下的公开文档，以下条目不应出现：\n  "
        + "\n  ".join(unexpected)
    )
