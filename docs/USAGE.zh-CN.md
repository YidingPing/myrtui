# 使用手册（简体中文）

[English](USAGE.md) | 简体中文

面向单个项目的终端工作台。Python（Textual）只做 UI 与编排，终端仿真交给 tmux。

一句话：**在某个项目根目录下启动 `myrtui`，左侧是文件树 + Git 提交历史，右侧是最多 4 个真实 shell 终端 pane，支持鼠标、detach/attach。**

> 它不是 tmux 的替代品——底层就是 tmux；myrtui 只是把「一个项目 = 一个会话」
> 这件事做成了默认行为，并把文件树、Git 历史、预览顺手放在同一个屏幕里。

## 安装

需要外部 `tmux >= 3.1`（`capture-pane -N` 为 3.1 新增）：

```sh
# macOS
brew install tmux
# Linux (Debian/Ubuntu)
sudo apt install tmux

pipx install myrtui
myrtui --version          # 校验安装结果
```

也可以用带依赖引导的脚本（自动检测平台、补齐 tmux、逐项询问可选组件）：

```sh
curl -fsSL https://raw.githubusercontent.com/YidingPing/myrtui/main/scripts/install.sh | sh
```

脚本可用的环境变量：`MYRTUI_VERSION`（固定版本，如 `0.4.4`）、`MYRTUI_PREFIX`
（venv 兜底安装位置）、`MYRTUI_DEPS`（`ask` 默认 / `yes` / `no`：是否尝试自动
安装 tmux 与推荐组件）。拒绝或安装失败不影响 myrtui 本体，功能自动降级。

- 开发安装：`pip install -e .`（或 `uv sync`）；
- wheel / sdist 随 tag 自动发布到 PyPI 与 GitHub Releases；
- 不支持 Windows（`cli.py` 依赖 `fcntl`，且 tmux 无原生 Windows 版）；
- **运行依赖只有 Python 与 tmux**（Python 包：Textual / platformdirs /
  mdit-py-plugins；Python ≥3.12 的安装还会自动带上位图依赖 `textual-image` +
  `Pillow`）；「打开」调用你本机的编辑器
  （`$MYRTUI_EDITOR` / `$EDITOR` / `$VISUAL`，否则按 `micro` → `vim` → `vi`
  探测）；Markdown 预览在应用内渲染，公式调用可选的 `termtex`；只读查看同样是
  可选外部命令——`ov`（滚轮/选区/搜索，没有则用系统的 `less`），缺任一工具时
  自动降级（见「打开 / 预览」）；`termtex` 的 Go 工具链仅用于安装它，
  **不是运行依赖**；
- 预览位图（Sixel / TGP）依赖 `textual-image` + `Pillow`：**Python
  ≥3.12 的安装会自动带上**；3.10/3.11 装不了（`textual-image` 限制），预览
  一直走 termtex / 原文，功能自动降级；终端不支持 Sixel/TGP 时同理。

## 升级

```sh
pipx upgrade myrtui        # uv tool upgrade myrtui / pipx 之外按各自通道升级
```

用安装脚本装的也可以重跑同一条命令（脚本会**探测既有通道并原地升级**，
不会掉进 venv 兜底去覆盖 `~/.local/bin/myrtui` 里已有的 shim）：

```sh
curl -fsSL https://raw.githubusercontent.com/YidingPing/myrtui/main/scripts/install.sh | sh
```

开发安装（`pip install -e .` / `uv sync`）的更新就是拉代码后重新同步：

```sh
git pull && uv sync        # 或：pip install -e .
```

## 使用

```sh
cd some-project
myrtui                  # 或 uv run myrtui
```

- 首次运行会在该项目目录创建一个隔离的 tmux 实例（socket 名由项目**物理路径**的
  sha256 前 16 位派生，符号链接会归一化到真实路径）；
- 再次运行同一目录 = attach 回同一会话，恢复离开时的画面；
- 同一目录同时只能运行一个实例（flock 互斥，第二个会提示「该目录已有实例在运行」，
  并触发**接管让位**——第二个成为前台，第一个前端干净退出、shell 无损）；
- **不同项目目录 = 完全并行的独立实例**（锁 / tmux socket / 布局各自隔离），
  可在任意目录用 `myrtui --root /path/to/proj` 启动另一个项目的实例。

### 启动参数

```sh
myrtui --root /path/to/proj    # 项目根覆盖（先 realpath 再派生身份/锁/socket/侧栏）
myrtui --shell /bin/fish       # pane 的 shell（默认 $SHELL，退化到 /bin/zsh）
myrtui --scrollback 20000      # 历史深度 history-limit（默认 5000，范围 1..1000000）
myrtui --lang zh_CN            # 本次运行的界面语言（默认 en；也可写进 MYRTUI_LANG）
```

`--shell` / `--scrollback` 对**已存在的会话只影响之后新建的 pane**（tmux 在
window/pane 创建时快照选项，`Ctrl-a r` 重开的也是建窗时记录的命令）——这种情况下
启动时会给出提示。

### 环境变量

| 变量 | 作用 |
|---|---|
| `MYRTUI_LANG` | 界面语言：`en`（默认）/ `zh_CN`。优先级：`--lang` > `MYRTUI_LANG` > `config.json` 的 `lang` > `LC_ALL`/`LANG` 是否 `zh*` > `en`；改完重启生效。 |
| `MYRTUI_COMPACT` | 窄屏布局：`auto`（默认）= 宽 < 60 列或高 < 16 行进入紧凑档、宽 >= 64 且高 >= 18 才退出（带滞回，软键盘弹收不来回闪）；`on` 强制紧凑；`off` 保持旧布局。 |
| `MYRTUI_KEYBAR` | 紧凑档 3 行高触摸底栏（3×3 cell 键 + 1 cell 空隙，靠左）：pane 里是 AI coding 键，文件树 / Git 整页换成导航 / `≡` / `ENT`；点按闪 150 ms；顶栏 `KEY` 只开关本次运行；`off` 永久关闭（`KEY` 一起隐藏）。 |
| `MYRTUI_IDLE_INTERVAL` | 空闲轮询间隔（秒，float，默认 `0.15`；非法/非正值回落默认，合法值夹取 `0.05–5.0`）。画面持续变化时（`tail -f`、图像动画）myrtui 自己提到约 17 次/秒，静止后回落，不必手动调。多实例时给**后台/次要实例**降频：`MYRTUI_IDLE_INTERVAL=0.5 myrtui`（空闲抓屏 6.7 → 2 次/s，系统级 fork 与 CPU 同降；显式调大后不再自动提频）。活跃实例保持默认即可。 |
| `MYRTUI_TRANSPORT` | tmux 传输方式：`auto`（默认，常驻 `tmux -C` 客户端；建不起来时自动回落逐次起进程）/ `exec`（每次调用起一个 tmux 进程，旧路径）/ `control`（强制常驻，建不起来直接报错）。非法值告警后按 `auto`。 |
| `MYRTUI_LOG` | TUI 接管终端后 warning 写到哪里（默认平台日志目录：macOS `~/Library/Logs/myrtui/myrtui.log`、Linux `~/.local/state/myrtui/log/`）；空串 = 不落盘。启动前的 CLI 错误仍走 stderr。 |
| `MYRTUI_LOOP_DEBUG` | `=1` 时把事件循环迟到 > 30ms 的事件写到 stderr（性能遥测，排障/验收用，默认关闭、无行为影响）。 |
| `MYRTUI_MOUSE_DEBUG` | `=1` 时把每个鼠标事件与拖拽结论写进 `<tmp>/myrtui-mouse-debug-<pid>.log`（排障用，默认关闭）。 |
| `MYRTUI_COPY_ON_SELECT` | 左键拖拽松手后是否**自动复制**（默认 `1`；`0/false/no/off` 关闭）。关闭后选区保持高亮 10s，按 `Ctrl-a c` 复制，其他键 / 点击 / 滚轮取消。 |
| `MYRTUI_DRAG_SELECT` | `smart`（默认）= 真终端约定：pane 内程序请求了鼠标就把拖动转给它（vim `mouse=a`、`ov` 拖选、opencode 选择即复制），**Shift + 拖动**才由 myrtui 选字；`always` = 左键拖拽**永远**由 myrtui 选字（v0.35 行为）；`app` = 只要程序请求了鼠标就转给它，没有 Shift 逃生口。 |
| `MYRTUI_CLIPBOARD` | 复制通道：`auto`（默认，OSC 52 + 系统命令都发）/ `osc52`（只发 OSC 52）/ `system`（只调系统命令）。非法值告警后按 `auto`。 |
| `MYRTUI_CLIPBOARD_PASSTHROUGH` | 是否把 **pane 内程序发起的复制**（OSC 52）转发到终端 / 系统剪贴板（默认开；`0/false/no/off` 关闭）。 |
| `MYRTUI_OUTER_MUX_HINT` | 启动时检测到外层 tmux / screen 是否提示一次排障文案（默认开；`0/false/no/off` 关闭）。 |
| `MYRTUI_GRAPHICS` | 预览位图协议：`auto`（默认，启动时探测终端）/ `sixel` / `tgp` 强制 / `unicode` / `off` 关闭。缺 `textual-image`、非 tty、探测失败都等同 `unicode`；非法值告警后回落 `auto`。只影响图片与 LaTeX 整篇版面。 |
| `MYRTUI_CHROME` | 指定 Chrome/Chromium（`v` LaTeX 整篇排版用；默认常见安装路径 → `PATH` 探测）。 |
| `MYRTUI_CELL_PX` | 覆盖终端单元格像素尺寸（如 `10x20`）；终端不回报 cell 尺寸时用它校正图片与整篇版面的像素换算。 |
| `MYRTUI_PDFTOCAIRO` | 指定 PDF→PNG 转换器（`v` 整篇排版用；默认优先 `pdftocairo`，其次 `pdftoppm`）。 |
| `MYRTUI_PAGE_THEME` | `v` 排版页面配色：`sepia`（默认米黄纸 `#E6DABE` / `#3D3427`）、`paper`（`#F7F4EC`）、`gray`、`white`（v0.4.0 纯白）、`dim`（夜间低亮度）、`dark`（跟随深色终端）。非法值告警后按 `sepia`。 |
| `MYRTUI_MATH_BACKEND` | Markdown 文本预览的公式后端：`auto`（默认，探测到 `termtex` 就用）、`plain`（强制 LaTeX 原文）、`termtex`（指定后端；缺失时告警并回落原文）。非法值告警后按 `auto` 处理。 |

> `MYRTUI_IDLE_INTERVAL` **由用户按实例启动时手动指定**：myrtui 无法可靠感知
> 「终端窗口是否在前台」（终端焦点上报无标准通道），自动降频不可靠，故不做
> 自动切换（内部性能计划 §2.2）。输入高频段（30ms）与鼠标节流不受影响——
> 那是活跃实例的参数。**画面内容在变时的提频不在此列**：它看的是 capture
> 有没有变化，不看焦点，静止后自动回落。

### 实例管理（`ls` / `attach` / `stop`）

```sh
myrtui ls            # 列出实例（默认只显示存活的 running / detached）
myrtui ls --all      # 连同 dead 残留（只有锁文件、无会话）一起显示
myrtui ls --json     # JSON 输出：index / state / root / short_hash / pid
myrtui attach [目标] # 接入实例；目标 = 序号 / hash 前缀 / 项目路径
myrtui stop [目标]   # 结束实例：请前端干净退场后 kill tmux 会话（不强杀）
```

- **序号只对当次 `ls` 输出有意义**：`attach` / `stop` 解析目标时会按同一规则
  重新扫描（排序：项目路径字典序，序号只在存活实例内分配）；纯数字一律按
  序号解释；
- **`attach` 到 running 实例 = 触发接管让位，原终端的前端将退出**
  （不是 tmux 式共享 attach）；detach 后的实例 attach 回去会恢复上次的布局比例；
- `stop` 走协作式退场标记（`<hash>.stop`）：前端 3s 内不响应则报错退出、
  **不强杀**；拿到锁后会先重验「目标没被新前台接管」再结束会话；
- 死掉的实例（`dead`）不占序号、默认不显示；锁文件按设计**永不清理**
  （`attach` / `stop` 到 dead 目标会提示「无存活实例」并退出 2）；
- `attach` 无参数且只有一个存活实例时直接接入；0 个或多个则提示并退出 2。

## 键位（Ctrl-a 前缀）

| 键 | 作用 |
|---|---|
| `Ctrl-a d` | detach：前端退出，tmux 会话与全部 shell 继续存活 |
| `Ctrl-a k` | kill tmux server（带确认），全部 shell 结束 |
| `Ctrl-a t` | 新建 pane（自动进入下一布局档，最多 4 个） |
| `Ctrl-a 1..4` | 切换焦点 pane（越界静默忽略） |
| `Ctrl-a w` | 关闭当前 pane（有程序在跑时先确认；只剩 1 个时 no-op） |
| `Ctrl-a r` | 重开已退出的 pane（焦点 pane 已死则优先它，否则最近死亡的） |
| `Ctrl-a b` | 折叠 / 展开侧栏（折叠后 pane 变宽，tmux 尺寸同步联动）；紧凑档：打开 / 关闭文件树整页 |
| `Ctrl-a s` | 焦点移入侧栏（文件树）——键盘用户进入侧栏的入口；紧凑档：打开文件树整页 |
| `Ctrl-a g` | 焦点移入侧栏的 **Git 历史**面板（↑↓ 移动，`Esc` 回终端）；紧凑档：打开 Git 历史整页 |
| `Ctrl-a c` | 复制当前**选区**（拖拽松手已自动复制，本键用于手动再复制 / 关闭自动复制时）；无选区只提示 |
| `Ctrl-a v` | 粘贴：**本地优先系统剪贴板**，读不到才回落 myrtui 内部缓冲；**SSH 会话**改为提示用终端自己的粘贴键（macOS `Cmd+V` · Windows Terminal `Ctrl+V` · Tabby `Ctrl+Shift+V`） |
| `Ctrl-a ?` | 显示 / 收起快捷键浮层（等价于点击或悬停顶栏 `?`；tmux 同款语义） |
| `Esc` | 焦点在侧栏时 → 回到当前活动 pane |
| `Ctrl-a` 后超时 1s | 把 `Ctrl-a` 本身发给内层 shell |
| 其他一切键 | 直接透传给内层 shell（方向键的 DECCKM 由 tmux 自决） |

紧凑档底部是 3 行高的常用键栏：pane 里发送 `ESC` / `ENT` / `⇧ ⇥` / `/` / `@` /
`↑` / `↓` / `C-J` / `DEL`；文件树和 Git 整页会换成各自的按钮。
顶栏 `KEY` 只开关本次运行；`MYRTUI_KEYBAR=off` 永久关闭。

文件打开动作都在**文件树上下文菜单**里：文件行 `打开（编辑器）` / `预览 Markdown`
（仅 md）/ `预览图片`（仅图片，且不再有「打开」）/ `复制相对路径` /
`复制绝对路径`；目录行是
`刷新此目录` / `复制相对路径` / `复制绝对路径`。
打开的编辑器按 `$MYRTUI_EDITOR` → `$EDITOR` → `$VISUAL` → `micro` → `vim` →
`vi` 解析，菜单会显示实际工具名；想用 helix / nvim 请显式设置前两个环境变量。
菜单可用鼠标右键，也可全键盘调出
（`Enter` 文件行 / `Space` 任意行，见「左侧侧栏」）。

**忘了键位？** 悬停或点击顶栏 `Kill` 右侧的 `?`，或按 `Ctrl-a ?`（全键盘），
会弹出快捷键浮层（全局 `Ctrl-a` 键 / 侧栏两段）；再点一次 `?`（或再按
`Ctrl-a ?`、点浮层外）收起。指针移进浮层可继续阅读，移开自动收起；键盘 /
点击打开的浮层不会被指针离开误关。

**布局档（v0.12 起列优先）**：1 个 pane = 全宽；2 个 = **上下分**（第二个在
第一个**下面**，同宽）；3 个 = 左2右1（左列上下两个、右列一个满高）；
4 个 = 2×2（左列 pane 1/2、右列 pane 3/4）。

**焦点模型**：焦点在 pane 内时，一切键与粘贴都透传给内层 shell；**焦点在侧栏
内时，键与粘贴都不进 shell**（归侧栏的树/历史列表），`Esc` 回终端。前缀键
`Ctrl-a …` 在任何位置都可用。

pane 内 shell 退出后该 pane 标灰、顶栏标 `N:dead` 并提示 `Ctrl-a r`，此时打字
不会被透传；连续 5 次抓屏失败会在顶栏提示「与会话失去连接…正在重连…」，恢复后
提示「已重新连接」。

鼠标：点击 pane 聚焦；滚轮翻 scrollback（备用屏内忽略，触底自动回实时尾部）。
若 pane 内程序请求了鼠标（如 vim 的 `mouse=a`、opencode 的 1003），**单击 / 滚轮 /
右键 / 中键 / hover** 会按该程序请求的**编码**注入——请求 1006 的收 SGR(1006)，
否则收 xterm 旧 3 字节编码（vim 默认 `ttymouse=xterm` 即此形态），与真实终端发出的
字节一致；**shift+滚轮**随时强制翻 scrollback（逃生舱，mouse-on 程序不会吃掉历史入口）。
单击在**松手时**才到达程序（延迟 = 按住时长，典型 50–150ms），换取「拖拽永远选字」。

**选字与复制（v0.35；0.7.0 改默认）**：拖拽的归属按**真终端约定**
（`MYRTUI_DRAG_SELECT=smart`，默认）——pane 内程序请求了鼠标（vim / htop /
opencode）时，拖动**转给程序**：它自己的选区 UI 能用，它复制的内容也会被转发
回来（见下）；**Shift + 拖动**才是 myrtui 选字。程序没要鼠标时拖动归 myrtui：
**松手即自动复制**（OSC 52 + 系统命令），选区高亮保留 600ms 让你看清复制了什么。
拖拽期间冻结该 pane 的画面，选区不会随 capture 刷新漂移。想要回 v0.35 的
「永远选字」设 `MYRTUI_DRAG_SELECT=always`。

- 复制内容：行尾补白裁掉、中文 / 宽字符按 cell 对齐、只取**按下时那个 pane** 的
  内容（拖到侧栏 / 另一个 pane 也只复制按下 pane 覆盖的部分）；
- 复制通道：OSC 52（iTerm2/WezTerm/Windows Terminal 有效）+ 系统命令
  （`pbcopy`/`wl-copy`/`xclip`/`clip.exe`）——**macOS Terminal.app 不支持 OSC 52**，
  靠后者兜底。两条**默认都发**，用 `MYRTUI_CLIPBOARD=osc52` / `=system` 收窄。
  SSH 会话里系统剪贴板是**远端机器**的：写进去无害，而远程桌面客户端
  （UU 远程、ToDesk 之类）通常会同步——这正是它该被保留的理由；
- **pane 内程序自己的复制也会被转发（0.7.0）**：opencode 的「选择即复制」、
  codex 的 `/copy`、vim 的 `"+y` 都是往 pane 里发 OSC 52，而 tmux 要么把它
  塞进 paste buffer（裸形态）、要么直接丢弃（`$TMUX` 下的 `ESC Ptmux;` 形态）。
  myrtui 从控制通道里把它捡出来，转发给终端与系统剪贴板，并在顶栏提示
  「已从 pane k 复制 N 字符」。`MYRTUI_CLIPBOARD_PASSTHROUGH=0` 可关闭；
  单次转发的上限是 **256 KiB**（解码后），超出只写日志、不提示；
- 自动复制不想要？`MYRTUI_COPY_ON_SELECT=0`：松手保持高亮 10s，按 `Ctrl-a c`
  复制，其他键 / 点击 / 滚轮取消；
- **迁移注意（v0.35 → 0.7.0）**：默认从 `always` 改成 `smart`——vim `mouse=a`
  的拖动可视选择、`ov` 的拖选、opencode 的「选择即复制」现在**开箱即用**；
  代价是这些程序里本地选字要按 **Shift**。想要回旧手感设
  `MYRTUI_DRAG_SELECT=always`；`app` 则连 Shift 逃生口也不要（拖动永远给程序）。
  单击 / 滚轮 / 右键 / 中键的转发三档都不受影响；转发期间 myrtui 不再点亮自己的
  选区，不会出现两套高亮。

**粘贴（v0.14；v0.35 改优先级）**：`Ctrl-a v` 把文本送进当前活动 pane。

- **默认**：**系统剪贴板优先**（`pbpaste`/`wl-paste`/`xclip`/`Get-Clipboard`），
  读不到（无命令 / 失败）才回落到 myrtui 内部缓冲（`Ctrl-a c` 复制过的内容）。
  注意取舍：系统剪贴板里的无关旧内容会排在内部缓冲前面；
- **`MYRTUI_CLIPBOARD=osc52`**：没有可读的剪贴板 → 只提示用终端自己的粘贴快捷键
  （macOS `Cmd+V` / Windows Terminal `Ctrl+V` / Tabby `Ctrl+Shift+V`；拿不到
  `TERM_PROGRAM` 时并列提示），不读系统剪贴板、也不粘内部旧值；
- **右键粘贴（0.8.0）**：**任何** pane 上右键 = 粘贴——与 `Ctrl-a v` 同一条链
  （系统剪贴板 → 内部缓冲）。这是终端约定（Windows Terminal 同款），也填了
  「鼠标上报打开后右键没反应」的洞；**Shift + 右键**才把按钮还给程序，vim /
  opencode 自己的右键行为（默认都没有）因此保留。
- 终端原生粘贴（`Cmd+V` / `Ctrl+V` / `Ctrl+Shift+V`，取决于外层终端）始终
  可用，走同一条 tmux `paste-buffer` 通道（程序请求了 bracketed paste 才会包
  bracket）。

### 在外层 tmux 里（UU 远程、本地镜像）

myrtui 自己可以被套在另一层复用器里——**UU 远程的终端本身就是一个 tmux 3.5a
会话**。外层如果 `mouse on` 且覆盖了 root 表的 `MouseDrag1Pane`，它会在第一次
拖动时进 copy-mode：按下能到 myrtui，**按住期间的 motion 全被吃掉**，松手时
`copy-pipe pbcopy` 把外层选区写进剪贴板。症状是「拖动选不中，但剪贴板里多了点
东西」。myrtui 启动时能识别外层 tmux / screen 并给一行提示；真正的修法在外层
服务端。

把 tmux 的默认绑定恢复回去（要鼠标的程序转发给它，其余仍走 copy-mode）。UU
远程：

```sh
UU="/Applications/UURemote.app/Contents/Helpers/tmux/uuyc-mux"
S="$HOME/Library/Application Support/UURemote/tmux.sock"
"$UU" -S "$S" bind -Troot MouseDrag1Pane \
  if-shell -F '#{||:#{pane_in_mode},#{mouse_any_flag}}' \
  'send-keys -M' 'copy-mode -M'
```

任何 tmux 都是同一条命令（`tmux bind -Troot MouseDrag1Pane …`）；用
`list-keys -T root | grep MouseDrag1Pane` 确认。改完之后：在请求了鼠标的程序上
拖动 → 转给程序（myrtui 再按 `MYRTUI_DRAG_SELECT` 处理）；在普通 shell 输出上
拖动 → 仍走外层的拖选复制。

绑定只存在于外层 server 的运行时，它一重启就没了——再跑一次即可（写进
`~/.zshrc` 的函数最省事；**不要**去改 UU app bundle 里的 `uuremote.conf`，升级
与签名都会踩）。粗暴替代是 `tmux set -g mouse off`：所有鼠标事件都交给程序，代价
是外层自己的拖选复制也没了。

## 在手机上

不足 60 列（或不足 16 行）时自动切到**紧凑档**：侧栏收起、只显示当前 pane
（去掉 1 cell 边框）、顶栏 tab 只留 `1 2 3 4`、按钮压成 `+ X D K KEY ?`
（`X` 关闭当前 pane，`KEY` 开关底栏）。顶栏仍是**单行**；底栏是 **3 行高**、
每键 3×3 cell + 1 cell 空隙，靠左排列（右侧留白，以后加键）：46×22 的手机
竖屏因此是一个 46×18 的 pane，而不是四块 10 列的「看不清」（关掉底栏后
回到 46×21）。窗口变宽后自动恢复；阈值带滞回（进 < 60，退 >= 64×18），
软键盘反复弹收不会来回切布局。

底栏按 **AI coding** 选键：

| 按钮 | 发送 | 用途 |
|---|---|---|
| `ESC` | Esc | 中断生成 / 关弹层 |
| `ENT` | Enter | 发送 |
| `⇧ ⇥` | Shift+Tab | 切 agent / 权限模式 |
| `/` | `/` | slash 命令 |
| `@` | `@` | 文件引用 |
| `↑` `↓` | 方向键 | prompt 历史 / 列表导航 |
| `C-J` | Ctrl-J | 换行不发送（所有终端都可用） |
| `DEL` | Backspace | 纠正误点的前缀 |

每个键 3×3 cell（整块都是热区），点按闪 150 ms accent。极窄屏按优先级
从尾部丢键（pane 行含空隙共 35 cell；40/49 列显示全部 9 个，30 列仍能
放 7 个）。`Tab`、`Ctrl-C`、左右方向键刻意不放——它们要么仍然需要键盘
输入，要么与 `ESC` 重叠。

紧凑档打开**文件树整页**时，底栏换成 `ESC` / `↑` / `↓` / `ENT` / `≡`
（Space，打开右键菜单）。`ENT` 保持键盘语义：文件行选中 + 弹菜单，目录行
展开 / 收起；`≡` 在任意行弹菜单（这是手机上访问目录「刷新此目录 / 复制路径」
的唯一入口）。打开 **Git 历史整页**时换成 `ESC` / `↑` / `↓` / `ENT`：
`ENT` 显示当前高亮提交的完整卡片（标题、正文、sha、作者、日期），替代在整页
宽度下没有落脚处的悬停卡片。再点一次 `ENT` 或点卡片外关闭。

如果你的手机终端已经自带完整的一套常用键，`MYRTUI_KEYBAR=off` 永久关闭底栏
（`KEY` 开关一起隐藏）；默认 `auto` 下 `KEY` 只改变本次运行。

- `Ctrl-a b` 把**文件树**当整页打开（pane 区隐藏）；再按一次、按 `Esc`、或点
  顶栏 tab 返回。
- `Ctrl-a s` / `Ctrl-a g` 分别打开文件树 / Git 历史的**各自整页**——另一块
  隐藏，树拿满高度，右键菜单才有完整空间。
- `Ctrl-a 1..4` 或点 tab 切换可见 pane。被隐藏的 pane 保持原 tmux 尺寸，切回去
  时重排一次。
- 紧凑档不能拖分隔线（线全隐藏）；`layout.json` 里的比例不动，宽屏恢复后照旧。

`MYRTUI_COMPACT=on` 在任意尺寸强制紧凑档；`off` 立刻回到旧布局（不需要回退
代码）。

## 左侧侧栏（文件树 + Git 历史）

- **文件树**：启动即见「项目根 + 第一层子项」；**点目录行**逐层展开/收起（懒加载：
  目录扫描在**线程** worker 里跑，展开大目录不卡 UI）；**点文件行**仅选中（高亮），
  不向 pane 注入任何字符；文件打开动作只走**上下文菜单**（v0.28：双击仅选中；
  v0.28.1：`Enter` 文件行 = 选中 + 弹菜单，`Space` 任意行 = 弹菜单）；`.git`
  不列出，其余隐藏项/依赖目录照常列出。
- **Git 状态配色**（VS Code 习惯）：**修改=金**、**新增/未跟踪=绿**、删除=红、
  重命名=青、冲突=红；**目录按子项继承颜色**（子项里「新增」优先于「修改」）。
  状态每 3s 刷新一次（`git status --porcelain -z -uall`，在 worker 线程里跑），
  同一轮轮询顺带让树跟着磁盘走：**App 之外**新建或删除的文件会在一个周期内出现 /
  消失，且不会展开或收起你已经打开的目录。树跟的是**磁盘**而不只是 git：
  git 看不见的路径（被 gitignore 的目录、嵌套仓内部、不在任何仓库里的目录）
  同样自动更新，唯一排除的只有 `.git`；单个目录超过 2000 项时跳过扫描
  （保持轮询便宜），那里仍用右键「刷新此目录」。
- **上下文菜单**（v0.20；v0.28 文件行改为注入 pane；v0.28.1 键盘可达；
  v0.31 文件行改为「打开」；v0.30 目录行加「刷新此目录」；
  v0.36 文件行加「预览 Markdown」；渲染到 pane 已移除）：
  右键点文件 / 目录行 → 指针处弹出菜单；键盘在文件树里按 `Enter`（文件行，
  先选中并切 Git 历史）或 `Space`（任意行，不改 Git 历史）→ 在光标行右侧弹出
  同一菜单。文件行是「打开（<编辑器>）/ 预览 Markdown（仅 md）/
  预览图片（仅图片，图片行**没有**「打开」）/ 复制相对路径 / 复制绝对路径」，
  目录行是
  「刷新此目录 / 复制相对路径 / 复制绝对路径」；
  相对路径以项目根为基准（根节点复制 `.`）；右键**只高亮该行**，不展开目录、
  不切换 Git 历史；选「打开 / 预览」是显式动作，**会**把 Git 历史切到
  该文件；打开把命令注入**当前 pane**（见下节），预览则在应用内打开全屏
  阅读器，不占用 pane；`Esc` 或点菜单外关闭，关闭后焦点回文件树
  （打开成功时焦点跟到 pane）。
  目录行的「刷新此目录」让外部新增 / 删除的文件或子目录立刻反映到树上。
- **Git 历史**：默认显示仓库级近期提交（**日期 + 标题**，最近在最上；侧栏
  变宽时日期自动从 `MM-DD` 补成 `YYYY-MM-DD`，再宽显示 `YYYY-MM-DD HH:MM`）；
  `Ctrl-a g` 可直接把焦点送进历史面板（↑↓ 移动、`Esc` 回终端；与 `Ctrl-a s`
  进文件树对称）；鼠标在某条提交上**停留 1 秒**会弹出详情卡片（完整标题 +
  完整正文 + 短 sha + 日期时间 + 作者；正文过长可在卡片内滚轮查看）；卡片可把
  鼠标移上去**拖动选中文字**，再按 `Ctrl-a c` 复制；选中某个文件后切为**该文件**的提交历史；区块标题
  显示当前分支（`GIT <branch>`，选中文件时 `GIT <branch> · <相对路径>`）；
  选中目录回到仓库级。非 git 目录显示「非 git 仓库」，git 不可用显示「git 不可用」，
  紧凑档 **Git 整页**没有侧边空间、也没有悬停，底栏 `ENT` 会在列表上方
  显示同一张详情卡片，再点一次或点卡片外关闭。
  空仓库（已 init 无提交）显示「当前分支暂无提交」。
- 侧栏宽度自适应（默认终端宽度的 **15%**，最小 20 列，v0.11.2 起「够放文件树
  即可」）；`Ctrl-a b` 可折叠让 pane 变宽。

## 刷新此目录

「刷新此目录」会展开该目录并重扫（保留其它已展开子树与光标）；目录已被外部
删除时刷新的是**父目录**（让被删节点从树上消失）；项目根被删则全树重载。


## 打开 / 预览

在文件树里通过上下文菜单（鼠标右键 / 键盘 `Enter`·`Space`）选文件行：

- **打开**：把编辑器命令注入当前 pane；
- **预览 Markdown**：在应用内打开全屏只读阅读器（不依赖 pane，公式走
  `termtex` 的 Unicode 二维排版）。

### 预览 Markdown

仅 `.md` / `.markdown` / `.mdown` / `.mkd`。选中「预览 Markdown」后打开一个
全屏只读阅读器：

- GFM 渲染：标题 / 段落 / 列表 / 表格 / 引用 / 代码块；代码块由 Textual 自带
  高亮（不依赖 Pygments）；
- 行内公式 `$...$` / `\(...\)` → `termtex -inline` 的 Unicode 文本；
- 块级公式 `$$...$$` / `\[...\]` → `termtex -width N` 的二维字符排版
  （分式堆叠、矩阵对齐、`cases`）；输出按显示宽度补齐，居中不破坏对齐；
- `$100 and $200` 这类货币不误判；`\(...\)` / `\[...\]` 会先归一化成
  `$...$` / `$$...$$`（跳过代码围栏与行内代码）；
- **没有 `termtex`**：公式显示 LaTeX 原文，绝不空白 / 崩溃；也可用
  `MYRTUI_MATH_BACKEND=plain` 强制原文；
- 链接点击不导航；文件变化不自动刷新——改动会在 footer 提示一句，按 `r` 重载；
- 上限 **512 KiB**：超过时在打开前提示「Markdown 过大」，请改用「打开」；
  二进制 / 缺失 / 空文件会在预览屏内显示可读原因。

| 键 | 作用 |
|---|---|
| `Esc` / `q` | 关闭预览，焦点回文件树 |
| `r` | 重新读盘 + 重新渲染（尽量保留滚动位置） |
| `v` | **LaTeX 排版**：整篇交给浏览器排版（KaTeX PDF 分页位图，见下） |
| `p` | **导出 A4 PDF**：整篇排成白底黑字的矢量 PDF，落在源文件同目录（见下） |
| `↑` / `↓` / `PgUp` / `PgDn` / `Home` / `End` | 滚动 |
| 鼠标滚轮 | 滚动 |

预览**不占用 pane**，所以当前 pane 在跑 opencode / vim / ssh，或已经退出时，
仍然可以看 Markdown。

预览（Markdown / LaTeX 排版 / 图片）打开期间，**背后的 pane 不再重绘**：主屏的
脏区会被 Textual 抄到顶层预览屏，重绘 pane 等于把整屏位图重发一遍——LaTeX 排版
视图的闪烁正是这么来的。打开预览的那一屏同样被冻结。关闭预览会立刻重绘 pane、
标签与侧栏；tmux 本身不受影响。

#### LaTeX 排版视图（v0.37.2，`v`）

按 `v`：整篇 md → 自包含 HTML（KaTeX `auto-render`）→ Chrome `--print-to-pdf`
（页面尺寸 = 终端可视区）→ `pdftocairo -png` → 每页一张位图，**按整页翻**。
默认是**米黄纸**（正文 `#3D3427` / 页面 `#E6DABE`，对比度 ~8.8:1），标题栏
与外围控件用同一套颜色；觉得太黄 / 太暗可换预设（见下）。

| `MYRTUI_PAGE_THEME` | 页面底 | 正文 | 说明 |
|---|---|---|---|
| `sepia`（默认） | `#E6DABE` | `#3D3427` | 米黄纸；靠色度而不是亮度拉开观感 |
| `paper` | `#F7F4EC` | `#26241F` | v0.4.1 的纸白（14.1:1），偏亮 |
| `gray` | `#F4F4F2` | `#222222` | 中性灰白，完全不偏黄 |
| `white` | `#FFFFFF` | `#111111` | v0.4.0 的纯白（18.9:1，最锐利） |
| `dim` | `#EDEDEA` | `#2E2E2E` | 夜间低亮度，接近电子墨水 |
| `dark` | `#1E1E1E` | `#E0E0E0` | 跟随深色终端主题 |

非法值告警后按 `sepia` 处理。换主题后首次打开会重排（缓存键含配色）。

> 为什么默认要选"偏黄"的？`paper` / `gray` / `dim` 的平均通道只差 2–6/255
> （242 / 244 / 236），大面积纯色下人眼分辨不出，看着都是白；`sepia` 的平均
> 通道 213，并且**色度**差别明显，这才真正压掉眩光。

- **为什么**：行内位图在 Sixel 终端上做不到（图像是光标处的矩形），整篇交给
  浏览器后，正文 / 行内公式 / 块级公式是**同一套排版**，没有「文本 mono +
  公式 CM」的混搭；表格、代码、标题层级也一起排好；
- **页面 1:1**：页宽 = 列 × cell 宽、页高 = 行 × cell 高，`-r 96` 出来的 PNG
  与页面像素相同，不重采样；一页 = 一屏，**翻页整屏切换**；
- **为什么不让逐行滚动（v0.6.1）**：页是整屏位图，`textual-image` 的重编码
  缓存键含可见 `crop`——滚一行就要把上下两张半页重新编码（2520×1260 实测
  ≈380 ms/张，且在事件循环上），键盘滚动默认还带动画（一次 PgDn 十余帧）。
  吸附到页边界后屏幕上下永远只有一张完整页，缓存恒定命中；代价是没有半页
  过渡，换来的是一次按键 = 一次整屏写；
- **代价**（这就是它是「逃生口」而不是默认阅读器的原因）：这一屏**不能选中
  文本、不能搜索/复制**；改窗口尺寸会重新排版（约 1–2 s）；
- **前提**：Chrome/Chromium + `pdftocairo`，且终端支持 Sixel/TGP；缺任一项
  会在预览里提示，不会白屏；
- 缓存：`<cache>/myrtui/pages/<sha1>/p-001.png…`（键含正文、页面尺寸、主题色、
  资产版本），同文档同尺寸再打开 0 子进程。

| 键（LaTeX 排版视图内） | 作用 |
|---|---|
| `Esc` / `q` / `v` | 回文本预览 |
| `↑` / `↓` / `PgUp` / `PgDn` / `Home` / `End` | **翻页**（`↑`/`↓` 也是一整页；header 显示 `2/9 页`） |
| 鼠标滚轮 | 翻页（带 ~120 ms 冷却，触控板惯性别连翻） |
| `r` | 丢掉缓存重渲染 |

#### 导出 A4 PDF（`p`）

按 `p`：整篇走**同一条** md → HTML 管线（公式判定、`\label`→`\tag`、图片内联
与预览完全一致），但打印到 **A4 纸**、**白底黑字**——拿到的是矢量 PDF（字可选中、
打印不虚），不是位图。适合发出去或打印。

- **落点**：源文件**同目录**的 `<stem>.pdf`（`README.md` → `README.pdf`）；
  先写临时文件，Chrome 成功后才 `os.replace` 覆盖，所以导出失败**不会**留下
  半截 PDF，也不会毁掉上一份好的；
- **不联网**：本地图片内联进 HTML，`http(s)` 图片直接丢掉；KaTeX 资产是仓库
  自带的；
- **长代码行换行**而不是被裁右半边；代码块 / 表头底色会真的打印出来
  （`print-color-adjust: exact`）；表格跨页时表头重复；
- **没有页码**：Chrome 的 `@page` 不支持页码计数器，做页码要引入 PDF 库，本仓
  不为这个加运行时依赖；
- **前提**：Chrome/Chromium（与 `v` 同一个探测，`MYRTUI_CHROME` 可指定）；缺了
  只在 footer 提示，不影响预览本身；
- 上限沿用预览的 **512 KiB**，公式再多也在 `--virtual-time-budget` 的 3 s 内排完。

### 预览图片

文件树图片行（`.png` / `.jpg` / `.jpeg` / `.gif` / `.webp` / `.bmp` /
`.tif` / `.tiff` / `.ico` / `.avif`）的菜单只有「预览图片 / 复制路径」——
图片是二进制，**不再提供「打开（编辑器）」**（`micro` 打开只会是乱码；
没有编辑器时的 `ov / less / cat` 兜底也一并对图片禁用）。

- 按可视区等比适配、居中显示；不放大、不修改原文件；
- **同目录切换**：`→` / `n` 下一张，`←` / `p` 上一张；到目录头尾**循环**。
  顺序与文件树一致（按文件名忽略大小写），只在本目录内、不进子目录；
- header：`cat.png · 3/12 · 1200×800 · PNG · 1.2 MiB`（只有一张时省略 `3/12`）；
- `r` 重扫目录 + 重载当前（中途拖进新图也能纳入循环）；`Esc` / `q` 关闭；
- 关闭后焦点回文件树，**光标落在最后看的那张**，接着右键 / 继续看都顺手；
- 图片解码、缩放、透明预合成都在 worker 线程；连按方向键只保留最后一次；
- 透明图按当前主题背景色预合成；没有 Sixel / TGP 时仍显示元信息与提示；
  目标图坏掉 / 被删只影响那一张，仍然可以继续翻；
- 上限沿用 `imageprep`：≤10 MB、≤4000×4000；动图只显示首帧，SVG 暂不支持。

#### 图片位图（v0.37；v0.3.1 起依赖自动安装）

位图依赖 `textual-image` + `Pillow` 从 v0.3.1 起是**条件硬依赖**：Python ≥3.12
（`pipx` / `uv` / `venv` 任意通道）安装 / 升级 myrtui 时自动带上；3.10/3.11
装不了（`textual-image` 的要求），图片回落 `🖼 alt`。终端支持 **Sixel**
（Tabby / WezTerm / mlterm / foot …）或 **Kitty TGP**（kitty / Ghostty）时：

- **Markdown 里的图片**：只处理**独立成段**的 `![alt](path)`；路径相对
  Markdown 文件，允许绝对路径与 `file://`，**拒绝远程 URL**（不联网）；
  PNG / JPEG / GIF（首帧）/ WebP / BMP；单图 ≤10 MB、像素 ≤4000×4000、
  显示高度 ≤20 行；
- **图片预览**：文件树里选「预览图片」，按可视区等比适配、居中；同目录
  `←` / `→`（或 `n` / `p`）循环切换，`Esc` / `q` 关闭、`r` 重扫 + 重载；
  header 显示 `序号/总数`、尺寸、格式、大小。没有位图协议时仍显示元信息与
  可执行提示（实测过的终端见 [README](../README.zh-CN.md) 的「终端支持」）；
- **公式不再出位图**：文本预览里的行内 / 块级公式统一由 `termtex` 排版；
  高质量整篇排版用 `v`（KaTeX，纸色底）；
- **不做**：句子中间的行内图片位图（Sixel 图像是光标处矩形，塞不进一行）、
  pane 内位图、目录画廊、动图播放（只显示首帧）；
- **透明背景**：终端不做 alpha 合成，myrtui 在 worker 里把图片预合成到
  块背景色——透明区域是与背景一致的实色，不是黑块；
- 没有 `textual-image` / 终端不支持 / 非 tty → 图片显示 `🖼 alt`，不报错、
  不打印乱码。

环境变量：`MYRTUI_GRAPHICS` / `MYRTUI_CELL_PX`（见「环境变量」表）。

> **外层 tmux**：tmux 3.8 支持 Sixel，但默认不认；需要
> `set -ga terminal-features ',*:sixel'`，必要时再加
> `set -g allow-passthrough on`。不配也能用，只是位图不出现。

> v0.36 只做 Unicode 公式与 GFM 文本；v0.37 在此之上加图片位图；v0.4 起
> 文本预览的公式**只走 termtex**，整篇 LaTeX 排版用 `v`。Mermaid / 搜索 /
> 编辑 / 源码视图仍不做。

### 打开

按下面的顺序解析编辑器，菜单会显示实际选中的工具名（如「打开（micro）」）：

1. `$MYRTUI_EDITOR`（可带参数，如 `code --wait`）；
2. `$EDITOR`；
3. `$VISUAL`；
4. 自动探测：`micro` → `vim` → `vi`。

helix / nvim 不再自动探测；想用它们请显式指定，例如
`export MYRTUI_EDITOR=nvim` 或 `export EDITOR=hx`。

打开后可以立刻阅读、编辑、搜索；退出编辑器（micro `Ctrl-Q` / vim `:q`）即回
shell。`$MYRTUI_EDITOR` / `$EDITOR` 里写裸命令名也可以，myrtui 会尽量解析成
绝对路径，避免 pane 内 PATH 差异。

```sh
brew install micro    # 推荐：无模式、Ctrl-S 保存 / Ctrl-Q 退出 / Ctrl-F 搜索
```

如果这些编辑器都没装，菜单会退化成「查看（ov）」或「查看（less）」：用分页器
只读打开，同样可滚轮 / 翻页 / 搜索。

**复制**：v0.35 起直接**拖选即复制**（myrtui 的选区 + OSC 52 / 系统剪贴板），
不再依赖 `ov` 自己的复制通道。若想恢复 `ov` 自己的拖选 / 双击选词行为，设
`MYRTUI_DRAG_SELECT=app`；此时可给 `ov` 加一条配置让它走 OSC 52
（`~/.config/ov/config.yaml`）：

```yaml
ClipboardMethod: "OSC52"
```

myrtui 会为会话设置 `set-clipboard on`（前端不 attach tmux client，`external`
会把 pane 内的 OSC 52 丢掉），这样程序自己复制的文本仍能被 tmux 收进 buffer。
macOS Terminal.app 不支持 OSC 52，可把上面改成 `ClipboardMethod: system`。

**注意**：编辑器里看到的是 Markdown / LaTeX 源码，不是渲染结果；「预览」是
应用内阅读器。终端编辑器没有可靠的实时双栏预览方案（nvim 的预览插件还要
node），本仓不引入。

**前置条件**（仅「打开」）：当前 pane 必须停在 shell 提示符。
若正在运行 micro / vim / opencode / ov / less 等前台程序、处于备用屏或 pane
已退出，动作会提示并拒绝，避免把命令注射进别的程序。发送前会先发 `C-u`
清空当前输入行（会丢掉已输入未执行的内容）。「预览 Markdown」不经过这条
限制。

可选安装（不装也能打开与查看，只是没有公式 / 排版 / 图片增强）：

```sh
brew install micro                                      # 推荐：打开用的无模式编辑器
brew install ov                                         # 推荐：查看器（滚轮 + 选区 + 搜索）
go install github.com/doug/termtex/cmd/termtex@latest   # 可选，不装公式显示原文
brew install poppler                                    # 可选：v LaTeX 排版（pdftocairo / pdftoppm）
# v 排版用 Chrome/Chromium（macOS 一般已装）+ poppler；缺任一项会提示，不会白屏
```

位图依赖（v0.3.1 起随 myrtui 自动安装；`textual-image` 要求 Python ≥3.12）。
只有**升级前的老环境**或手工 `pip install myrtui` 才需要补装：

```sh
pipx inject myrtui textual-image pillow                 # pipx 安装的 myrtui
# 或：pip install -e ".[graphics]"

# 想用 helix / nvim：显式指定，不参与自动探测
export MYRTUI_EDITOR=nvim    # 或 export EDITOR=hx
```

安装 / 升级脚本会在交互式运行时逐项询问是否安装这些推荐组件；全部跳过也不
影响打开与查看（会自动回退到 `vim` / `less` / 原文）。

**限制**：

- 打开复用**当前 pane**：想同时看两个文件，先 `Ctrl-a t` 开新 pane，
  或退出当前编辑器再开下一个；预览不受此限（它是应用内全屏层）；
- `ov` 抢鼠标（`mouse` 打开）时，拖动默认归它自己（0.7.0 起 `MYRTUI_DRAG_SELECT=smart`）
  → 用 `ov` 的拖选 / 双击选词开箱即用；想用 myrtui 的选字按住 **Shift** 再拖
  （`always` 则相反：拖动永远归 myrtui）；`less` 不抢鼠标，但滚轮不驱动它，
  请用键盘滚动；
- 二进制文件（前 8 KiB 含 NUL）不打开；路径含换行 / 控制字符时拒绝执行；
- 没有 `termtex` 时公式是 LaTeX 原文；
- v0.37 预览的位图依赖（`textual-image` + `Pillow`）在 Python ≥3.12 时会自动
  安装；Python 3.10/3.11、终端不支持 Sixel/TGP、或没有 TeX/poppler 时，公式
  回落 termtex、图片显示 `🖼 alt`；行内公式永远是 Unicode；
  pane 内永远没有位图；Mermaid / 搜索 / 编辑仍不做。


## 可拖拽分隔线

界面里的分隔线都是 1 cell 厚，**按住拖动即可调整相邻两块的比例**：

| 分隔线 | 位置 | 调整什么 |
|---|---|---|
| 侧栏 ↔ pane 区 | 侧栏右边缘（竖线） | 侧栏宽度 |
| 文件树 ↔ Git 面板 | 侧栏内（横线） | 文件树高度 |
| 左列 ↔ 右列 | pane 区中间（竖线） | 左右两列宽度 |
| 列内上下 | 每列中间（横线） | 列内上下两块高度（两列共用一份比例 → 2×2 始终十字对齐） |

- **手不用准**：分隔线只有 1 cell 宽，但**左右 1 格内按下都算抓住它**
  （热区共 3 格，v0.20 从 ±2 收回；树 / Git 面板内按下同样有效）；
  指针靠近时那条线会提亮，告诉你「这儿能拖」。注意**侧栏 ↔ pane 区那条
  竖线只能从 pane 一侧拖**——侧栏的 padding 让文件树最后一格距线 2 格，
  树侧不在 ±1 热区内。
- **拖拽只用左键**（0.8.0 收回单键）：双击复位，中键不参与。v0.11.5 曾恢复
  「左 + 右」双键——当时把 UU 远程里拖不动归因成「左键事件不可达」；0.8.0 查明
  真因是**外层 tmux 的 `MouseDrag1Pane` 被改成无条件 copy-mode**，把按住期间的
  motion 全吃掉了（见「在外层 tmux 里」一节）。绑定恢复后左键即可，而右键留着
  拖拽会抢走 pane 上的右键（0.8.0 起右键是粘贴），故收回。
  拖拽在 **App 层**处理（v0.11.4：事件分发前截获，不依赖 widget 层转发与鼠标
  捕获——SSH 链路等环境下也稳定）。疑难排查：`MYRTUI_MOUSE_DEBUG=1` 启动时把每个
  鼠标事件写到 `<tmp>/myrtui-mouse-debug-<pid>.log`。
- **焦点视觉（v0.11.2）**：焦点 pane 的**边框**加粗变绿（`heavy` + 绿色，
  失焦恢复细灰）——边框恒在，切焦点不改变布局尺寸；侧栏聚焦则是**按半区
  变底色**（v0.11.3：焦点在文件树 → 只提亮上半区；焦点在 Git 面板 →
  只提亮下半区），没有任何绿线。
- **双击分隔线** → 该维度比例复位默认。
- **比例会记住**：拖完立刻落盘（`<user_data_dir>/myrtui/<hash>.layout.json`），
  **下次在同一项目 attach 时按上次的位置恢复**（文件缺失或损坏则回落默认值）。
  落盘格式带版本号：v0.12 布局改列优先后格式升到 **version 2**，**旧版本文件
  一律作废**（回落默认比例），不套用几何含义已变的旧数字。
- 比例用 `fr` 表达 → 终端窗口缩放时比例保持不变。

## 已知限制与提示

- **TERM 覆盖风险**：pane 的真彩能力来自 `set -g default-terminal tmux-256color`。
  如果你的 shell 启动文件（`.zshrc` / `.bashrc` 等）重设了 `TERM`，会覆盖该值——
  这是 tmux 官方文档明确警示的行为，myrtui 不做硬规避。
- **键入回显存在轮询延迟**（空闲 ~150ms；输入后自动提频到 30ms 数个 tick）——
  这是**回显轮询**的固有延迟，与卡顿是两回事：v0.17 起后端调用已移出事件循环
  （卡顿/阻塞见下条），但回显仍等下一次 poll 才刷新，根治归后续 B2。
- **多实例时可给后台实例降频**：`MYRTUI_IDLE_INTERVAL=0.5 myrtui`（空闲抓屏
  6.7 → 2 次/s，见「环境变量」小节）；排障/性能遥测用 `MYRTUI_LOOP_DEBUG=1`。
- pane 内程序未请求鼠标时，其鼠标事件不被转发（点击只聚焦、滚轮翻 scrollback），
  这是与真实终端一致的行为；motion 转发有 30ms 合并节流。
- **外层终端的原生选区用不了**：Textual 启动时会打开 mouse reporting
  （`ESC[?1003h`/`ESC[?1006h`），终端进入该模式后就不再自己做拖拽选区，
  鼠标事件全交给 myrtui。部分终端按住 **Shift** 拖拽能临时绕过（取决于终端）；
  myrtui 内的选字请直接**拖拽**（松手自动复制）。
- **点击延迟 50–150ms**：当拖动归 myrtui（程序没要鼠标，或 `MYRTUI_DRAG_SELECT=always`）
  时，左键 press 不立即注入，只有松手且判定为点击时才合成 `press + release`；
  延迟 = 按住时长。请求了鼠标的程序（vim / opencode）按下即收到 press，没有这个延迟。
- **本地选字要 Shift**（0.7.0 起的默认 `smart`）：vim `mouse=a`、`ov`、opencode 这些
  会自己处理拖动；想用 myrtui 的选字 + 自动复制，按住 **Shift** 再拖。想让拖动
  永远归本地就设 `MYRTUI_DRAG_SELECT=always`。
- **选区按字符偏移保存**：拖拽期间会**冻结**该 pane 的画面（选区不随 capture
  刷新漂移），松手复制后 600ms 解冻并恢复实时内容。
- **软折行段落会多出换行**：pane 的视觉软折行在 capture 里是独立行，跨行复制
  可能在视觉换行处多出 `\n`（v1 已知限制，后续用 `capture-pane -J` 做逻辑行合并）。
- **pane 左缘 1 格落在分隔线热区里**（±1 cell 的拖拽热区）：在那一带按下是
  拖分隔线，不是起选区。
- **没有「一键复制整屏」**：`Ctrl-a c` 只复制**选区**（v0.13 决策：无选区不复制）。
  要整段历史请走 tmux 后端：
  `tmux -L myrtui-<hash16> capture-pane -p -t myrtui-<hash16> -S - | pbcopy`
  （`<hash16>` = `realpath(项目根)` 的 sha256 前 16 位）。
- **粘贴会带上剪贴板里的换行**：剪贴板内容**原样**送进终端（与终端原生粘贴一致）
  → 复制的是带换行的命令，粘贴即回车。
- **内部缓冲不落盘**：`Ctrl-a c` 复制的选区只存在内存里，detach/attach 后消失
  （沿用 myrtui「无磁盘状态」原则）；且本地 `Ctrl-a v` 已改为**系统剪贴板优先**，
  内部缓冲只在读不到系统剪贴板时兜底。
- **双击不选中**：Textual 默认的「双击全选」被显式禁用（v0.13 决策），双击只聚焦
  pane；选字请拖拽。
- **`Ctrl-a c` / `Ctrl-a v` / `Ctrl-a g` 抢走了三个字母**：原行为是
  「Ctrl-a 后按 c / v / g → 把该字母透传给内层 shell」，现在分别是复制、
  粘贴、聚焦 Git 历史（与其它前缀键同款取舍）。`Ctrl-a o` 不再占用，
  会像其它未定义前缀键一样把 `o` 透传给 shell。
- **vim 默认 `mouse=`（空）不请求鼠标**——此时点击/滚轮在纯 tmux 下同样无效，
  不是 myrtui 的问题；在 vim 内 `:set mouse=a`（或写进 vimrc）后即可用。
- **焦点在侧栏时键与粘贴不进 shell**（焦点归侧栏，`Esc` 回终端）——侧栏宽度变宽后
  窄终端（如 80 列）里 pane 会变窄，可用 `Ctrl-a b` 折叠侧栏。
- 树内**不做** commit 详情/diff（右键只做打开 / 预览 / 复制路径，不显示
  diff、不自动刷新）。
- **打开的限制**（v0.31）：复用当前 pane，pane 正在运行前台程序或已退出
  时拒绝；只读查看依赖可选的 `ov`（缺失时退 `less`）；`ov` 抢鼠标期间左键
  拖拽仍归 myrtui（想用 ov 自己的选字设 `MYRTUI_DRAG_SELECT=app`）；二进制
  文件（前 8 KiB 含 NUL）不打开；发送前会发 `C-u` 清空当前输入行（会丢掉已
  输入未执行的内容）。**没有 `termtex` 时公式显示 LaTeX 原文**（可选外部命令；
  Python 依赖只有 Textual / platformdirs / mdit-py-plugins 与位图依赖，后者在
  Python ≥3.12 时自动安装）。
- 文件级 Git 历史用 `git log --follow` 跨重命名追踪；git 会把**仓库的根提交**也
  列进文件历史（git 自身的行为：根提交相对空树「新增」了所有文件）。
- **文件树配色只覆盖「文件在磁盘上」的变更**——被删掉的文件不在磁盘上，
  树里根本不显示，故红色的「删除」实际很少出现。
- **上传文件：本工具不提供**。远端与本地之间请用 sftp / scp 客户端（FileZilla、
  MobaXterm 自带 sftp 面板），远控场景用远控软件自带的文件传输。向终端窗口拖入
  文件时，终端只会把**路径文本**当成一次粘贴：焦点在侧栏（文件树 / Git 历史）时
  被丢弃，焦点在 pane 时就和普通粘贴一样落进 shell——**这不是上传入口**。想避免
  误贴，先把焦点放到侧栏（点一下目录）再拖。旧版本留下的 `<data>/endpoint.json` /
  `*.transfer.json` / `uplink/` 可以直接删除。

## 开发与贡献

```sh
pip install -e ".[dev]"
python -m pytest -n 4     # 跑测试（后端测试使用隔离 tmux 实例，自动清理；本机约 1 分钟）
```

发版：bump `pyproject.toml` 的 `version` 与 `myrtui/__init__.py` 的 `__version__`
（`myrtui --version` 读的是 `importlib.metadata`，装出来的 wheel 才体现新版本）→
推 main → 打 tag（`git tag v<版本> && git push origin v<版本>`）→
`.github/workflows/release.yml` 自动跑 Linux 验证、构建 wheel + sdist、发布到
PyPI 与 GitHub Releases（macOS 面由 Gitea 自建 runner 每次推送覆盖，GitHub 上的
macOS 任务改为手动触发，排队不再阻塞发布）。

贡献方式、代码风格与**注释语言政策**见 [CONTRIBUTING.md](../CONTRIBUTING.md)；
架构说明见 [ARCHITECTURE.md](ARCHITECTURE.md)；英文手册见 [USAGE.md](USAGE.md)；
首页见 [README.md](../README.md)。内部的设计 / 验收记录保存在 `docs-internal/`
（嵌套的独立 git 仓），不随公开仓发布。

