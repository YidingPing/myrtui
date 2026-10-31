"""项目文件树（侧栏 v2 上半区，P3 方案 §4.3；v0.10 增 git 状态配色）。

基类选 ``textual.widgets.DirectoryTree``（textual 8.2.8 自带，理由见 P3 方案
§5 D2）：懒加载在 ``@work(thread=True)`` 的 ``_load_directory`` 里完成（**不阻塞
事件循环**），过滤钩子 ``filter_paths`` 与标签钩子 ``process_label``/``render_label``
恰好覆盖「排除 .git」与「状态配色」两个需求，键盘导航/滚动/展开收起现成。

本类相对基类的改动：

- 图标换成 ``▸ / ▾ / ·``（基类 emoji 各占 2 cell 且终端宽度表现不一）；
- ``guide_depth`` 改 2（每层省 2 cell）——**必须在 ``__init__`` 里赋值**：
  ``guide_depth``/``show_root`` 在基类是 reactive（数据描述符），子类若用
  **普通类属性**覆盖会**遮蔽**描述符 → ``self.guide_depth = x`` 不再经过
  ``Reactive.__set__`` → ``watch_guide_depth`` 不触发 → 改宽度不重绘（R8）；
- 目录点击 = 展开/收起（懒加载子项），文件点击 = **仅选中**并发 ``PathSelected``；
  文件打开动作只走上下文菜单（v0.28 右键；v0.28.1 起键盘也可达：``Enter``
  文件行选中 + 弹菜单，``Space`` 任意行弹菜单）；双击仍只选中；
- **git 状态配色**（v0.10）：``set_status_map`` 灌入 ``{相对路径: 状态码}``，
  ``render_label`` 按状态给文件名上色（VS Code 习惯：修改=金、新增/未跟踪=绿、
  删除=红、重命名=青、冲突=红），**目录按子项继承颜色**；
- **跟随外部改动**（v0.53）：``sync_status`` 在配色之外，只对「行集合真的变了」
  的路径重扫最近的已加载祖先目录。App 之外发生的改动（AI 写文件、``git checkout``、
  外部编辑器保存）此前没有任何触发源，树会一直显示旧内容。
"""
from __future__ import annotations

import asyncio
from pathlib import Path
from typing import TYPE_CHECKING, Iterable

from rich.style import Style
from textual import events
from textual.message import Message
from textual.widgets import DirectoryTree

if TYPE_CHECKING:  # 仅类型：不引 Textual 的私有模块到运行期
    from textual.widgets._tree import TreeNode

# 状态 → 颜色（VS Code Dark+ 的 git decoration 近似值）。
# 终端只支持 16 色时 Textual 会自动降到最接近的 ANSI 色。
STATUS_COLORS = {
    "A": "#73c991",  # 新增 / 未跟踪 —— 绿
    "M": "#d7ba7d",  # 修改 —— 金
    "D": "#f14c4c",  # 删除 —— 红
    "R": "#4ec9b0",  # 重命名 —— 青
    "U": "#f14c4c",  # 冲突 —— 红
}

# 目录继承时的优先级（数值大者胜）：冲突 > 新增 > 修改 > 删除 > 重命名
STATUS_PRIORITY = {"U": 5, "A": 4, "M": 3, "D": 2, "R": 1}

# 文件名在标签里的起始偏移：图标 ``▸ ``/``▾ ``/``· `` 恒占 2 cell。
LABEL_OFFSET = 2

# 一轮外部刷新最多重扫的目录数（v0.53）。一次 ``rm -rf`` 会让每个消失的路径
# 都指向一个要重扫的目录；超过这个数就只处理最浅的那些，其余留到下一轮。
MAX_EXTERNAL_DIRS = 16


class FileTree(DirectoryTree):
    """项目文件树：目录点击逐层展开，文件点击仅选中。"""

    # 普通类属性：可安全覆盖（基类就是普通类属性，非 reactive）
    ICON_NODE = "▸ "  # 覆盖基类 emoji 图标（省宽度 + 避免 emoji 宽度歧义）
    ICON_NODE_EXPANDED = "▾ "
    ICON_FILE = "· "

    class PathSelected(Message):
        """选中一个路径（目录或文件）。

        - ``path``：**相对树根**的路径（供 ``git log -- <path>`` 用）；
        - ``is_dir``：True = 目录（点击语义是展开/收起），False = 文件
          （点击语义是仅选中，不做任何其它事）。
        """

        def __init__(self, path: str, is_dir: bool) -> None:
            super().__init__()
            self.path = path
            """相对树根的路径。"""
            self.is_dir = is_dir
            """是否目录。"""

    class PathContextRequested(Message):
        """右键请求上下文菜单（v0.20）。

        右键**只移动光标行**（高亮），不 toggle 目录、不发 ``PathSelected``
        ——因此 Git 历史作用域保持不变。
        """

        def __init__(
            self,
            path: str,
            absolute: str,
            is_dir: bool,
            screen_x: int,
            screen_y: int,
        ) -> None:
            super().__init__()
            self.path = path
            """相对树根的路径（根节点为 ``""``，与 ``PathSelected`` 同口径）。"""
            self.absolute = absolute
            """绝对路径（菜单「复制路径」用）。"""
            self.is_dir = is_dir
            """是否目录（``node.allow_expand`` 的快照）。"""
            self.screen_x = screen_x
            """右键位置（屏幕坐标，菜单定位用）。"""
            self.screen_y = screen_y
            """右键位置（屏幕坐标，菜单定位用）。"""

    def __init__(self, path: str, **kwargs) -> None:
        super().__init__(path, **kwargs)
        self._root_path = Path(path)
        # reactive：必须在 __init__ 里赋值（走描述符 → watcher 触发 → 重绘）
        self.guide_depth = 2
        # 基类 auto_expand=True 会在「选中」时自动 toggle 节点；本类自己处理
        # 展开/收起，若不禁用会与本类的 toggle **叠加**（点一下等于没点）
        self.auto_expand = False
        # git 状态：文件表 {相对路径: 状态码} + 目录继承表 {相对目录: 状态码}
        self._status_map: dict[str, str] = {}
        self._dir_status: dict[str, str] = {}
        # 节点 id → 相对路径缓存（render_label 每次渲染都要路径，不能每次
        # 都做 resolve() 的 syscall）
        self._rel_cache: dict[int, str | None] = {}

    def on_mount(self) -> None:
        # 根节点 label 统一为目录名（基类首帧可能是全路径 str(path)）
        self.root.label = self._root_path.name

    # ---------- 刷新目录（v0.30） ----------

    def reload_path(self, relative: str) -> bool:
        """按相对路径重载一个目录，返回是否在内存树里找到了目标节点。

        用 ``DirectoryTree.reload_node``（只重扫目标子树，不重置其它目录的
        懒加载缓存）。8.2.8 的 ``_reload`` 会保留其它已展开子树与光标，但
        **目标节点自身会被展开**；根路径未 ``resolve()``（macOS ``/var`` →
        ``/private/var``）时光标恢复是 best-effort（可能回落到根行）。

        目录被外部删除时节点仍在内存树里 → 本方法仍会命中并返回 ``True``，
        ``reload_node`` 只会把它刷成空子节点（不抛错）；“删除”语义由 App
        层的 ``is_dir()`` 检查刷新父目录承接。只有节点真的不在树里（未加载 /
        竞态）才走全树 ``reload()`` 兜底并返回 ``False``。
        """
        node = self.find_loaded(relative)
        if node is None:
            self.reload()
            self._rel_cache.clear()
            return False
        self.reload_node(node)
        self._rel_cache.clear()
        return True

    def find_loaded(self, relative: str) -> "TreeNode | None":
        """静默查找**已在内存树里**的节点；找不到返回 ``None``。

        只遍历已加载的 ``children``（懒加载的目录天然不在里面），**不读盘、
        不重载、不展开**——自动刷新拿它判断「这一行现在在不在树上」。与
        ``reload_path`` 的区别是找不到时不回落到全树 ``reload()``：那条兜底
        会把整棵已展开子树重读一遍并把滚动位置拽回光标行，自动刷新不能走它。

        路径先用纯字符串比较（``_loaded_nodes``，无系统调用）；落空时再按
        ``resolve()`` 比一次，兜住「根路径与子路径写法不同」（软链 / macOS
        ``/var`` → ``/private/var``）——``reload_path`` 依赖的老行为不变。
        """
        node = self._loaded_nodes().get(relative)
        if node is not None:
            return node
        target = (
            (self._root_path / relative).resolve()
            if relative
            else self._root_path.resolve()
        )
        stack = [self.root]
        while stack:
            node = stack.pop()
            data = node.data
            if data is not None:
                try:
                    if Path(data.path).resolve() == target:
                        return node
                except (OSError, RuntimeError):
                    pass  # 路径已消失 / 符号链接环：继续找别的节点
            stack.extend(node.children)
        return None

    # ---------- git 状态配色（v0.10） ----------

    def set_status_map(self, status: dict[str, str]) -> None:
        """灌入 git 状态表 ``{相对路径: 状态码}`` 并重绘。

        - 状态表没变 → **不重绘**（与项目既有的防闪烁口径一致）；
        - 变了 → 必须让 Textual 重算行：``Tree`` 有行缓存（``_line_cache``），
          不清掉的话颜色不会更新（见 ``_invalidate_labels``）。
        """
        if status == self._status_map:
            return
        self._status_map = dict(status)
        self._dir_status = self._derive_dir_status(self._status_map)
        self._invalidate_labels()

    # ---------- 外部改动（v0.53：树跟着磁盘走） ----------

    def sync_status(self, status: dict[str, str]) -> bool:
        """把一次 git 状态快照同步到树上：先配色，再按需重扫目录。

        返回状态表是否变化——没变化就什么也不做（防闪烁，也防每 3 s 白扫盘）。

        **为什么不能只配色**：``set_status_map`` 只能给**已存在的行**换颜色。
        外部新建的文件没有行、外部删掉的文件留着死行，两者都要动结构，而
        「动结构」是这里唯一昂贵、也唯一会打扰用户的操作，所以规则收得很窄：
        只有**行与磁盘不一致**的路径才重扫最近的已加载祖先目录，见
        ``_refresh_structure``。

        ``status`` 只含**当前**快照：被删/被移走的路径要从**上一份**表里找，
        所以比较的是 ``previous | current`` 两个键集合，而不是只看当前这份。
        """
        if status == self._status_map:
            return False
        previous = self._status_map
        self.set_status_map(status)
        self._refresh_structure(previous, status, self._loaded_nodes())
        return True

    def _refresh_structure(
        self,
        previous: dict[str, str],
        current: dict[str, str],
        loaded: dict[str, "TreeNode"],
    ) -> None:
        """按「行集合变了」重扫最近的已加载祖先目录（最多 ``MAX_EXTERNAL_DIRS`` 个）。

        每个候选路径只问一件事：**行和磁盘是不是一致**。

        - 行在、盘上也在（只是状态码变了）→ 跳过，配色已由 ``set_status_map`` 完成；
        - 行不在、盘上也不在 → 跳过；
        - 其余（新文件 / 真删掉）→ 找到最近的已加载祖先目录重扫。

        ``loaded`` 是**一次遍历**建好的 ``{相对路径: 节点}`` 索引（``_loaded_nodes``）：
        一次 ``git checkout`` 可能带来几百个候选路径，每个都去走一遍树会让事件循环
        卡住（实测 200 个路径 × 333 个可见节点的旧写法是 2.2 s）。

        选中的目录再按自身状态分流：已展开 → ``reload_node``；**已加载但收起**
        → 只置 ``DirEntry.loaded = False``，等用户下次展开时由基类重读
        （``_add_to_load_queue`` 只看这个字段）。**不能**直接 ``reload_node``：
        8.2.8 的 ``_reload`` 对目标节点无条件 ``expand()``，会把用户收好的目录
        弹开——自动刷新不该有这种副作用。
        """
        targets: dict[str, "TreeNode"] = {}
        for relative in set(previous) | set(current):
            node = loaded.get(relative)
            exists = self._path_exists(relative)
            # 只有「行与磁盘不一致」才动结构：都在 → 配色就够；都不在 → 没得动。
            if (node is not None) == exists:
                continue
            key, directory = self._nearest_loaded_dir(relative, loaded)
            if directory is None:
                continue  # 祖先都没加载 → 用户展开时自然会读盘，别在这里兜底
            targets[key] = directory

        # 祖先已选中的子目录不必再扫：reload_node 会把展开的子树一起重读。
        ordered = sorted(targets.items(), key=lambda item: item[0].count("/"))
        reload_list: list[tuple[str, "TreeNode"]] = []
        for key, node in ordered:
            if any(
                key == parent or key.startswith(parent + "/")
                for parent, _ in reload_list
            ):
                continue
            reload_list.append((key, node))
            if len(reload_list) >= MAX_EXTERNAL_DIRS:
                break  # 安全阀：rm -rf 一棵大树时不让一轮刷新无限扩散

        for _key, node in reload_list:
            if node.is_expanded:
                self.reload_node(node)
            elif node.data is not None:
                node.data.loaded = False  # 收起：标脏，展开时再读
        if reload_list:
            self._rel_cache.clear()

    def _loaded_nodes(self) -> dict[str, "TreeNode"]:
        """已加载节点 → ``{相对路径: 节点}``（根是 ``""``），一次遍历建好。

        路径用 ``Path.relative_to`` 纯字符串比较，**不做 ``resolve()``**：树里的
        子路径本来就由根路径拼出来（同上/同前缀），而 ``resolve()`` 是每个节点一次
        系统调用——几百个候选路径的场景下这是唯一的性能瓶颈（见 ``_refresh_structure``）。
        """
        out: dict[str, "TreeNode"] = {}
        stack = [self.root]
        while stack:
            node = stack.pop()
            data = node.data
            if data is not None:
                relative: str | None
                try:
                    relative = str(Path(data.path).relative_to(self._root_path))
                except (ValueError, OSError):
                    relative = None  # 根路径换了写法（软链 / 竞态）：跳过这一项
                if relative == ".":
                    relative = ""
                if relative is not None:
                    out[relative] = node
            stack.extend(node.children)
        return out

    def _path_exists(self, relative: str) -> bool:
        """相对路径在磁盘上是否还在（权限 / 符号链接环一律按「不在」处理）。"""
        try:
            return (self._root_path / relative).exists()
        except OSError:
            return False

    def _nearest_loaded_dir(
        self, relative: str, loaded: dict[str, "TreeNode"]
    ) -> tuple[str, "TreeNode | None"]:
        """从 ``relative`` 的父目录往上，找最近的**已加载**目录节点。

        返回 ``(相对路径, 节点)``；根节点恒在内存里，所以只要 ``loaded`` 里有根，
        最后一定收敛到根。``(…, None)`` 只可能发生在根也没匹配上（路径写法变了）。
        """
        parent = relative.rpartition("/")[0]
        while True:
            node = loaded.get(parent)
            if node is not None:
                return parent, node
            if not parent:
                return "", None
            parent = parent.rpartition("/")[0]

    @staticmethod
    def _derive_dir_status(status: dict[str, str]) -> dict[str, str]:
        """由文件状态派生**目录继承表**（VS Code：目录按子项取色）。

        每个变更文件的所有祖先目录都记一份状态，按优先级
        （冲突 > 新增 > 修改 > 删除 > 重命名）取最强的那一个；根目录记为 ``""``。
        """
        dirs: dict[str, str] = {}
        for path, code in status.items():
            parts = path.split("/")
            for index in range(len(parts)):
                directory = "/".join(parts[:index])
                current = dirs.get(directory)
                if current is None:
                    dirs[directory] = code
                elif STATUS_PRIORITY.get(code, 0) > STATUS_PRIORITY.get(current, 0):
                    dirs[directory] = code
        return dirs

    def _invalidate_labels(self) -> None:
        """让树重算已渲染的行（否则行缓存里的旧颜色会一直留着）。

        ``Tree._clear_line_cache`` 是 **textual 私有 API**（P3 方案 R5 已登记
        同类风险）：清掉行缓存 + 树线缓存，随后 ``refresh()`` 触发重绘。
        万一未来版本改名/删除，``hasattr`` 兜底 → 退化成「颜色要等下次重绘
        才更新」，不崩。
        """
        clear = getattr(self, "_clear_line_cache", None)
        if clear is not None:
            clear()
        self.refresh()

    def render_label(self, node, base_style, style):
        """按 git 状态给**文件名**上色（图标与背景不动）。

        基类 ``DirectoryTree.render_label`` 返回 ``Text.assemble(图标, 标签)``
        ——图标恒占前 2 cell，故从 ``LABEL_OFFSET`` 起叠前景色即可（实测 Rich
        的 span 覆盖顺序：后叠的 stylize 覆盖扩展名高亮，背景不受影响）。
        """
        text = super().render_label(node, base_style, style)
        color = self._status_color(node)
        if color is not None:
            text.stylize(Style(color=color), LABEL_OFFSET)
        return text

    def _status_color(self, node) -> str | None:
        """节点的 git 状态 → 颜色；无状态返回 None。"""
        if node.data is None:
            return None
        relative = self._node_relative(node)
        if relative is None:
            return None
        code = self._status_map.get(relative)
        if code is None:
            code = self._dir_status.get(relative)  # 目录：继承子项颜色
        if code is None:
            return None
        return STATUS_COLORS.get(code)

    def _node_relative(self, node) -> str | None:
        """节点 → 相对树根的路径（带缓存：渲染时每帧都会问）。

        根节点归一化成 ``""``（与目录继承表里的根目录键一致）；无 data 的
        节点返回 None。
        """
        key = node.id
        if key in self._rel_cache:
            return self._rel_cache[key]
        data = node.data
        relative = self._relative(data.path) if data is not None else None
        if relative == ".":
            relative = ""
        self._rel_cache[key] = relative
        return relative

    # ---------- 过滤（D3/D4：唯一硬排除 .git） ----------

    def filter_paths(self, paths: Iterable[Path]) -> Iterable[Path]:
        """唯一硬排除：``.git``（git 的内部数据库，对浏览项目文件无意义）。

        其余一律保留——``.gitignore`` / ``node_modules`` / ``.venv`` 等都列出
        （靠**懒加载**控制体量，而不是靠过滤；过滤规则将来只改这一处）。
        """
        return (p for p in paths if p.name != ".git")

    # ---------- 选中行为 ----------

    async def _on_tree_node_selected(self, event) -> None:
        """目录 → toggle 展开/收起；文件 → 仅选中并发 ``PathSelected``。

        覆写基类（``DirectoryTree._on_tree_node_selected``）的三个注意点：

        ① 基类该处理器是 **async**，覆写须同为 async；
        ② 基类先 ``event.stop()`` 再发消息——覆写替换了基类行为后，
           ``event.stop()`` 必须自己补，否则 ``NodeSelected`` 会继续冒泡到 App；
        ③ 目录判定不能读 ``node.data`` 的字段：``DirEntry`` 是 ``path``/``loaded``
           两个字段的数据类、**没有 is_dir**，基类用的是
           ``await asyncio.to_thread(self._safe_is_dir, path)``（容忍目录被迭代
           删除/替换），子类沿用 ``_safe_is_dir``，不要自己猜。
        """
        event.stop()
        dir_entry = event.node.data
        if dir_entry is None:
            return
        is_dir = await asyncio.to_thread(self._safe_is_dir, dir_entry.path)
        if is_dir:
            event.node.toggle()
        # 目录也发消息：选中目录 → Git 历史面板回到仓库级（P3 方案 §9 验收 5）
        self.post_message(self.PathSelected(self._relative(dir_entry.path), is_dir))

    async def _on_click(self, event: events.Click) -> None:
        """右键 = 只高亮 + 请求上下文菜单（v0.20）；左键交基类。

        **为什么必须覆写**：textual 8.2.8 的 ``Tree._on_click`` 不判断按钮——
        右键会被当成左键，目录直接 toggle、并触发 ``PathSelected`` 把 Git 历史
        作用域切走（v0.20 实测：``button=3`` 点击未展开的目录 → 立即展开）。

        **要点**：

        - textual 的私有处理器沿 **MRO 逐个执行**（``_get_dispatch_methods``）：
          只覆写 ``_on_click`` 并不能替换基类实现，基类那一次仍会跑 →
          右键必须 ``event.prevent_default()``（它让分发循环在下一个类
          之前 break）**并且不能**再显式调 ``super()._on_click()``（否则左键
          会跑两遍）；
        - 右键只设 ``cursor_line``，**不**调 ``select_cursor``（那会触发
          ``_on_tree_node_selected`` → toggle + ``PathSelected``）；
        - 右键点在展开箭头（``meta["toggle"]``）上也不切换展开状态；
        - 根节点的 ``_relative`` 结果是 ``"."``，此处归一为 ``""``——与
          ``_node_relative``（本文件 git 配色路径）同口径，保证
          ``PathContextRequested.path`` 与 ``PathSelected.path`` 完全同构；
        - v0.28：双击不再打开预览，左键全部交基类（第一击选中 + 切历史）。
        """
        if event.button != 3:
            return  # 左键/中键：交给 MRO 里的基类处理器（不显式 super，防跑两遍）

        event.prevent_default()  # 关键：跳过基类 Tree._on_click（否则目录会 toggle）
        event.stop()
        line = event.style.meta.get("line")
        if line is None:  # 空白区右键：没有行可作用
            return
        node = self.get_node_at_line(line)
        if node is None or node.data is None:
            return
        async with self.lock:  # 与基类一致：光标行变更与懒加载串行
            self.cursor_line = line

        relative = self._relative(node.data.path)
        if relative == ".":
            relative = ""  # 根节点（与 _node_relative 同口径）
        self.post_message(
            self.PathContextRequested(
                relative,
                self._absolute(node.data.path),
                is_dir=node.allow_expand,
                screen_x=event.screen_x,
                screen_y=event.screen_y,
            )
        )

    # ---------- 键盘打开菜单（v0.28.1） ----------

    def on_key(self, event: events.Key) -> None:
        """``Enter`` / ``Space`` 打开上下文菜单，让全键盘操作也能用菜单。

        - ``Enter`` **文件行**：先按原语义「选中 + 切 Git 历史」，再在光标行
          弹出菜单（与鼠标右键同一 ``PathContextRequested``）；
        - ``Enter`` **目录行**：原样放行给基类，仍展开 / 收起、不弹菜单；
        - ``Space`` **任意行**：等价键盘右键——弹菜单但**不改** Git 历史。

        textual 8.2.8 的处理器沿 MRO **逐类**分发：``prevent_default()`` 才能
        拦住 ``Tree`` 的 ``enter → select_cursor`` / ``space → toggle_node``
        绑定（``stop()`` 只阻止冒泡，两者都要）。
        """
        if event.key not in ("enter", "space"):
            return
        node = self.cursor_node
        if node is None or node.data is None:
            return
        if event.key == "enter" and node.allow_expand:
            return  # 目录：Enter 仍是展开 / 收起
        event.prevent_default()
        event.stop()
        relative = self._relative(node.data.path)
        if relative == ".":
            relative = ""  # 根节点（与 _node_relative 同口径）
        if event.key == "enter":
            # Enter 保持「选中」语义（Space 是纯菜单入口，不改历史）
            self.post_message(self.PathSelected(relative, False))
        screen_x, screen_y = self._menu_anchor()
        self.post_message(
            self.PathContextRequested(
                relative,
                self._absolute(node.data.path),
                is_dir=node.allow_expand,
                screen_x=screen_x,
                screen_y=screen_y,
            )
        )

    def _menu_anchor(self) -> tuple[int, int]:
        """键盘菜单锚点：与光标行同高、侧栏右缘（不遮住目标行）。

        树是逐行渲染（1 cell = 1 line），屏幕 y = 内容区顶 + 光标行 - 垂直滚动；
        夹进可视行范围，避免滚动中/空树时把菜单推出屏幕。
        """
        content = self.content_region
        offset = int(self.scroll_offset.y)
        row = max(0, min(self.cursor_line - offset, max(content.height - 1, 0)))
        return content.right, content.y + row

    # ---------- 定位（v0.4.3：图片预览关闭后回位） ----------

    def select_path(self, absolute: str) -> bool:
        """选中并滚动到某个**已加载**节点；找不到返回 ``False``。

        只沿内存里已有的层级走：路径中的目录没展开过就放弃——为了移一次光标
        去触发懒加载（可能扫盘 / 走网络盘）不划算。图片预览是从该目录的行打开
        的，同级节点必然已加载，所以正常路径都能命中。
        """
        try:
            relative = Path(absolute).resolve().relative_to(
                self._root_path.resolve()
            )
        except (ValueError, OSError):
            return False
        node = self.root
        for part in relative.parts:
            child = next(
                (
                    candidate
                    for candidate in node.children
                    if candidate.data is not None
                    and Path(candidate.data.path).name == part
                ),
                None,
            )
            if child is None:
                return False
            node = child
        try:
            self.select_node(node)
            self.scroll_to_node(node, animate=False)
        except Exception:  # noqa: BLE001 —— 节点已被刷新掉（teardown 竞态）
            return False
        return True

    # ---------- 路径 ----------

    def _absolute(self, path: Path) -> str:
        """绝对路径（右键菜单「复制路径」用）。

        ``resolve()`` 与 ``_relative`` 保持同一套身份口径；路径已消失也不报错
        （``strict=False``），失败时退回原路径字符串。
        """
        try:
            return str(Path(path).resolve())
        except OSError:
            return str(path)

    def _relative(self, path: Path) -> str:
        """相对树根的路径（供 ``git log -- <path>`` 用）。

        ``resolve()`` 两侧都做一遍：树里的子路径来自 ``iterdir()``（已 resolve），
        而根路径未必（macOS 上 ``/tmp`` → ``/private/tmp``），不统一会
        ``relative_to`` 失败。失败时退化为文件名（面板标题仍可用）。
        """
        try:
            return str(Path(path).resolve().relative_to(self._root_path.resolve()))
        except (ValueError, OSError):
            return Path(path).name
