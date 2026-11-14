#!/bin/bash
## ============================================================
## autosign — Trae CN / Qoder CN / CodeBuddy / 豆包 / MiniMax Code / 千问办公 每日签到
##
## 用法:
##   ./run.sh                 签到（等价于 run，幂等可重复执行）
##   ./run.sh run             同上
##   ./run.sh doctor          环境体检：缺什么、去哪装、下一步做什么（--verify 真实查状态）
##   ./run.sh status          只查状态，不领取
##   ./run.sh claim           只尝试领取
##   ./run.sh doubao-login    豆包首次扫码登录（有窗口，只需一次）
##   ./run.sh minimax-login   MiniMax Code 首次登录（有窗口，只需一次）
##   ./run.sh qwenwork-login  千问办公首次登录（有窗口，只需一次）
##   ./run.sh install-cron    安装 crontab 定时任务（幂等）
##   ./run.sh uninstall-cron  卸载 crontab 定时任务
##   ./run.sh cron-entry      打印当前要写入 crontab 的内容（调试用）
##   ./run.sh log [行数]      查看最近的运行日志
##   ./run.sh help            本帮助
##
## 依赖: python3 + openssl（Ubuntu 默认自带），无需 Node。
## Trae / CodeBuddy 读本机登录态直连接口；Qoder 需要客户端保持登录；
## 豆包需要一次性扫码登录（Playwright + 本机 Chrome，之后无头运行），
## MiniMax Code 需要一次性网页登录（登录态存本地，之后纯 HTTP 请求），
## 千问办公没有手动签到（每日积分 00:00 自动发放），脚本只做每日自动登录 + 积分核对，
## 未登录时只提示并跳过，不影响其他签到和 crontab 定时任务。
## 若项目内有 .venv（装 Playwright 用），run.sh 会自动优先使用它。
## ============================================================

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SCRIPTS_DIR="$SCRIPT_DIR/scripts"
LOG_DIR="$SCRIPT_DIR/logs"
LOG_FILE="$LOG_DIR/autosign.log"
LOCK_FILE="${TMPDIR:-/tmp}/autosign.lock"
# 豆包签到依赖 Playwright；装在项目内 .venv 时优先使用它
if [ -z "${PYTHON:-}" ]; then
    if [ -x "$SCRIPT_DIR/.venv/bin/python3" ]; then
        PYTHON="$SCRIPT_DIR/.venv/bin/python3"
    else
        PYTHON="python3"
    fi
fi

# 签到端点：Trae CN / Qoder CN / CodeBuddy / 豆包 / MiniMax Code / 千问办公。每个脚本独立输出
# 一行 JSON、独立退出，单个失败不会中断其余签到。
# 注意：Qoder 的 /campaigns 接口必须带 Cosy-ClientType: 10，缺了会误报 INACTIVE。
SIGNERS=("trae_signin.py" "qoder_signin.py" "codebuddy_signin.py" "doubao_signin.py" "minimax_signin.py" "qwenwork_signin.py")

# 定时任务每天跑 3 次（错开高峰），全部幂等，重复执行无副作用
CRON_SCHEDULE=("7 9 * * *" "7 15 * * *" "7 21 * * *")
CRON_BEGIN="# >>> autosign begin >>>"
CRON_END="# <<< autosign end <<<"

# ------------------------------------------------------------------ 输出
use_color() { [ -t 1 ]; }
say() { if use_color; then printf '\033[1;36m%s\033[0m\n' "$*"; else printf '%s\n' "$*"; fi; }
ok()  { if use_color; then printf '\033[1;32m%s\033[0m\n' "$*"; else printf '%s\n' "$*"; fi; }
warn(){ if use_color; then printf '\033[1;33m%s\033[0m\n' "$*"; else printf '%s\n' "$*"; fi; }
err() { if use_color; then printf '\033[1;31m%s\033[0m\n' "$*" >&2; else printf '%s\n' "$*" >&2; fi; }

log() {
    mkdir -p "$LOG_DIR"
    printf '[%s] [%s] %s\n' "$(date '+%F %T')" "$1" "${2:-}" >>"$LOG_FILE"
}

require_python() {
    if ! command -v "$PYTHON" >/dev/null 2>&1; then
        err "未找到 $PYTHON，请先安装：sudo apt-get install -y python3"
        return 1
    fi
    if ! command -v openssl >/dev/null 2>&1; then
        err "未找到 openssl（用于解密登录态）：sudo apt-get install -y openssl"
        return 1
    fi
    return 0
}

# ------------------------------------------------------------------ 执行签到
# 输出契约：每个签到脚本打印一行 JSON，取其中 result / report 两个字段
parse_field() {
    # $1=json 行  $2=字段名
    "$PYTHON" -c '
import json,sys
try:
    d=json.loads(sys.argv[1])
except Exception:
    print(""); sys.exit(0)
print(d.get(sys.argv[2]) or "")
' "$1" "$2"
}

run_one() {
    local script="$1" mode="${2:-auto}"
    local path="$SCRIPTS_DIR/$script"
    local out rc result report

    if [ ! -f "$path" ]; then
        warn "  跳过 $script（脚本不存在）"
        log WARN "skip $script (not found)"
        return 0
    fi

    out="$("$PYTHON" "$path" "$mode" 2>&1)"; rc=$?
    # 取最后一行 JSON（脚本保证只打印一行，兜底前面的警告输出）
    local json_line
    json_line="$(printf '%s\n' "$out" | grep -m1 '^{' || printf '%s' "$out")"

    result="$(parse_field "$json_line" result)"
    report="$(parse_field "$json_line" report)"
    [ -n "$report" ] || report="$out"

    case "$result" in
        ALREADY|INACTIVE|PENDING)
            ok "  [$script] $report"
            log INFO "$script $result: $report"
            return 0 ;;
        CLAIMED)
            ok "  [$script] ✓ $report"
            log INFO "$script CLAIMED: $report"
            return 0 ;;
        NO_AUTH|NO_SESSION)
            # Qoder 依赖桌面客户端/钥匙串，豆包、MiniMax 和千问办公依赖一次性登录；未登录
            # 属于预期状态，提示后跳过，不让整轮 cron 失败。
            case "$script" in
                qoder_signin.py|doubao_signin.py|minimax_signin.py|qwenwork_signin.py)
                    warn "  [$script] 跳过：$report"
                    log WARN "$script $result: $report"
                    return 0 ;;
            esac
            # CodeBuddy 与 Trae 一致按硬失败处理：本机已安装 CodeBuddy，
            # 出现 NO_AUTH 说明登录态丢失/文件被删，应当立刻发现并重新登录；
            # 若部署到没装 CodeBuddy 的机器，请从 SIGNERS 里移除它，而不是静默跳过。
            err "  [$script] [$result] $report"
            log ERROR "$script $result: $report"
            return 1 ;;
        "")
            err "  [$script] 无输出（exit=$rc）: $out"
            log ERROR "$script no output (exit=$rc): $out"
            return 1 ;;
        *)
            err "  [$script] [$result] $report"
            log ERROR "$script $result: $report"
            return 1 ;;
    esac
}

cmd_run() {
    local mode="${1:-auto}"
    require_python || return 2

    say "== autosign $(date '+%F %T') 模式=$mode =="
    local failed=0 total=0

    for s in "${SIGNERS[@]}"; do
        total=$((total + 1))
        run_one "$s" "$mode" || failed=$((failed + 1))
    done

    if [ "$failed" -eq 0 ]; then
        say "完成：$total 项全部正常"
        log INFO "run finished: $total ok"
        return 0
    fi
    err "完成：$failed/$total 项失败"
    log ERROR "run finished: $failed/$total failed"
    return 1
}

# ------------------------------------------------------------------ 环境体检
cmd_doctor() {
    if ! command -v "$PYTHON" >/dev/null 2>&1; then
        err "未找到 python3，请先安装 Python 3.8+：https://www.python.org/downloads/"
        return 2
    fi
    "$PYTHON" "$SCRIPTS_DIR/doctor.py" "$@"
    local rc=$?
    log INFO "doctor rc=$rc"
    return $rc
}

# ------------------------------------------------------------------ crontab
cron_lines() {
    local entry
    printf '%s\n' "$CRON_BEGIN"
    printf '# 自动生成，手动改动会在下次 install-cron 时被覆盖；卸载: ./run.sh uninstall-cron\n'
    printf 'SHELL=/bin/sh\nPATH=/usr/local/bin:/usr/bin:/bin\nMAILTO=""\n'
    mkdir -p "$LOG_DIR"
    for entry in "${CRON_SCHEDULE[@]}"; do
        # flock -n 防止上一次还没跑完又起一次
        #   stdout/stderr → logs/cron.out（人看的原始输出）
        #   log() 结构化行 → logs/autosign.log
        # cron 默认带 QODER_ALLOW_OFFLINE=1：定时任务不应依赖 Qoder 客户端常开，
        # 只要本地登录态 token 还有效就能签到；手动 ./run.sh 仍会先检查客户端。
        printf '%s QODER_ALLOW_OFFLINE=1 flock -n %s %s run >> %s 2>&1\n' \
            "$entry" "$LOCK_FILE" "$SCRIPT_DIR/run.sh" "$LOG_DIR/cron.out"
    done
    printf '%s\n' "$CRON_END"
}

current_crontab() { crontab -l 2>/dev/null || true; }

cmd_install_cron() {
    command -v crontab >/dev/null 2>&1 || { err "系统没有 crontab，无法安装定时任务"; return 1; }
    [ -x "$SCRIPT_DIR/run.sh" ] || chmod +x "$SCRIPT_DIR/run.sh"

    local merged
    merged="$(current_crontab | sed "\|^$CRON_BEGIN\$|,\|^$CRON_END\$|d" ; cron_lines)"
    printf '%s\n' "$merged" | crontab -

    if crontab -l | grep -qxF "$CRON_BEGIN"; then
        ok "已安装 crontab 定时任务："
        crontab -l | sed -n "/^$CRON_BEGIN\$/,/^$CRON_END\$/p" | sed 's/^/    /'
        log INFO "crontab installed"
        return 0
    fi
    err "crontab 安装失败"
    log ERROR "crontab install failed"
    return 1
}

cmd_uninstall_cron() {
    command -v crontab >/dev/null 2>&1 || { err "系统没有 crontab"; return 1; }
    local kept
    kept="$(current_crontab | sed "\|^$CRON_BEGIN\$|,\|^$CRON_END\$|d")"
    if [ -z "$(printf '%s' "$kept" | tr -d '[:space:]')" ]; then
        crontab -r 2>/dev/null || true
    else
        printf '%s\n' "$kept" | crontab -
    fi
    ok "已卸载 autosign 的 crontab 定时任务"
    log INFO "crontab removed"
}

cmd_doubao_login() {
    require_python || return 2
    local path="$SCRIPTS_DIR/doubao_signin.py"
    if [ ! -f "$path" ]; then
        err "缺少 $path"
        return 2
    fi
    say "打开浏览器扫码登录豆包（登录态保存到 ~/.cache/autosign/doubao.state.json）"
    "$PYTHON" "$path" login
    local rc=$?
    log INFO "doubao login rc=$rc"
    return $rc
}

cmd_minimax_login() {
    require_python || return 2
    local path="$SCRIPTS_DIR/minimax_signin.py"
    if [ ! -f "$path" ]; then
        err "缺少 $path"
        return 2
    fi
    say "打开浏览器登录 MiniMax（登录态保存到 ~/.cache/autosign/minimax.state.json）"
    "$PYTHON" "$path" login
    local rc=$?
    log INFO "minimax login rc=$rc"
    return $rc
}

cmd_qwenwork_login() {
    require_python || return 2
    local path="$SCRIPTS_DIR/qwenwork_signin.py"
    if [ ! -f "$path" ]; then
        err "缺少 $path"
        return 2
    fi
    say "打开浏览器登录千问办公（登录态保存到 ~/.cache/autosign/qwenwork.state.json）"
    "$PYTHON" "$path" login
    local rc=$?
    log INFO "qwenwork login rc=$rc"
    return $rc
}

cmd_log() {
    local n="${1:-40}"
    if [ ! -f "$LOG_FILE" ]; then
        warn "还没有日志：$LOG_FILE"
        return 0
    fi
    tail -n "$n" "$LOG_FILE"
}

usage() {
    awk 'NR>1 && /^##/{sub(/^## ?/,""); print; next} NR>1{exit}' "${BASH_SOURCE[0]}"
}

# ------------------------------------------------------------------ 入口
cmd="${1:-run}"
case "$cmd" in
    run|sign|签到)      cmd_run auto ;;
    doctor|check|体检)  shift; cmd_doctor "$@" ;;
    status)             cmd_run status ;;
    claim)              cmd_run claim ;;
    doubao-login|doubao_login|login) cmd_doubao_login ;;
    minimax-login|minimax_login) cmd_minimax_login ;;
    qwenwork-login|qwenwork_login) cmd_qwenwork_login ;;
    install-cron|install) cmd_install_cron ;;
    uninstall-cron|uninstall) cmd_uninstall_cron ;;
    cron-entry)         cron_lines ;;
    log)                shift; cmd_log "${1:-40}" ;;
    help|-h|--help)     usage ;;
    *)                  err "未知命令: $cmd"; usage; exit 2 ;;
esac
exit $?
