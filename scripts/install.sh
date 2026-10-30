#!/usr/bin/env sh
# myrtui one-line installer (public repo, PyPI channel)
#
#   curl -fsSL https://raw.githubusercontent.com/<owner>/myrtui/main/scripts/install.sh | sh
#
# 首选安装方式其实是 `pipx install myrtui`；本脚本只是它的**带依赖引导**版本：
# 在装包之前先确保运行时依赖（tmux >= 3.1）可用，并在装完之后逐项询问推荐组件。
#
# 做这些事：
#   1) 平台校验（macOS / Linux，x86_64 / arm64）；
#   2) tmux >= 3.1（缺则**询问后**尝试 brew / apt / dnf / pacman 自动装；
#      拒绝 / 无权限 / 失败时打印指引**继续执行**——tmux 只影响运行）；
#   3) 从 **PyPI** 安装 `myrtui`，按既有通道原地升级（uv tool → pipx → venv 兜底），
#      全部落在用户目录，不污染系统 Python；最后用 `myrtui --version` 收口；
#   4) 推荐组件 micro / ov / termtex（交互式逐项询问；拒绝 / 失败时
#      回退并打印手动命令，不影响 myrtui 本体）。
#
# v0.4.4 起安装源改为 PyPI：旧版的 Gitea Release API 查询、wheel 资产下载、
# 访问令牌 / git 凭据复用、版本比对全部删除。副作用（都是想要的）：
#   - 不再需要任何仓库地址或访问令牌类环境变量；
#   - 「已是最新」由 pipx / uv 自己判断，脚本不再提前退出；
#   - 装/升级走同一条命令：重跑本脚本即可。
#
# 可覆盖的环境变量：
#   MYRTUI_VERSION   固定版本（如 0.5.0；默认装 PyPI 上的最新版）
#   MYRTUI_PREFIX    venv 兜底安装位置（默认 ~/.local/opt/myrtui）
#   MYRTUI_DEPS      ask（默认）/ yes / no：是否尝试安装缺失的依赖
#
# 退出码：0 成功；1 环境/安装失败；用法类错误直接退出 1。
set -eu

MYRTUI_VERSION="${MYRTUI_VERSION:-}"
MYRTUI_PREFIX="${MYRTUI_PREFIX:-$HOME/.local/opt/myrtui}"
MYRTUI_DEPS="${MYRTUI_DEPS:-ask}"
BIN_DIR="$HOME/.local/bin"

# 安装规格：`myrtui` 或 `myrtui==<版本>`（固定版本调试用）
SPEC="myrtui"
if [ -n "$MYRTUI_VERSION" ]; then
    SPEC="myrtui==${MYRTUI_VERSION}"
fi

log() { printf 'myrtui-install: %s\n' "$*"; }
die() { printf 'myrtui-install: %s\n' "$*" >&2; exit 1; }
have() { command -v "$1" >/dev/null 2>&1; }
have_tool() { have "$1" || [ -x "$BIN_DIR/$1" ]; }
dep_status() {
    if have_tool "$1"; then
        printf 'yes'
    else
        printf 'no'
    fi
}

case "$MYRTUI_DEPS" in
    ask|yes|no) ;;
    1|true) MYRTUI_DEPS="yes" ;;
    0|false) MYRTUI_DEPS="no" ;;
    *)
        log "unknown MYRTUI_DEPS=${MYRTUI_DEPS}, falling back to ask"
        MYRTUI_DEPS="ask"
        ;;
esac

# 从 /dev/tty 读确认：`curl … | sh` 的 stdin 是脚本本身，不能用来交互。
# 非交互环境按 no 处理，避免管道安装时挂住等输入。
ask_yes() {
    _question=$1
    case "$MYRTUI_DEPS" in
        yes|1|true) return 0 ;;
        no|0|false) return 1 ;;
    esac
    if [ ! -r /dev/tty ]; then
        log "non-interactive, skipping: ${_question} (use MYRTUI_DEPS=yes to auto-install)"
        return 1
    fi
    printf 'myrtui-install: %s [Y/n] ' "$_question" > /dev/tty
    _answer=""
    read -r _answer < /dev/tty || return 1
    case "$_answer" in
        n|N|no|NO|No) return 1 ;;
        *) return 0 ;;
    esac
}

as_root() {
    if [ "$(id -u)" -eq 0 ]; then
        "$@"
    elif have sudo; then
        sudo "$@"
    else
        return 1
    fi
}

pkg_install() {
    _pkg=$1
    if [ "$_os" = "Darwin" ]; then
        have brew || return 1
        brew install "$_pkg"
    elif have apt-get; then
        as_root apt-get install -y "$_pkg"
    elif have dnf; then
        as_root dnf install -y "$_pkg"
    elif have pacman; then
        as_root pacman -S --noconfirm "$_pkg"
    else
        return 1
    fi
}

ensure_tool() {
    _cmd=$1
    _label=$2
    _pkg=$3
    _manual=$4
    if have_tool "$_cmd"; then
        log "$_label already available"
        return 0
    fi
    log "$_label not found ($_cmd)"
    if ! ask_yes "install ${_label}?"; then
        log "skipped ${_label}; install manually: $_manual"
        return 1
    fi
    if pkg_install "$_pkg"; then
        hash -r
        if have_tool "$_cmd"; then
            log "$_label installed"
            return 0
        fi
    fi
    log "auto-install of $_label failed; install manually: $_manual"
    return 1
}

ensure_termtex() {
    if have_tool termtex; then
        log "termtex already available"
        return 0
    fi
    _manual="go install github.com/doug/termtex/cmd/termtex@latest"
    if ! have go; then
        log "termtex not found; Go is required to auto-install it. Manual: $_manual"
        return 1
    fi
    if ! ask_yes "install termtex via go (2-D math typesetting)?"; then
        log "skipped termtex; install manually: $_manual"
        return 1
    fi
    mkdir -p "$BIN_DIR"
    log "running: GOBIN=$BIN_DIR go install github.com/doug/termtex/cmd/termtex@latest"
    if GOBIN="$BIN_DIR" go install github.com/doug/termtex/cmd/termtex@latest; then
        hash -r
        if have_tool termtex; then
            log "termtex installed: $BIN_DIR/termtex"
            return 0
        fi
    fi
    log "termtex auto-install failed; install manually: $_manual"
    return 1
}

install_recommended_deps() {
    log "checking optional tools (all skippable; myrtui runs without them)"
    ensure_tool micro "micro (editor)" micro \
        "brew install micro / apt install micro" || true
    ensure_tool ov "ov (pager)" ov \
        "brew install ov / see project page" || true
    ensure_termtex || true

    log "optional tools: micro $(dep_status micro)  ov $(dep_status ov)  termtex $(dep_status termtex)"
    log "all of them are optional; re-run this script or install manually later."
}

# ── 1) 平台校验 ─────────────────────────────────────────────────────
_os=$(uname -s)
_arch=$(uname -m)
case "$_os:$_arch" in
    Darwin:x86_64 | Darwin:arm64 | Linux:x86_64 | Linux:aarch64 | Linux:arm64) ;;
    *) die "unsupported platform: ${_os} ${_arch} (macOS / Linux x86_64 / arm64 only)" ;;
esac
log "platform: $_os $_arch"

# ── 2) tmux >= 3.1 ──────────────────────────────────────────────────
tmux_ge_31() {
    have tmux || return 1
    # `tmux -V` → "tmux 3.8" / "tmux 3.2a" / "tmux next-3.4"；取首个 major.minor
    _v=$(tmux -V 2>/dev/null | sed -n 's/.*\([0-9][0-9]*\)\.\([0-9][0-9]*\).*/\1 \2/p' | head -1)
    [ -n "$_v" ] || return 1
    set -- $_v
    [ "$1" -gt 3 ] || { [ "$1" -eq 3 ] && [ "$2" -ge 1 ]; }
}

if tmux_ge_31; then
    log "tmux ready: $(tmux -V)"
else
    log "tmux missing or too old (need >= 3.1)"
    if ask_yes "install tmux?"; then
        if pkg_install tmux; then
            hash -r
        fi
    fi
    if tmux_ge_31; then
        log "tmux installed: $(tmux -V)"
    else
        log "WARNING: tmux still unavailable — myrtui **needs** tmux >= 3.1 at runtime;"
        log "         install manually: brew install tmux / apt install tmux / dnf install tmux / pacman -S tmux"
    fi
fi

# ── 3) Python >= 3.10（venv 兜底与 pipx/uv 引导都要用） ─────────────
PY=""
for c in python3.14 python3.13 python3.12 python3.11 python3.10 python3; do
    if have "$c" && "$c" -c 'import sys; raise SystemExit(0 if sys.version_info[:2] >= (3, 10) else 1)'; then
        PY=$c
        break
    fi
done
[ -n "$PY" ] || die "Python >= 3.10 not found; install python3 first (or https://astral.sh/uv then retry)"
log "python: $($PY -V)"

# ── 4) 既有安装通道探测（升级必须原地走同一条通道） ─────────────────
# 检测顺序即优先级：uv → pipx → venv 兜底。
existing=""
if have uv; then
    if uv tool list 2>/dev/null | grep -q '^myrtui[[:space:]]'; then
        existing="uv"
    fi
fi
if [ -z "$existing" ] && have pipx; then
    if pipx list --short 2>/dev/null | grep -q '^myrtui[[:space:]]'; then
        existing="pipx"
    fi
fi
if [ -z "$existing" ] && [ -x "$MYRTUI_PREFIX/venv/bin/myrtui" ]; then
    existing="venv"
fi
if [ -n "$existing" ]; then
    log "detected existing install channel: ${existing} (upgrading in place)"
fi

# 本机当前版本（仅用于结尾打印 old → new）
myrtui_bin_pre=$(command -v myrtui || true)
if [ -z "$myrtui_bin_pre" ] && [ -x "$BIN_DIR/myrtui" ]; then
    myrtui_bin_pre="$BIN_DIR/myrtui"
fi
old_version=""
if [ -n "$myrtui_bin_pre" ]; then
    old_version=$("$myrtui_bin_pre" --version 2>/dev/null | sed -n 's/^myrtui[[:space:]]*//p' || true)
fi
if [ -n "$old_version" ]; then
    log "currently installed: myrtui $old_version"
fi

# 旧版脚本的兜底 shim 可能还占着 ~/.local/bin/myrtui（真身已由 uv/pipx 管）：
# 不清掉的话，`ln -sf` 一类的兜底路径会顶掉正确那份。
if [ -n "$existing" ] && [ "$existing" != "venv" ] && [ -e "$BIN_DIR/myrtui" ]; then
    _shim_target=$(readlink "$BIN_DIR/myrtui" 2>/dev/null || true)
    case "$_shim_target" in
        "$MYRTUI_PREFIX"/*)
            log "cleanup: $BIN_DIR/myrtui -> ${_shim_target} (stale fallback shim)"
            rm -f "$BIN_DIR/myrtui"
            ;;
    esac
fi

# ── 5) 安装 / 升级（PyPI；全部落用户目录） ──────────────────────────
# 规则：已装过就**原地升级**，不允许掉进 venv 兜底去覆盖既有 shim。
installed=""
if [ "$existing" = "uv" ]; then
    log "upgrading (uv): uv tool install --force ${SPEC}"
    uv tool install --force "$SPEC" || die "uv upgrade failed (manual: uv tool install --force ${SPEC})"
    installed="uv"
elif [ "$existing" = "pipx" ]; then
    log "upgrading (pipx): pipx install --force ${SPEC}"
    pipx install --force "$SPEC" || die "pipx upgrade failed (manual: pipx install --force ${SPEC})"
    installed="pipx"
elif [ "$existing" = "venv" ]; then
    log "upgrading (venv): pip install --upgrade ${SPEC}"
    "$MYRTUI_PREFIX/venv/bin/python" -m pip install --quiet --upgrade "$SPEC" ||
        die "pip upgrade failed"
    installed="venv"
fi

# 首装（本机此前没有 myrtui）：uv → pipx → venv 三级降级
if [ -z "$installed" ] && have uv; then
    log "installing with uv: uv tool install --python $PY ${SPEC}"
    uv tool install --python "$PY" "$SPEC" && installed="uv"
fi
if [ -z "$installed" ] && have pipx; then
    log "installing with pipx: pipx install --python $PY ${SPEC}"
    pipx install --python "$PY" "$SPEC" && installed="pipx"
fi
if [ -z "$installed" ]; then
    log "falling back to venv at $MYRTUI_PREFIX"
    venv="$MYRTUI_PREFIX/venv"
    mkdir -p "$MYRTUI_PREFIX"
    "$PY" -m venv "$venv" || die "venv creation failed (Debian/Ubuntu may need: apt install python3-venv)"
    "$venv/bin/python" -m pip install --quiet --upgrade "$SPEC" || die "pip install failed"
    mkdir -p "$BIN_DIR"
    ln -sf "$venv/bin/myrtui" "$BIN_DIR/myrtui"
    installed="venv"
fi

# ── 6) PATH 提示 + 安装校验 ─────────────────────────────────────────
if ! command -v myrtui >/dev/null 2>&1 && [ -x "$BIN_DIR/myrtui" ]; then
    rcfile="$HOME/.zshrc"
    case "${SHELL:-}" in
        *bash) rcfile="$HOME/.bashrc" ;;
    esac
    log "note: ${BIN_DIR} is not on PATH; add this line to ${rcfile}:"
    printf 'myrtui-install:   export PATH="$HOME/.local/bin:$PATH"\n'
fi

# 校验**刚装的那一份**，而不是 PATH 上第一个 myrtui：多通道并存时
# （仓库 venv / 旧 pipx / homebrew）PATH 里可能是旧的，会打印出自相矛盾的版本。
installed_bin() {
    case "${installed:-}" in
        uv)
            _tool_dir=$(uv tool dir 2>/dev/null || true)
            if [ -n "$_tool_dir" ] && [ -x "$_tool_dir/myrtui/bin/myrtui" ]; then
                printf '%s\n' "$_tool_dir/myrtui/bin/myrtui"
                return 0
            fi
            ;;
        pipx)
            _pipx_bin=$(pipx environment --value PIPX_BIN_DIR 2>/dev/null || true)
            if [ -n "$_pipx_bin" ] && [ -x "$_pipx_bin/myrtui" ]; then
                printf '%s\n' "$_pipx_bin/myrtui"
                return 0
            fi
            ;;
        venv)
            if [ -x "$MYRTUI_PREFIX/venv/bin/myrtui" ]; then
                printf '%s\n' "$MYRTUI_PREFIX/venv/bin/myrtui"
                return 0
            fi
            ;;
    esac
    if [ -x "$BIN_DIR/myrtui" ]; then
        printf '%s\n' "$BIN_DIR/myrtui"
        return 0
    fi
    command -v myrtui || true
}

mrtui_bin=$(installed_bin || true)
[ -n "$mrtui_bin" ] || die "verification failed: myrtui executable not found"
version=$("$mrtui_bin" --version) || die "verification failed: myrtui --version errored"
_path_bin=$(command -v myrtui || true)
if [ -n "$_path_bin" ] && [ "$_path_bin" != "$mrtui_bin" ]; then
    _path_version=$("$_path_bin" --version 2>/dev/null || true)
    log "note: myrtui on PATH (${_path_bin}) is still ${_path_version:-unknown version}"
    log "      this run installed ${mrtui_bin}; reorder PATH or remove the old install"
fi
if [ -n "$old_version" ]; then
    log "upgraded: myrtui $old_version -> ${version#myrtui } (channel: ${installed})"
fi
log "installed (${installed}): ${version}"
log "usage: cd your-project && myrtui   (list instances: myrtui ls)"

# ── 7) 推荐组件（可选，逐项询问） ──────────────────────────────────
# 放在 myrtui 本体安装成功之后：brew / apt 慢或失败都不会挡住安装。
install_recommended_deps
