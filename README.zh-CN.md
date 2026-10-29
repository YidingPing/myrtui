# myrtui

[English](README.md) | **简体中文**

[![PyPI](https://img.shields.io/pypi/v/myrtui)](https://pypi.org/project/myrtui/)
[![Python](https://img.shields.io/pypi/pyversions/myrtui)](https://pypi.org/project/myrtui/)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue)](LICENSE)
![Platforms](https://img.shields.io/badge/platform-macOS%20%7C%20Linux-lightgrey)

**面向单个项目的终端工作台**：左侧文件树 + Git 历史，右侧最多 4 个真实 shell
pane。一个项目一个会话，退出再进来还是原来的布局。

> 它不是 tmux 的替代品——底层就是 tmux（每个项目一个独立 socket）。
> myrtui 补的是 tmux 故意不做的那些事。

<!-- 录制方式：vhs scripts/demo.tape -> docs/demo.gif，录完取消下面这行注释。 -->
<!-- ![demo](docs/demo.gif) -->

## 为什么不用 tmux 自己分屏

| | tmux / zellij | tmuxinator / sesh | myrtui |
|---|---|---|---|
| 一项目一会话，自动命名与隔离 | 手工 | 要写配置文件 | `cd 项目 && myrtui` |
| 文件树 + Git 历史与 shell 同屏 | — | — | 有 |
| 每个项目记住分隔线比例 | — | 每次改配置 | 有 |
| 打开 / 预览 Markdown、图片、LaTeX | — | — | 有 |
| detach / attach，shell 继续存活 | 有 | 有 | 有 |

## 特性

- **一个目录 = 一个实例**：身份由真实路径派生，两个项目互不干扰；再次运行
  同一目录即 attach 回原会话，pane、布局、滚动历史都在。
- **最多 4 个真实 tmux pane**，鼠标可用：点击聚焦、滚轮翻历史、拖动分隔线
  调比例（比例按项目落盘记住）。
- **文件树带 Git 状态配色**（VS Code 习惯），懒加载；右键菜单可打开 / 预览 /
  复制路径。
- **Git 历史面板**：仓库级或按文件，鼠标停留 1 秒看提交详情。
- **Markdown 预览**：GFM、`$公式$` 走 `termtex`，可选 `v` 用 KaTeX 分页排版
  （米黄纸底）。
- **图片预览**：终端支持 Sixel / Kitty 时按可视区等比显示。
- **选区与剪贴板**在 TUI 里真的能用：拖拽选字，松手即复制（OSC 52 + 系统命令）。

## 安装

需要 `tmux >= 3.1` 与 Python `>= 3.10`（macOS / Linux；不支持 Windows——
`fcntl` 依赖，且 tmux 无原生 Windows 版）。

```sh
brew install tmux              # Linux: sudo apt install tmux
pipx install myrtui
myrtui --version
```

也可以交给脚本（额外检查依赖、逐项询问可选组件）：

```sh
curl -fsSL https://raw.githubusercontent.com/YidingPing/myrtui/main/scripts/install.sh | sh
```

升级用 `pipx upgrade myrtui`。Python ≥ 3.12 会自动带上位图依赖（`textual-image`
+ `Pillow`）；3.10/3.11 上预览回落为文本，其余功能不受影响。

## 使用

```sh
cd some-project
myrtui                 # 首次运行创建会话，之后都是 attach
```

- `myrtui ls` —— 列出存活实例（`--all` 含 dead 残留，`--json` 机器可读）；
- `myrtui attach [目标]` —— 目标 = 序号 / hash 前缀 / 项目路径；
- `myrtui stop [目标]` —— 协作式退场（不 kill pane）；
- `myrtui --root /path/to/other` —— 不 `cd` 也能开另一个项目的实例；
  `--shell` / `--scrollback` 设置 pane 的 shell 与历史深度。

## 键位（Ctrl-a 前缀）

| 键 | 作用 |
|---|---|
| `Ctrl-a d` | detach：前端退出，所有 shell 继续运行 |
| `Ctrl-a k` | 结束会话（先确认） |
| `Ctrl-a t` / `w` | 新建 / 关闭 pane（最多 4 个） |
| `Ctrl-a 1..4` | 切换焦点 pane |
| `Ctrl-a r` | 重开已退出的 pane |
| `Ctrl-a b` | 折叠 / 展开侧栏 |
| `Ctrl-a s` / `g` | 聚焦文件树 / Git 历史 |
| `Ctrl-a c` / `v` | 复制选区 / 粘贴 |

其余按键直接透传给内层 shell。`Esc` 从侧栏回到 pane；`Ctrl-a ?`（或鼠标悬停
顶栏的 `?`）显示完整快捷键浮层。全表见[使用手册](docs/USAGE.zh-CN.md)。

## 配置

<details>
<summary>环境变量</summary>

| 变量 | 作用 |
|---|---|
| `MYRTUI_EDITOR` | 「打开」用的编辑器（否则 `$EDITOR` / `$VISUAL` / `micro` → `vim` → `vi`） |
| `MYRTUI_IDLE_INTERVAL` | 空闲抓屏间隔，秒（默认 `0.15`，夹取 `0.05–5.0`）；后台实例可调大 |
| `MYRTUI_COPY_ON_SELECT` | 拖拽松手自动复制（默认 `1`；`0` 时用 `Ctrl-a c` 手动复制） |
| `MYRTUI_DRAG_SELECT` | `always`（默认）= 拖拽永远选字；`app` = 程序请求鼠标时把拖拽还给它 |
| `MYRTUI_GRAPHICS` | 位图协议：`auto`（默认）/ `sixel` / `tgp` / `unicode` / `off` |
| `MYRTUI_PAGE_THEME` | `v` 排版页面配色：`sepia`（默认）/ `paper` / `gray` / `white` / `dim` / `dark` |
| `MYRTUI_MATH_BACKEND` | Markdown 公式后端：`auto`（默认）/ `plain` / `termtex` |
| `MYRTUI_CELL_PX` | 终端不回报单元格像素尺寸时手动指定（`10x20`） |
| `MYRTUI_CHROME` | `v` 排版用的 Chrome/Chromium |
| `MYRTUI_PDFTOCAIRO` | PDF → PNG 转换器（优先 `pdftocairo`，其次 `pdftoppm`） |
| `MYRTUI_LOOP_DEBUG` / `MYRTUI_MOUSE_DEBUG` | 排障：事件循环延迟 / 鼠标事件写日志 |

</details>

## 已知限制

- **仅 macOS / Linux**：Windows 没有原生 tmux，`cli.py` 也依赖 `fcntl`。
- **pane 内点击延迟 50–150 ms**：拖拽永远归选字，所以点击在松手时才注入
  （`MYRTUI_DRAG_SELECT=app` 可把拖拽还给程序）。
- **pane 内没有位图**：图片只在预览层显示。
- **软折行复制会多出换行**：抓屏是按行来的。
- **空闲回显约 150 ms**：抓屏是轮询的，输入后会临时提频到 ~30 ms。

## 文档与贡献

- [使用手册（中文）](docs/USAGE.zh-CN.md) —— 完整键位、预览、分隔线、实例管理、
  全部环境变量。
- [docs/USAGE.md](docs/USAGE.md) —— 同一份手册的英文版。
- [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) —— 模块划分与设计取舍。
- [CONTRIBUTING.md](CONTRIBUTING.md) —— 如何跑测试，以及注释语言政策。

按项目约定，**代码注释与 docstring 用中文**；面向用户的文本（README、界面、
`docs/`）提供英文。Issue 与 PR 中英文都欢迎。

## 许可证

MIT —— 见 [LICENSE](LICENSE)。
