"""「打开 / 查看」命令构造（v0.31，承接 v0.28 的注入通道）。

只做纯逻辑，不 import Textual / tmux：App 拿到 :class:`CommandPlan` 后
直接交给后端注入当前 pane。

口径：

- **打开**：按 ``$MYRTUI_EDITOR`` → ``$EDITOR`` → ``$VISUAL`` → ``micro`` →
  ``vim`` → ``vi`` 的顺序解析编辑器；显式环境变量原样尊重（可带参数，按
  ``shlex`` 拆分），自动探测项用 ``shutil.which`` 的绝对路径。helix / nvim
  不再自动探测，但用户显式设置环境变量时仍可用。命令注入后可直接阅读 /
  编辑 / 搜索，退出编辑器即回 shell。
- **查看**（无编辑器时的兜底）：优先分页器（``ov`` → ``less -R``；``ov`` 有
  滚轮 / 拖选 / 增量搜索，``less`` 只读兜底），都没有时退回 ``cat``；
- 路径含换行/控制字符、文件不存在、不是普通文件、前 8 KiB 含 NUL（二进制）
  → 拒绝；
- 路径与工具路径统一 ``shlex.quote``，避免 pane 内的 alias / PATH 差异。
"""
from __future__ import annotations

import os
import shlex
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping

MARKDOWN_SUFFIXES = frozenset({".md", ".markdown", ".mdown", ".mkd"})
IMAGE_SUFFIXES = frozenset(
    {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".tif", ".tiff", ".ico", ".avif"}
)
BINARY_SNIFF_BYTES = 8 * 1024

# 显式指定的编辑器优先（按顺序）；``VISUAL`` 与 ``EDITOR`` 的次序沿用
# 「全屏编辑器看 VISUAL」的惯例，但本仓把它排在 EDITOR 之后，避免用户只
# 设了 EDITOR 时被空 VISUAL 干扰（空值会被跳过）。
EDITOR_ENV_VARS = ("MYRTUI_EDITOR", "EDITOR", "VISUAL")
# 自动探测顺序：modeless 的 micro 最省心，vim / vi 兜底。helix / nvim 不再
# 自动探测（用户想用时通过 $MYRTUI_EDITOR / $EDITOR 显式指定）。
EDITOR_CANDIDATES = ("micro", "vim", "vi")
# 分页器：``ov`` 有滚轮 / 拖选 / 增量搜索（最接近 Micro 的阅读体验），
# ``less -R`` 是几乎必然存在的只读兜底。
PAGER_ENV_VARS = ("MYRTUI_PAGER",)
PAGER_CANDIDATES = (("ov", ()), ("less", ("-R",)))


@dataclass(frozen=True)
class CommandPlan:
    """一条待注入命令 + 需要提示用户的降级/拒绝原因。"""

    command: str | None
    notice: str | None = None


@dataclass(frozen=True)
class EditorSpec:
    """解析后的编辑器命令（``argv`` 已按 shell 词法拆好）。"""

    argv: tuple[str, ...]

    @property
    def name(self) -> str:
        """菜单展示用短名（``code --wait`` → ``code``）。"""
        return os.path.basename(self.argv[0]) if self.argv else ""


def is_markdown(path: str | os.PathLike[str]) -> bool:
    """扩展名判断（大小写不敏感）。"""
    return Path(path).suffix.lower() in MARKDOWN_SUFFIXES


def is_image(path: str | os.PathLike[str]) -> bool:
    """图片扩展名判断（大小写不敏感；能否解码由 Pillow 决定）。"""
    return Path(path).suffix.lower() in IMAGE_SUFFIXES


def _bad_path_reason(path: str) -> str | None:
    if not path:
        return "文件路径为空"
    for char in path:
        code = ord(char)
        if char in "\r\n" or code < 0x20 or code == 0x7F:
            return "路径包含换行或控制字符，未执行"
    return None


def _file_problem(path: str) -> str | None:
    """返回拒绝原因；``None`` = 可以安全交给外部工具打开。"""
    reason = _bad_path_reason(path)
    if reason is not None:
        return reason
    target = Path(path)
    try:
        if not target.is_file():
            return "文件不存在或不是普通文件"
    except OSError as exc:
        return f"无法读取文件：{exc}"
    try:
        with target.open("rb") as handle:
            probe = handle.read(BINARY_SNIFF_BYTES)
    except OSError as exc:
        return f"无法读取文件：{exc}"
    if b"\0" in probe:
        return "二进制文件，未打开"
    return None


def _which_or_keep(words: list[str], which) -> tuple[str, ...]:
    """把裸工具名解析成绝对路径（已是路径 / 查不到时原样保留）。"""
    head = words[0]
    if os.path.sep not in head and which(head):
        head = which(head)
    return (head, *words[1:])


def resolve_editor(
    *,
    which=None,
    env: Mapping[str, str] | None = None,
) -> EditorSpec | None:
    """解析编辑器：环境变量优先，其次探测 ``micro`` / ``vim`` / ``vi``。

    ``env`` / ``which`` 显式注入便于测试；不传时读 ``os.environ`` /
    ``shutil.which``。显式环境变量即使 ``which`` 查不到也原样使用（用户
    可能在 pane 的 shell 里配了 alias 或自己的 PATH），不会静默忽略。
    """
    which = which or shutil.which
    env = os.environ if env is None else env

    for var in EDITOR_ENV_VARS:
        raw = env.get(var)
        if not raw or not raw.strip():
            continue
        try:
            words = shlex.split(raw)
        except ValueError:
            continue  # 引号不配对：跳过这个变量，继续下一个候选
        if words:
            return EditorSpec(_which_or_keep(words, which))

    for name in EDITOR_CANDIDATES:
        found = which(name)
        if found:
            return EditorSpec((found,))
    return None


def resolve_pager(
    *,
    which=None,
    env: Mapping[str, str] | None = None,
) -> tuple[str, ...] | None:
    """解析分页器：``$MYRTUI_PAGER`` 优先，其次 ``ov`` / ``less -R``。

    返回完整 argv（``ov`` → ``("/…/ov",)``、``less`` → ``("/…/less", "-R")``）；
    环境变量可带参数（如 ``MYRTUI_PAGER='less -R'``），显式值原样尊重。
    """
    which = which or shutil.which
    env = os.environ if env is None else env

    for var in PAGER_ENV_VARS:
        raw = env.get(var)
        if not raw or not raw.strip():
            continue
        try:
            words = shlex.split(raw)
        except ValueError:
            continue
        if words:
            return _which_or_keep(words, which)

    for name, extra in PAGER_CANDIDATES:
        found = which(name)
        if found:
            return (found, *extra)
    return None


def pager_name(*, which=None, env: Mapping[str, str] | None = None) -> str | None:
    """分页器短名（菜单展示用）；没有分页器返回 ``None``。"""
    spec = resolve_pager(which=which, env=env)
    return os.path.basename(spec[0]) if spec else None


def viewer_name(*, which=None) -> str:
    """无编辑器时的查看器名字（菜单展示用）。"""
    which = which or shutil.which
    pager = pager_name(which=which)
    return pager or "cat"


def build_open(
    path: str,
    editor: EditorSpec | None = None,
    *,
    which=None,
    env: Mapping[str, str] | None = None,
) -> CommandPlan:
    """打开文件：``<editor> '<绝对路径>'``；无编辑器时退回 :func:`build_view`。"""
    which = which or shutil.which
    absolute = os.path.abspath(path)
    problem = _file_problem(absolute)
    if problem is not None:
        return CommandPlan(None, problem)

    spec = editor or resolve_editor(which=which, env=env)
    if spec is None:
        view = build_view(absolute, which=which)
        return CommandPlan(
            view.command,
            view.notice or "未找到编辑器，已改用查看器打开",
        )

    argv = " ".join(shlex.quote(word) for word in spec.argv)
    return CommandPlan(f"{argv} {shlex.quote(absolute)}")


def build_view(path: str, *, which=None) -> CommandPlan:
    """只读查看兜底：分页器（``ov`` / ``less -R``）→ ``cat``。"""
    which = which or shutil.which
    absolute = os.path.abspath(path)
    problem = _file_problem(absolute)
    if problem is not None:
        return CommandPlan(None, problem)

    quoted_path = shlex.quote(absolute)
    pager = resolve_pager(which=which)
    if pager:
        argv = " ".join(shlex.quote(word) for word in pager)
        return CommandPlan(f"{argv} {quoted_path}")

    cat = which("cat")
    if cat:
        return CommandPlan(
            f"{shlex.quote(cat)} -- {quoted_path}",
            "未找到分页器，已直接输出（不可翻页 / 搜索）",
        )
    return CommandPlan(
        f"cat -- {quoted_path}",
        "未找到分页器 / cat，按 shell 内置 cat 输出",
    )


__all__ = [
    "BINARY_SNIFF_BYTES",
    "CommandPlan",
    "EDITOR_CANDIDATES",
    "EDITOR_ENV_VARS",
    "EditorSpec",
    "IMAGE_SUFFIXES",
    "MARKDOWN_SUFFIXES",
    "PAGER_CANDIDATES",
    "PAGER_ENV_VARS",
    "build_open",
    "build_view",
    "is_image",
    "is_markdown",
    "pager_name",
    "resolve_editor",
    "resolve_pager",
    "viewer_name",
]
