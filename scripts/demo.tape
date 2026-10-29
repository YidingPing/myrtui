# 用 vhs 录制 README 首屏的 demo.gif：`vhs scripts/demo.tape`
#
# 前置：vhs（brew install vhs）、tmux ≥ 3.1、myrtui 已安装；
# 另需一个演示用项目（下面的 DEMO 变量），建议包含：
#   - 几个源码文件 + 一个 README.md（带公式）+ 两张图片
#   - 已初始化的 git 仓库（>= 3 个提交，便于展示历史面板）
#
# 录制要点：只演示 3 件事——分 pane、拖分隔线、预览 Markdown。
# 时间轴太长的 GIF 没人看完；目标 8–12 秒。

Output docs/demo.gif

Set Shell "zsh"
Set FontSize 15
Set Width 1100
Set Height 700
Set Padding 8
Set Theme "Catppuccin Mocha"
Set TypingSpeed 45ms

# 项目目录（录之前改成本机的演示项目）
Hide
Type "cd ${DEMO:-$HOME/src/demo-project}"
Enter
Show

Type "myrtui"
Enter
Sleep 2s

# 1) 开第二个 pane（上下分），再开第三、第四个（2×2）
Ctrl+A
Type "t"
Sleep 700ms
Ctrl+A
Type "t"
Sleep 700ms
Ctrl+A
Type "t"
Sleep 900ms

# 2) 拖动左列分隔线，展示比例联动
MouseDrag 560 220 560 320
Sleep 600ms

# 3) 文件树里打开 Markdown 预览（键盘路径：聚焦树 → Space → 选 Preview）
Ctrl+A
Type "s"
Sleep 500ms
Type "j"
Sleep 200ms
Type "j"
Sleep 200ms
Type " "
Sleep 500ms
Type "j"
Sleep 200ms
Type "j"
Sleep 200ms
Type "j"
Sleep 200ms
Enter
Sleep 2500ms

# 4) 关闭预览，回到 pane
Type "q"
Sleep 1200ms
