"""左侧侧栏 v2：文件树 + Git 历史（P3；v0.10 去掉顶部摘要）。

- 文件树（P3）：``FileTree``（DirectoryTree 子类），目录点击逐层展开（懒加载），
  文件点击仅选中；**v0.10 增 git 状态配色**（按 VS Code 习惯上色，目录继承）；
- Git 历史（P3）：``GitLogPanel``，仓库级 / 文件级两级（取数与刷新节奏由 App
  驱动，见 P3 方案 §5 D7）；行格式为日期 + 标题（日期随宽度三档自适应），
  悬停 1s 显示完整提交详情卡片；标题行（``#gitlog-title``）由**本类**渲染成
  ``GIT <branch>``（选中文件时 ``GIT <branch> · <相对路径>``）——分支数据在
  本类手里，App 只负责告知「当前看的是哪个文件」（``set_scope``）；
- 宽度自适应（P3 D5）：CSS ``width: 15fr; min-width: 20; max-width: 50%``
  （15fr 是**默认值**，运行时由分隔线拖拽改写，v0.11；v0.11.2 收窄到
  「基本显示文件树即可」），标题按**实际内容宽**截断；控件未挂载 /
  宽度未知时用兜底预算 ``FALLBACK_CONTENT_CELLS``
  （= 侧栏最小宽 20 − padding 2）。
- 刷新：分支 1s 一轮、**per-file 状态 3s 一轮**，都走 worker（git 是阻塞调用，
  放事件循环里每秒卡一次，P3 R3）。
- **v0.11.3**：标题行 + 历史面板包进 ``#git-area`` 容器——聚焦 Git 面板时
  底色**只染下半区**（含标题行），与文件树各染各的半区（CSS 无法跨兄弟
  节点染色，需共同容器做 ``:focus-within`` 锚点；容器不改变布局）。
数据源固定为启动目录，不与 tmux 发生关系。
"""
from __future__ import annotations

import asyncio

from rich.cells import cell_len
from textual import work
from textual.containers import Vertical
from textual.css.query import NoMatches
from textual.widgets import Static

from myrtui.client.cells import truncate_cells as _truncate_cells
from myrtui.client.file_tree import FileTree
from myrtui.client.git_panel import GitLogPanel
from myrtui.client.separators import Separator
from myrtui.gitstat import GitInfo, git_file_status, git_status

# 标题行的**兜底**预算（cell）：控件未挂载 / 宽度未知时用。
# 与 app.py CSS ``#sidebar { min-width: 20; padding: 0 1; }`` 对齐
# （20 盒宽 − 2 padding = 18 cell，v0.11.2）；侧栏变宽后按实际内容宽
# 动态算（D5）。
FALLBACK_CONTENT_CELLS = 18

# per-file 状态刷新节奏（秒）：比分支慢——``-uall`` 要扫未跟踪文件，成本更高。
FILE_STATUS_INTERVAL = 3.0
# 分支刷新节奏（秒）：沿用 P1 的 1s 节流。
BRANCH_INTERVAL = 1.0


class Sidebar(Vertical):
    """侧栏 v2：文件树 + Git 历史（标题含分支名）。"""

    def __init__(self, root: str, **kwargs):
        super().__init__(**kwargs)
        self.root = root
        self._git_status: str = "unavailable"
        self._git: GitInfo | None = None
        self._scope: str | None = None
        """当前 Git 历史的作用域（相对 root 的路径）；None = 仓库级。"""
        self._title = ""

    def compose(self):
        yield FileTree(self.root, id="file-tree")
        # 文件树 | Git 面板之间的横分隔线（可拖）
        yield Separator("horizontal", id="sep-tree")
        # git-area 包裹标题 + 历史面板（v0.11.3）：聚焦 Git 面板时**只染
        # 下半区**（含标题行）——CSS 无法跨兄弟节点染色，需共同的容器做
        # ``:focus-within`` 锚点；容器本身不改变布局（高度 = 树的对半 fr）。
        with Vertical(id="git-area"):
            yield Static("", id="gitlog-title")  # height 1：GIT <branch> · <file>
            yield GitLogPanel(id="git-log")

    def on_mount(self) -> None:
        # 注意：不能叫 refresh——Textual Widget.refresh 是刷新渲染的方法
        # （Static.update 内部会调用它传 layout= 参数）。
        self.refresh_branch()
        # per-file 状态：异步首刷（``-uall`` 扫未跟踪文件可能很慢，不在
        # on_mount 里同步阻塞 UI），随后按 FILE_STATUS_INTERVAL 周期刷。
        self._refresh_file_status_worker()
        # 周期刷新走 worker：阻塞调用挪进线程，不卡事件循环（R3）。
        self.set_interval(BRANCH_INTERVAL, self._refresh_branch_worker)
        self.set_interval(FILE_STATUS_INTERVAL, self._refresh_file_status_worker)

    def on_resize(self, event) -> None:
        """宽度变化 → 重算标题截断（D5：预算按实际内容宽动态算）。"""
        self._render_title()

    # ---------- 分支 / 标题 ----------

    def refresh_branch(self) -> None:
        """同步刷新分支（未挂载也可调用——测试直接构造 Sidebar 时用）。"""
        status, info = git_status(self.root)
        self._git_status, self._git = status, info
        self._render_title()

    def refresh_data(self) -> None:
        """同步刷新全部数据（分支 + per-file 状态）——测试直接构造时用。"""
        self.refresh_branch()
        self._refresh_file_status()

    @work(exclusive=True, group="sidebar-branch", exit_on_error=False)
    async def _refresh_branch_worker(self) -> None:
        """分支刷新（worker）：``git status`` 在线程里跑。"""
        status, info = await asyncio.to_thread(git_status, self.root)
        self._git_status, self._git = status, info
        self._render_title()

    def set_scope(self, path: str | None) -> None:
        """设置 Git 历史的作用域（None = 仓库级）→ 标题随之变化。

        由 App 在历史面板内容切换时调用（仓库级 ⇄ 文件级）。
        """
        if path == self._scope:
            return
        self._scope = path
        self._render_title()

    def _branch_label(self) -> str:
        """分支标签：分支名 / ``-``（非仓库）/ ``?``（git 不可用）。"""
        if self._git is not None:
            return self._git.branch
        return "-" if self._git_status == "not-repo" else "?"

    def _render_title(self) -> None:
        """渲染标题行：``GIT <branch>``（+ `` · <相对路径>``）。"""
        prefix = f"GIT {self._branch_label()}"
        if self._scope:
            # 路径段可截断，「GIT <branch>」始终完整
            head = f"{prefix} · "
            budget = self._content_cells() - cell_len(head)
            title = head + _truncate_cells(self._scope, budget)
        else:
            title = _truncate_cells(prefix, self._content_cells())
        if title != self._title:
            self._title = title
            self._update_title(title)

    def _content_cells(self) -> int:
        """标题可用宽度（cell）：已挂载按实际内容宽，未挂载走兜底预算。"""
        width = self.content_size.width
        return width if width > 0 else FALLBACK_CONTENT_CELLS

    def _update_title(self, text: str) -> None:
        try:
            self.query_one("#gitlog-title", Static).update(text)
        except NoMatches:
            pass  # 未挂载（测试直接构造 Sidebar 时无子控件）

    # ---------- per-file 状态（文件树配色） ----------

    def _tree(self) -> FileTree | None:
        try:
            return self.query_one("#file-tree", FileTree)
        except NoMatches:
            return None

    @work(exclusive=True, group="sidebar-file-status", exit_on_error=False)
    async def _refresh_file_status_worker(self) -> None:
        """per-file 状态刷新（worker）：``git status -z -uall`` 在线程里跑。

        侧栏折叠时跳过（没人看得到颜色），省一次 git 调用；树的引用在
        ``await`` **之后**再取——避免首刷时控件尚未挂载。
        """
        if not self.display:
            return
        status, files = await asyncio.to_thread(git_file_status, self.root)
        tree = self._tree()
        if tree is None or status != "ok":
            return
        tree.set_status_map(files)

    def refresh_file_status(self) -> None:
        """立即起一轮 per-file 状态刷新（异步；侧栏展开回来时调用）。"""
        self._refresh_file_status_worker()

    def _refresh_file_status(self) -> None:
        """同步取一次 per-file 状态（测试用）。"""
        tree = self._tree()
        if tree is None:
            return
        status, files = git_file_status(self.root)
        if status == "ok":
            tree.set_status_map(files)
