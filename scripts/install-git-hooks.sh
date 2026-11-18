#!/bin/sh
# ============================================================================
# 安装 value-dashboard 的 git hooks（Git Bash / Linux / macOS 用；幂等，可重复跑）
#
#   sh scripts/install-git-hooks.sh              # 安装
#   sh scripts/install-git-hooks.sh --uninstall  # 卸载（恢复 git 默认 hooks 目录）
#   sh scripts/install-git-hooks.sh --help
#
# 做的事只有三件：
#   1. git config --local core.hooksPath .githooks
#   2. chmod +x .githooks/pre-commit
#   3. 把 .githooks/* 的 CRLF 刷成 LF（core.autocrlf=true 时 checkout 会把钩子写成 CRLF，
#      而 `#!/bin/sh\r` 会让钩子直接起不来 —— 这是 Windows 上最常见的"装了却没生效"）
# 不碰工作区里的其它文件，不改 .gitignore，不提交任何东西。
# ============================================================================
set -eu

HOOK_DIR_NAME=".githooks"
HOOK_FILE="pre-commit"

YELLOW="" BOLD="" RESET=""
if [ -t 1 ]; then YELLOW=$(printf '\033[33m'); BOLD=$(printf '\033[1m'); RESET=$(printf '\033[0m'); fi
info() { printf '%s\n' "$*"; }
warn() { printf '%s[warn]%s %s\n' "$YELLOW" "$RESET" "$*" >&2; }
die() { printf '[error] %s\n' "$*" >&2; exit 1; }

usage() {
    sed -n '2,12p' "$0" | sed 's/^# \{0,1\}//'
}

ACTION="install"
case "${1:-}" in
    "") ACTION="install" ;;
    --uninstall | -u) ACTION="uninstall" ;;
    --help | -h) usage; exit 0 ;;
    *) die "未知参数：$1（可用：--uninstall / --help）" ;;
esac

SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd) || die "无法定位脚本所在目录"
REPO_ROOT=$(CDPATH= cd -- "$SCRIPT_DIR/.." && pwd) || die "无法定位仓库根"
cd "$REPO_ROOT" || die "无法进入 $REPO_ROOT"

command -v git >/dev/null 2>&1 || die "找不到 git"
git rev-parse --git-dir >/dev/null 2>&1 || die "$REPO_ROOT 不是 git 仓库"

# ---------------------------------------------------------------- 卸载
if [ "$ACTION" = "uninstall" ]; then
    current=$(git config --local --get core.hooksPath 2>/dev/null || true)
    if [ -z "$current" ]; then
        info "core.hooksPath 本来就没设置，无需卸载（幂等）。"
    else
        git config --local --unset core.hooksPath
        info "已移除 core.hooksPath（原值：$current）。git 回到默认 .git/hooks 目录。"
    fi
    info ""
    info "验证：git config --local --get core.hooksPath   # 应无输出且退出码 1"
    exit 0
fi

# ---------------------------------------------------------------- 安装
[ -f "$HOOK_DIR_NAME/$HOOK_FILE" ] || die "缺少 $HOOK_DIR_NAME/$HOOK_FILE，无法安装（仓库不完整？）"

# 1) 幂等设置 core.hooksPath
current=$(git config --local --get core.hooksPath 2>/dev/null || true)
if [ "$current" = "$HOOK_DIR_NAME" ]; then
    info "core.hooksPath 已是 $HOOK_DIR_NAME，跳过设置（幂等）。"
else
    git config --local core.hooksPath "$HOOK_DIR_NAME"
    if [ -n "$current" ]; then
        info "core.hooksPath：$current -> $HOOK_DIR_NAME"
    else
        info "core.hooksPath：未设置 -> $HOOK_DIR_NAME"
    fi
fi

# 2) CRLF -> LF
#    core.autocrlf=true 时 checkout 会把钩子写成 CRLF；Git for Windows 多数情况能忍，
#    但 macOS/Linux 的 `#!/bin/sh\r` 会 bad interpreter，属于"装了却没生效"的经典事故。
#    注意：MSYS 的 grep/wc 会做文本模式转换（把 \r 吃掉），所以用 od 按字节判断。
has_cr() {
    od -An -v -tx1 "$1" 2>/dev/null | tr ' ' '\n' | grep -qx '0d'
}
fixed_eol=0
for f in "$HOOK_DIR_NAME"/*; do
    [ -f "$f" ] || continue
    if has_cr "$f"; then
        tmp="$f.tmp.$$"
        tr -d '\r' <"$f" >"$tmp" && mv "$tmp" "$f"
        fixed_eol=$((fixed_eol + 1))
    fi
done
if [ "$fixed_eol" -gt 0 ]; then
    warn "已把 $fixed_eol 个钩子文件的 CRLF 刷成 LF（避免 #!/bin/sh\\r 在 Unix 上起不来）。"
fi

# 3) 可执行位（Windows 上无害，Linux/macOS 必需）
chmod +x "$HOOK_DIR_NAME/$HOOK_FILE" 2>/dev/null || warn "chmod +x 失败，请在类 Unix 系统上手动执行。"

# 4) 语法自检，尽早发现钩子本身写坏了
if command -v sh >/dev/null 2>&1; then
    sh -n "$HOOK_DIR_NAME/$HOOK_FILE" || die "钩子语法检查失败，安装中止：$HOOK_DIR_NAME/$HOOK_FILE"
fi

info ""
info "${BOLD}安装完成${RESET}（钩子会在 git commit 时自动运行，可用 --no-verify 绕过）"
info ""
info "验证方式："
info "  1) git config --local --get core.hooksPath          # 应输出 .githooks"
info "  2) sh -n .githooks/pre-commit                        # 应无输出"
info "  3) bash .githooks/pre-commit; echo exit=\$?          # 无暂存内容时快速 exit=0"
info "  4) 真实触发：随便改一个文件 git add 后 git commit，看是否出现 [pre-commit] 输出"
info ""
info "钩子做了什么 / 怎么绕过：见 .githooks/README.md"
info "卸载：sh scripts/install-git-hooks.sh --uninstall"
