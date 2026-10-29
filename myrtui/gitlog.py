"""git 提交历史取数与解析（P3，DESIGN.md §5.1）。

与 ``gitstat.py`` 严格同构：**解析是纯函数（可单测）；I/O 函数任何失败都不抛错**。

三态（与 gitstat 同口径，用户据此分辨「没有历史」与「git 坏了」）：

- ``("ok", commits)``      → 正常（**空仓库**也是 ok + 空列表）；
- ``("not-repo", [])``     → 目录不在任何 git 仓库内；
- ``("unavailable", [])``  → git 未安装 / 超时 / 其他错误。

命令构造（``git -C <root> log …``）：

```sh
# 仓库级
git -C <root> log --no-color --no-decorate \
    --date=format:%Y-%m-%d %H:%M \
    --pretty=format:%h%x1f%an%x1f%ad%x1f%s%x1f%b%x00 -n <limit>
# 文件级（追加 --follow 与路径）
git -C <root> log --follow --no-color --no-decorate \
    --date=format:%Y-%m-%d %H:%M \
    --pretty=format:%h%x1f%an%x1f%ad%x1f%s%x1f%b%x00 -n <limit> -- <relpath>
```

- **字段分隔用 ``\\x1f``（US）**：subject 可含 ``|``、``:``、空格等任意非换行
  字符，用 ``|`` 分隔会炸；
- **记录分隔用 NUL**：正文（``%b``）可以多行，不能再按行切分；format 末尾
  显式 ``%x00``（不用 ``-z``：``-z`` 不会给最后一条补 NUL）；
- ``--follow`` 仅对**单个文件路径**有效（目录路径不适用）；
- 路径必须是**相对 root 的仓库内路径**（越界 → git 自身报错 → unavailable）。
"""
from __future__ import annotations

import subprocess
from dataclasses import dataclass

Status = str  # "ok" | "not-repo" | "unavailable"

# 字段分隔符：US（unit separator）——subject 可能含 | : 空格等任意非换行字符
_FIELD_SEP = "\x1f"

# 记录分隔符：NUL。正文（%b）可以有多行，不能再按行切分；format 末尾显式
# 追加 %x00，保证**只有一条提交时**也有记录终止符。
_RECORD_SEP = "\x00"

# %h 短 hash / %an 作者 / %ad 日期时间（format → 2026-10-19 14:30）/ %s 标题
# / %b 正文（不含标题；可多行）
_PRETTY = "--pretty=format:%h%x1f%an%x1f%ad%x1f%s%x1f%b%x00"

# --date=format: 只用数字段，locale 无关；面板按宽度再裁成 MM-DD / 日期 / 日期+时间
_DATE_FORMAT = "--date=format:%Y-%m-%d %H:%M"

DEFAULT_LIMIT = 50  # Q6：默认显示 50 条


@dataclass(frozen=True)
class Commit:
    """一条提交（git log 的一条记录）。"""

    sha: str
    """短 hash（%h，通常 7 位）。"""
    author: str
    """作者名（%an）。"""
    date: str
    """日期时间（%ad，``--date=format:%Y-%m-%d %H:%M`` → 2026-10-19 14:30）。"""
    subject: str
    """标题（%s）。"""
    body: str = ""
    """正文（%b，不含标题；可能为空或多行）。"""


def parse_git_log(output: str) -> list[Commit]:
    """解析 ``git log --pretty=format:…`` 输出。空/异常输出 → ``[]``。纯函数。

    新格式用 NUL 分隔记录（``%x00``），正文可以含换行；旧格式（一行一提交、
    4 个字段）仍然兼容，此时 ``body=""``。

    段数不足的记录**跳过**（不抛错，R7）——git 输出被截断或格式变化时不崩。
    """
    if _RECORD_SEP in output:
        records = [
            record.lstrip("\r\n")
            for record in output.split(_RECORD_SEP)
            if record.strip()
        ]
        record_mode = True
    else:
        records = output.splitlines()
        record_mode = False

    commits: list[Commit] = []
    for record in records:
        if record_mode:
            # maxsplit=4：前 4 个字段固定，余下全归正文（正文里的 US 不拆字段）
            parts = record.split(_FIELD_SEP, 4)
            if len(parts) < 4:
                continue
            subject = parts[3]
            body = parts[4].rstrip("\r\n") if len(parts) == 5 else ""
        else:
            # 兼容旧格式：一行一提交，subject 里的 US 原样拼回
            parts = record.split(_FIELD_SEP)
            if len(parts) < 4:
                continue
            subject = _FIELD_SEP.join(parts[3:])
            body = ""
        commits.append(
            Commit(
                sha=parts[0],
                author=parts[1],
                date=parts[2],
                subject=subject,
                body=body,
            )
        )
    return commits


def _run_git(
    args: list[str], root: str, git_bin: str
) -> subprocess.CompletedProcess:
    return subprocess.run(
        [git_bin, "-C", root, *args],
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=2.0,
    )


def git_log(
    root: str,
    path: str | None = None,
    limit: int = DEFAULT_LIMIT,
    git_bin: str = "git",
) -> tuple[Status, list[Commit]]:
    """取提交历史。``path`` 为相对 root 的路径时只取该路径的提交。

    判定顺序（P3 方案 §4.2）：

    1. ``git log`` rc==0 → ``("ok", commits)``；
    2. rc!=0 → 探针 ``rev-parse --is-inside-work-tree``：rc≠0 → ``not-repo``；
    3. rc==0（是仓库）→ 探针 ``rev-parse --verify --quiet HEAD``：rc≠0 →
       **空仓库** → ``("ok", [])``（面板显示「当前分支暂无提交」）；
    4. 否则 → ``("unavailable", [])``。

    第 3 步刻意**不匹配 stderr 文案**（本地化 git 的消息会变，R10）——
    用 locale 无关的探针判定。
    """
    args = [
        "log",
        "--no-color",
        "--no-decorate",
        _DATE_FORMAT,
        _PRETTY,
        "-n",
        str(limit),
    ]
    if path:
        args.append("--follow")
    cmd = list(args)
    if path:
        cmd += ["--", path]
    try:
        proc = _run_git(cmd, root, git_bin)
    except FileNotFoundError:
        return "unavailable", []
    except (subprocess.TimeoutExpired, OSError):
        return "unavailable", []
    if proc.returncode == 0:
        return "ok", parse_git_log(proc.stdout)
    # log 失败：区分「非仓库」「空仓库」「其他错误」（三者不能混成一个状态）
    try:
        probe = _run_git(["rev-parse", "--is-inside-work-tree"], root, git_bin)
    except Exception:
        return "unavailable", []
    if probe.returncode != 0:
        return "not-repo", []
    try:
        head = _run_git(["rev-parse", "--verify", "--quiet", "HEAD"], root, git_bin)
    except Exception:
        return "unavailable", []
    if head.returncode != 0:
        return "ok", []  # 空仓库：git 可用，只是当前分支没有提交
    return "unavailable", []
