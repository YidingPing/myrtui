"""git 状态轮询（P1，DESIGN.md §5；v0.10 增 per-file 状态）。

侧栏第二层 ``GIT <branch>``：``git status --porcelain -b -uno`` 节流 1s。
- 解析与执行分离：``parse_git_status`` 为纯函数（可单测）；
- ``git_status`` 区分三种结果（侧栏据此显示）：
  - ``("ok", GitInfo)``          → ``GIT <branch> ±N``
  - ``("not-repo", None)``       → ``GIT -``（目录不在任何 git 仓库内）
  - ``("unavailable", None)``    → ``GIT ?``（git 未安装 / 超时 / 其他错误）

**per-file 状态**（v0.10：文件树按 VS Code 习惯着色）：

- ``parse_file_status`` 为纯函数：解析 ``git status --porcelain -z`` 的 NUL
  记录 → ``{相对路径: 状态码}``；
- ``git_file_status`` 负责取数（``rev-parse --show-prefix`` + ``status -z
  -uall -- .``），失败不抛错，与 ``git_status`` 同口径。
"""
from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass

# porcelain -b 头行：## <branch>...<upstream> [ahead 1, behind 2]
# 无 upstream 时无 "..." 段；detached HEAD 为 "## HEAD (no branch)"
_BRANCH_RE = re.compile(r"^##\s+(\S+?)(?:\.\.\.| |$)")
# 空仓库（无 commit）特例（P1 验收 F1）：普通正则会把
# "## No commits yet on main" 解析成 "No"——须先按整段取分支名。
# 新 git（>=2.28 时代）是 "No commits yet on"，旧 git 是 "Initial commit on"。
_NO_COMMITS_RE = re.compile(r"^##\s+(?:No commits yet on|Initial commit on)\s+(.+)$")

Status = str  # "ok" | "not-repo" | "unavailable"


@dataclass(frozen=True)
class GitInfo:
    branch: str
    dirty: int  # 变更条数（-uno：不含未跟踪文件）


def parse_git_status(output: str) -> GitInfo | None:
    """解析 ``git status --porcelain -b`` 输出。空输出/格式异常返回 None。"""
    lines = output.splitlines()
    if not lines or not lines[0].startswith("##"):
        return None
    # 空仓库特例先行（F1）：须先于 _BRANCH_RE，否则分支名被取成 "No"/"Initial"
    m0 = _NO_COMMITS_RE.match(lines[0])
    if m0:
        return GitInfo(branch=m0.group(1).strip(), dirty=len(lines) - 1)
    m = _BRANCH_RE.match(lines[0])
    if not m:
        return None
    # detached HEAD：porcelain -b 打印 "## HEAD (no branch)"，branch 段即 "HEAD"
    return GitInfo(branch=m.group(1), dirty=len(lines) - 1)


def _run_git(args: list[str], root: str, git_bin: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [git_bin, "-C", root, *args],
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        # 文件名可能不是合法 UTF-8（C locale 下尤甚）——解码失败时替换而不是
        # 抛 UnicodeDecodeError 把整个轮询打断（这类文件只是匹配不上颜色）
        errors="replace",
        timeout=2.0,
    )


# ---------- per-file 状态（v0.10：文件树着色） ----------

# 归一化后的状态码（与 git porcelain 的 XY 两列一一对应到「一种颜色」）：
#   A = 新增 / 未跟踪（绿）  M = 修改（金）  D = 删除（红）
#   R = 重命名（青）          U = 冲突（红）
# 未跟踪（``??``）按 VS Code 惯例与「新增」同色（绿）。
FileStatus = dict[str, str]


def _normalize_status(code: str) -> str | None:
    """porcelain 的 ``XY`` 两列 → 归一化状态码；无意义的状态返回 None。

    优先级：冲突 > 新增 > 删除 > 重命名 > 复制 > 修改——与 git 自身的
    「后发生的覆盖先发生的」直觉一致（``AM``=已暂存新增又改了 → 仍算新增，
    ``MD``=改了又删了 → 算删除）。
    """
    if len(code) < 2:
        return None
    x, y = code[0], code[1]
    if "U" in (x, y):  # 冲突（UU/AA/AU/…）
        return "U"
    if "A" in (x, y):  # 新增（已暂存）
        return "A"
    if "D" in (x, y):  # 删除
        return "D"
    if "R" in (x, y):  # 重命名
        return "R"
    if "C" in (x, y):  # 复制 → 按新增着色
        return "A"
    if "M" in (x, y) or "T" in (x, y):  # 修改 / 类型变更
        return "M"
    return None


def parse_file_status(records: list[str], prefix: str = "") -> FileStatus:
    """解析 ``git status --porcelain -z`` 的记录 → ``{相对路径: 状态码}``。

    - ``records``：按 NUL 切分的记录列表（每条形如 ``XY<space>PATH``；最后
      一条通常是空串，忽略即可）；
    - ``prefix``：**仓库根**相对本项目根的前缀（``git rev-parse --show-prefix``，
      仓库根处为空串、子目录处形如 ``sub/``）——porcelain 的路径恒为仓库根
      相对，这里剥掉前缀换成**项目根相对**；不在前缀下的条目（非本项目子树）
      一律丢弃。

    重命名/复制的记录是 ``XY<space>NEW\\0OLD\\0``——``OLD`` 单独占一条记录且
    **没有状态前缀**，按「上一条是 R/C 则跳过下一条」处理。
    """
    out: FileStatus = {}
    index = 0
    total = len(records)
    while index < total:
        record = records[index]
        index += 1
        if len(record) < 3:
            continue
        code = record[:2]
        path = record[3:]
        if "R" in code or "C" in code:
            index += 1  # 下一条是旧路径（无状态前缀），跳过
        if code == "??":
            status = "A"  # 未跟踪 → 按新增着色
        elif code == "!!":
            continue  # 忽略项（我们不查 --ignored，兜底跳过）
        else:
            status = _normalize_status(code)
        if status is None or not path:
            continue
        if prefix:
            if not path.startswith(prefix):
                continue  # 不在本项目子树内
            path = path[len(prefix):]
        if path:
            out[path] = status
    return out


def git_file_status(root: str, git_bin: str = "git") -> tuple[Status, FileStatus]:
    """取每个文件的变更状态（路径相对 ``root``）。失败不抛错。

    返回 ``("ok", {相对路径: 状态码})`` / ``("not-repo", {})`` /
    ``("unavailable", {})``——与 ``git_status`` 同口径。
    """
    try:
        prefix_proc = _run_git(["rev-parse", "--show-prefix"], root, git_bin)
    except FileNotFoundError:
        return "unavailable", {}
    except (subprocess.TimeoutExpired, OSError):
        return "unavailable", {}
    if prefix_proc.returncode != 0:
        return "not-repo", {}  # 不在任何仓库内
    prefix = prefix_proc.stdout.strip()
    try:
        proc = _run_git(
            ["status", "--porcelain", "-z", "-uall", "--", "."], root, git_bin
        )
    except FileNotFoundError:
        return "unavailable", {}
    except (subprocess.TimeoutExpired, OSError):
        return "unavailable", {}
    if proc.returncode != 0:
        return "unavailable", {}
    return "ok", parse_file_status(proc.stdout.split("\0"), prefix)


def git_status(root: str, git_bin: str = "git") -> tuple[Status, GitInfo | None]:
    """在 root 取 git 状态。任何失败都不抛错——侧栏是尽力而为的信息。"""
    try:
        proc = _run_git(["status", "--porcelain", "-b", "-uno"], root, git_bin)
    except FileNotFoundError:
        return "unavailable", None
    except (subprocess.TimeoutExpired, OSError):
        return "unavailable", None
    if proc.returncode == 0:
        info = parse_git_status(proc.stdout)
        if info is not None:
            return "ok", info
        return "unavailable", None
    # status 失败：区分「非仓库」与「其他错误」（如 git 损坏、权限）
    try:
        probe = _run_git(["rev-parse", "--is-inside-work-tree"], root, git_bin)
    except Exception:
        return "unavailable", None
    if probe.returncode != 0:
        return "not-repo", None
    return "unavailable", None
