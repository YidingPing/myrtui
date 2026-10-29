# myrtui 测试说明

## 运行

```sh
uv run --extra dev pytest tests/ -q -n 4   # 全量（P0..P3 + v0.7..v0.37），本机 8 核约 1 分钟
.venv/bin/python -m pytest tests/ -q                 # 串行（约 5 分钟，排查并行干扰时用）
.venv/bin/python -m pytest tests/test_p2.py -q       # 单文件
```

并行（`pytest-xdist`）是安全的：tmux socket 名带 uuid、项目根走 `tmp_path`、
`cli._data_dir` 被指到 `tmp_path`，没有共享固定路径。`-n` **不要写进
`addopts`**——单文件调试会被多 worker 拖慢；CI（2 核）固定 `-n 2`。

约定：

- 后端测试跑在**隔离 tmux 实例**上（`-L myrtui-test-<随机>`，`-f /dev/null`），
  fixture 末尾 `kill-server`，不触碰既有会话；
- App 级测试用 Textual headless driver（`App.run_test(size=...)` + `Pilot`），
  同样对接隔离实例；
- **App 的项目根用 `app_root` fixture，不要用 `tempfile.gettempdir()`**：
  系统临时目录会积累条目（实测 2.8 万个），文件树挂载要把根目录整棵渲染一遍
  → 每个 App 用例凭空多 1.5~2s，且耗时随机器使用线性恶化、本地与 CI 不一致；
- **并行下已知的易碎点**：「`create_session` 后立刻 `send_text`」的 capture 类
  用例，shell 未就绪时输入会被 tmux 丢弃，只能等 5s 轮询超时。`-n 2` 实测稳定；
  若把 `-n` 提到 4+ 撞上这类失败，先给该用例加「就绪握手」（轮询到 capture
  非空再发键），**不要靠加大 sleep**；
- 含真实子进程/子 shell 的用例依赖 `sleep`/轮询，串行全量 1 分钟级属正常。

### v0.11（分隔线）相关约定

- `tests/test_v11.py` 用**假后端**（只给拓扑，不碰 tmux）——布局逻辑与 tmux
  无关，跑得快也稳定；
- 模拟拖拽要**自己发 `MouseMove` 且 `button=1`**：`Pilot.hover` 发的是
  `button=0` 的悬停事件，模拟不了「按住拖动」；
- 断言「焦点边框真的画成粗绿、失焦退回细灰」要取**屏幕上该列的实际字符与实际
  前景色**（`screen._compositor.render_strips()` → segment 的 `style.color`），
  只看 `styles.border` 状态位会漏掉「visual 缓存没失效」「CSS 规则没生效」
  这类回归（v0.11.2 起焦点视觉在 `PaneWidget` 的永久边框上：`:focus` →
  `heavy $success`；分隔线恒为细线，`Separator.heavy` 已删除）；
- **`Pilot` 的鼠标事件绕过 `App.on_event`** → headless 里**不会生成 click**；
  要测「拖分隔线不该产生 click」必须自己 `app.post_message(MouseDown/MouseUp)`。

### v0.30（目录右键刷新）相关约定

- 项目根放在 `tmp_path/proj`——状态文件不能落在项目根里，否则会出现在文件树里
  干扰行号断言；
- **`dismiss` 返回值陷阱**：消息处理器 / 定时器里 `self.dismiss(...)` 必须是
  **语句**；`return self.dismiss(...)` 会让 Textual await 返回的 `AwaitComplete`
  并抛 `ScreenError`（8.2.8 实测）；
- “已删除目录”用例必须 `shutil.rmtree`（目录里有子文件），并断言刷新的是
  **父目录**（被删节点从树上消失）；`reload_path("no-such")` 才是“节点不在
  树里 → 返回 `False`”的兜底用例；
- **光标断言只刷新子目录**：`tmp_path` 在 macOS 是 `/var` → `/private/var`，根
  路径未 `resolve()` 时根刷新 / 全树 `reload()` 会把光标回落到根行；刷新根只
  断言新文件出现；
- 弹框输入 `Input.value = ...` 后 `pause` 触发 `Changed`；`Static.content` 取
  预览 / 提示文案（可能是 `Text`，`str()` 后比较）；
- 80×24 用例锁定“长目录 + 长路径下两个按钮仍在首屏”；改弹框行数（标签、
  提示行、`max-height`）必须跑它。

### v0.35（拖拽选字 + 自动复制）相关约定

- **默认会写剪贴板**：左键拖拽松手即自动复制。App 级用例凡是拖拽，要么
  `app._copy_on_select = False`（只测选区 / 手动复制），要么 monkeypatch
  `clipboard.copy_to_system_clipboard` 并断言调用；**绝不碰真实系统剪贴板**；
- **SSH / 开关环境隔离**：`clipboard.is_remote_session()` 读 `SSH_CONNECTION` /
  `SSH_CLIENT` / `SSH_TTY`；`conftest.py` 的 autouse fixture 默认清空三者，
  并清空 `MYRTUI_COPY_ON_SELECT` / `MYRTUI_DRAG_SELECT`（用户 shell 的全局
  设置不能让测试语义漂移）。测 SSH / 逃生舱语义的用例在用例体内
  `monkeypatch.setenv(...)` 即可；
- **点击在松手时合成**：旧转发用例（拖拽可视化、跨 pane 配对、侧栏释放兜底）
  必须 `monkeypatch.setenv("MYRTUI_DRAG_SELECT", "app")`；默认 `always` 下
  左键按下不注入、松手只发一次 `press+release` 拼接串；
- **手势与真实 tmux**：`pilot.mouse_up()` 会先补一条 `button=1` 的 MouseMove，
  选区终点因此正确；跨 widget 拖拽用 `pilot.mouse_up("<目标 widget>", offset)`
  并注意**向上/向左**会触发 Textual 的 start/end widget 交换（pressed pane
  拿到 `Selection(None, S)` → 复制「pane 顶部 → 按下点」，这是合法部分）；
- **冻结断言**：拖拽中改 `backend.text` 并 `_request_poll()` → 冻结 pane 的
  `_lines` 不变；解冻后（600ms 高亮结束）下一轮 poll 才恢复；
- **超时不要真等**：直接调 `app._thaw_after()` 模拟 30s / 10s 定时器到点。

### v0.29（`Ctrl-a ?` 快捷键浮层）相关约定

- 浮层有两条来源：鼠标悬停（定时器）与键盘（pinned）。**键盘打开后
  `_help_pinned=True`，鼠标离开的宽限定时器必须直接返回**，否则「指针扫过
  `?`」会把用户正在读的浮层关掉；
- 测试里用 `pilot.press("ctrl+a")` + `pilot.press("question_mark")`（Textual
  的键名映射，不是 `"?"`）；开关两次后断言 `_help_pinned is False` 防状态泄漏；
- 前缀分支里 `?` 必须在**通用 `_hide_shortcut_help()` 之前**分流——先收起再
  toggle 会看到「已隐藏」又打开，第二次按关不掉；
- 80×24 是完整可见的底线：v0.28 后正文 21 行 + 边框 2 行。

### v0.28（菜单注入到当前 pane）/ v0.31（打开）相关约定

- **命令纯逻辑**：`tests/test_v28.py` / `tests/test_v31.py` 直接测
  `myrtui.pane_commands`，用 `which=` 注入假 `micro` / `ov` / `less`，
  用 `env=` 显式传编辑器 / 分页器环境变量，不要求本机安装这些工具；
- **与开发机环境解耦**：编辑器探测会读真实 `$MYRTUI_EDITOR` / `$EDITOR` /
  `$VISUAL` / `$MYRTUI_PAGER`，App 级用例必须先
  `monkeypatch.delenv(..., raising=False)`，否则本机装了 micro / vim / ov 就会
  让菜单标签与注入命令随机器变化；
- **App 级**：`_FakeBackend.run_command` 记录注入命令；`poll` 要给每个 pane
  返回 `CaptureResult`，否则 `_poll_render` 会跳过状态（`dead` / `command` /
  `alternate_on`）更新；
- **入口**：右键 / `Enter` / `Space` 都能调出同一个菜单；`Enter` 文件行保留
  「选中 + 切 Git 历史」，`Space` 与右键一样不改历史。双击 / `Ctrl-a o` 的
  回归用「不产生 run_command」断言；`Ctrl-a o` 应像其它未定义前缀键一样把
  `o` 透传给 shell；
- **复制兜底**：`_FakeBackend.show_buffer` 返回固定文本；`Ctrl-a c` 只在
  「无 Textual 选区 + pane 门控级别 ≠ NONE」时才读它。真 tmux 侧测
  `set-clipboard on` 与 `list-buffers`/`show-buffer -b`，注意显式
  `-b name` 建的 buffer **不会**成为裸 `show-buffer` 的默认目标；
- 真实 `termtex` / micro / ov 只做人工冒烟，不进自动化。

### v0.32（编辑器探测简化 / 依赖代装）与 v0.4.4（PyPI 发布）相关约定

- 编辑器自动探测只认 `micro` → `vim` → `vi`；`test_v31` 里显式断言
  「即使装了 hx / nvim，也不自动选中」，避免以后又把它们加回探测链；
- `tests/test_install_script.py` 只做静态检查（`sh -n` + 关键字符串），
  真正装包需要网络 / 包管理器，不进自动化；
- 安装源改为 **PyPI**（v0.4.4）：该文件还额外断言脚本的可执行部分不再出现
  Gitea / 内网地址 / token 一类字样，防止旧逻辑回流；
- 每个版本的变更说明写在 **GitHub Releases**（`--generate-notes` 自动生成 +
  手写摘要），仓库内不再维护 `docs/releases/` 与对应的生成器。


### v0.36（Markdown 预览 + Unicode 公式）相关约定

- **不探测真 termtex**：`tests/test_v36.py` 的 autouse fixture 把
  `mathrender.probe_termtex` 替成 `None` 并每个用例 `reset_state()`；需要
  termtex 成功路径时用 `_FakeTermtex`（继承 `TermtexRenderer`，
  `prewarm_document` 的 `isinstance` 闸门才放行）+ monkeypatch
  `select_backend`，不要真的 spawn 子进程；
- **块级缓存键**：`MathBlock` 用 `mathrender.clamp_width(app.size.width)` 查
  缓存，键是 `(token.content, width)`，包含 `math_block` 的首尾换行；测试预
  写 `_CACHE` 时必须用同一个 `token.content`，否则会永远 miss、看到 LaTeX
  原文；
- **512 KiB 双闸门**：入口闸门在 `_context_menu_closed("preview")` 推屏前，
  屏内 `_read_and_render` 保留同一条检查作竞态兜底。测试大文件时两种路径都要
  覆盖（`test_large_markdown_is_blocked_before_push` /
  `test_screen_guard_blocks_oversize_file`）；
- **私有 API**：v0.36 的 `MathBlock` 继承 `textual.widgets._markdown.MarkdownBlock`；
  v0.37 起改为普通 `Vertical` 容器，**不再依赖该私有类**。升级 Textual 后仍建议
  先跑 `test_v36.py` / `test_v37.py`；
- **菜单文案**：`test_v22` 的快捷键浮层 needle 已含「打开 / 预览 / 渲染」；
  改侧栏文案必须同步 `tests/test_v36.py::test_sidebar_shortcut_text_is_synced`
  与 `test_v22`；
- **入口不依赖 pane**：`preview` 不查 `_pane_problem`；现有 `test_v28` /
  `test_v31` 的「打开 / 渲染」断言已按新菜单 id / 文案改写。


### v0.37（Sixel / TGP 位图）相关约定

- **位图依赖可能不在**：`textual-image` / `Pillow` 从 v0.3.1 起是条件硬依赖
  （Python ≥3.12 自动装，3.10/3.11 没有），TeX 也可能不在。模块级绝不
  import 它们——`mdpreview._image_widget_class()` 里才 import
  `textual_image.widget`，`mathimg` / `imageprep` 的 Pillow 也在函数内 import；
  测试用 `pytest.importorskip("PIL.Image")` 或注入 fake，**不允许**把
  `[graphics]` 加进 `[dev]`；
- **能力一律注入，不探测真终端**：`Capabilities(...)` 直接构造（或用
  `graphics.detect(env=..., probe=..., tty=...)` 注入假 probe），不要在没有
  伪终端的情况下跑真 `probe_terminal()`（它会读 stdin，且在 pytest 里非 tty）；
- **位图控件替身**：`monkeypatch.setattr("myrtui.client.mdpreview._image_widget_class",
  lambda caps: _DummyImageWidget)`，这样连 `textual-image` 都不用装就能测
  「有位图 → 挂 Image / 无位图 → termtex 文本 / alt」；
- **LaTeX 工具链**：`tests/test_v37.py` 用 `_fake_runner` 伪造
  `pdflatex`/`pdftocairo` 的返回码与产物（`math.pdf` + `page-N.png`），
  锁定批量 / 逐条重试 / 失败 once 语义；真实管线只用一条
  `@pytest.mark.skipif(shutil.which(...))` 的冒烟用例，本机有 TeX 才跑；
- **缓存要隔离**：autouse fixture 里 `mathimg.set_cache_root(tmp_path/"cache")`
  + `reset_state()`，否则会读到开发机 `~/Library/Caches` 里的真实产物，
  出现「本地绿、CI 红」；用例结束 `set_cache_root(None)`；
- **透明语义**：`imageprep` / `mathimg` 产出的 PNG 必须是**不透明 RGB**
  （预合成到块背景色），断言 `mode == "RGB"`；Sixel 的 P2=1 透明模式在
  Tabby 上表现是「露出控件背景」，不要依赖它；
- **容器断言**：`MathBlock` / `ImageBlock` 是 `Vertical`，文本在子
  `Static` 里——断言用 `block.query_one(Static).content`，别再假设
  `MathBlock._content`；`Static.content` 在 8.2.8 可能是 `str` 或
  `Content`，统一 `str(...)` 再比。


### v0.37.1（KaTeX 公式位图）相关约定

- **不启动真 Chrome**：`tests/test_v37_1.py` 用 fake `printer`（写一个
  `%PDF-1.4` 假文件）+ fake `runner`（写 `page-N.png`），断言「一次 Chrome
  出整篇」「缓存命中不再启动」「页数不符逐条重试」「内容触边时放大重试」；
  需要真实管线时只留一条 `@pytest.mark.skipif(katex.find_chrome() is None)`；
- **后端要显式注入**：`mathimg.select_backend()` 会探测本机 Chrome / TeX；
  v0.37 的 autouse fixture 把它替成 `(None, None)`，避免预览用例意外 spawn
  Chrome（否则 `pilot.pause(0.6)` 会等不到渲染结果）。要测后端选择的用例
  自己 monkeypatch `katex.find_chrome` / `mathimg.find_tools` / `find_converter`；
- **缓存键含后端与 cell**：`cache_key(..., kind="katex", cell=(10,20))` 还混入
  KaTeX 版本；换 cell（`MYRTUI_CELL_PX`）或换后端都会 miss，这是预期行为，
  别在测试里复用别的后端命名的键；
- **错误公式留空页**：KaTeX 用 `throwOnError: true` + try/catch，失败页
  alpha bbox 为空 → `_flatten_to_rgb` 抛「公式产物为空」→ 该条标记失败并回落
  termtex。测试请断言「失败条目不在返回 dict 里」，不要断言红色 `katex-error`；
- **尺寸口径**：1em = `cell_height / 1.2`；`katex.raster_dpi(em)` 给 2× 超采样
  的 DPI；产物最终是 1×、不透明 RGB。**字号倍率** `mathimg.math_img_scale()`
  默认 `DEFAULT_SCALE=1.2`（x-height 补偿）并进缓存键——测尺寸/键值时用
  `terminal_em_px()`，别写死 16.7 之类的中途值；
- **图片预览（v0.4.3）**：切换用例走 `app.run_test`，没有 `textual-image` 时
  也能断言（只会显示"没有位图协议"提示，header 照常更新）；断言计数用
  `screen._header_text`，当前项用 `screen.current`。`sibling_images()` 是纯函数，
  直接给临时目录即可测排序 / 过滤 / 目标缺失；菜单断言用 `OptionList.options`
  的 id 列表，别按位置硬编码。
- **图片盒子**：`_display_cells` 宽高都向上取整（盒 ≥ 图），图片由
  `textual-image` 等比放进盒子；测试锁「不放大、超宽/超高等比缩小、盒子
  不小于图」，不要再断言已删除的公式补边函数；
- **LaTeX 模板**：`mathimg.build_tex` 只给**含 CJK 的页**包 `CJK*`（`gbsn`）
  并加载 `CJKutf8`；纯数学文档不得出现 `CJK`（有测试锁死）。改模板要 +1
  `mathimg.LATEX_STYLE_VERSION`（进缓存键）；
- **资产**：`client/htmlexport_data/` 是 vendored 静态文件（KaTeX 0.19.1），
  改 HTML 生成时保持**零 `http(s)://`**，并保留 `</script>` 转义测试；
- **CI 通常有 `[graphics]`，但不能假设**：Python 3.14 的 CI 会随主依赖装上
  `textual-image` / `Pillow`，3.10/3.11 环境没有。任何依赖 `PIL` /
  `textual_image` 的用例必须 `pytest.importorskip("PIL.Image")` /
  `pytest.importorskip("textual_image")`
  （图片盒子换算、LaTeX 排版的尺寸归一、位图控件挂载都属于这一类）；
- **LaTeX 排版（v0.37.2）**：`docpage.render_pages` 是**唯一** spawn 点（worker 调用），
  `cached_pages` 只 stat；测试一律 fake `katex.find_chrome` + `mathimg.find_converter`
  + printer/runner（会真的写假 PNG），别让用例去开 Chrome。页面尺寸断言用
  `page_spec()`，别写死 1100；`_normalize_page` 会把 Chrome 的 1px 误差裁掉，
  所以假页故意做成 781 高来锁这条；`pageview` 的用例用 `app.run_test`，滚动
  断言读 `scroll_target_y`（平滑滚动期间 `scroll_y` 还在动）；
- **页面配色（v0.4.1）**：断言颜色用 `docpage.PAPER_PAGE` / `PAGE_THEMES[...]`
  取，别写死 `#FFFFFF`；`tests/test_v37_2.py` 的 autouse fixture 会清掉
  `MYRTUI_PAGE_THEME`，避免开发机的环境变量把默认值换掉。透明页的合成由
  `_normalize_page` 负责，用例里造一张全透明 PNG 断言角落像素 = 页面底色；
- **公式只走 termtex**：行内 `-inline`、块级 `-width`，文本预览不再调用
  `mathimg` 公式位图；`$...$` 独占一段也保持行内（`sole_inline_math` 只作为
  独立工具保留，不再参与预渲染 / token 改写）；无渲染器时不动原文；
- **回落诊断**：失败公式记在 `mathimg.last_failures()`（`reset_state()` 清空，
  同源同因去重）；`MYRTUI_MATH_IMG_DEBUG=1` 时 `prewarm_document` 会写
  `<cache>/mathimg/failures.log`。测回落的用例优先断言这个列表，不要去解析日志文本。


### v0.20（文件树右键菜单）相关约定

- **textual 私有处理器沿 MRO 逐类执行**：覆写 `Tree._on_click` **不能**替换
  基类实现——右键分支必须 `event.prevent_default()` 才能拦住基类的 toggle，
  非右键分支**不能**显式 `await super()._on_click()`（否则左键跑两遍）；
- 同理，测试里覆写 `on_file_tree_path_*` 之类的**记录型 App** 不要调 `super()`
  ——基类处理器会由 MRO 自然执行，显式 super 会让菜单被 push 两层；
- 菜单「点外关闭」必须测 **MouseDown**（点空白处 App 不记 `_mouse_down_widget`，
  不会生成 Click）；
- 「热区不截获」必须自己 `app.post_message(MouseDown/MouseUp)`（pilot 绕过
  `App.on_event`）；`tests/test_v20.py` 已按这两条写好模板。

## ⚠️ 环境陷阱：Textual 运行期间接管 stdout / stderr

**在 `App.run_test()` 内部 `print()` 的内容要等 App 退出才到达管道。**

因此**不可用「读子进程 stdout」作为 App 级测试的同步手段**——会拿到假阳性：
你以为「启动 App 后锁被释放 / 另一进程秒拿锁」，实际只是 print 被积压到
App 结束后才输出（P1 验收报告 §6 记录了一次真实踩坑与撤回）。

**正确做法**（任选其一）：

- **文件心跳**：后台线程/子进程写一个时间戳文件，主测试轮询该文件
  （不受 stdout 捕获影响，可拿到毫秒级先后次序）；
- **直写 fd 2**：`os.write(2, ...)` 或 `sys.__stderr__.write(...)`，
  绕过 Textual 对 `sys.stdout` / `sys.stderr` 的替换。

详见私有仓 `myrtui-internal` 的 `docs/P1-ACCEPTANCE.md` §6（内部验收记录，不随公开仓发布）。
