#!/bin/sh
# Agent 后台升级脚本
# 用法: sh agent-upgrade.sh <ROOT> <DATA_DIR> <UNIT> <BRANCH> <TARGET_VERSION>
# 退出码: 0=成功, 75=已有升级在运行, 78=版本验证不匹配, 其他=失败
set -eu

ROOT="$1"
DATA_DIR="$2"
UNIT="$3"
BRANCH="$4"
TARGET_VERSION="$5"

mkdir -p "$DATA_DIR"

STATUS_FILE="$DATA_DIR/.upgrade-status.json"
TASK_LOCK_FILE="$DATA_DIR/.upgrade-task.lock"

write_status() {
    # write_status <state> <message> <exit_code>
    tmp="$STATUS_FILE.tmp.$$"
    printf '{"state":"%s","unit":"%s","target_version":"%s","message":"%s","exit_code":%s,"updated_at":%s}\n' \
        "$1" "$UNIT" "$TARGET_VERSION" "$2" "$3" "$(date +%s)" > "$tmp"
    mv "$tmp" "$STATUS_FILE"
}

cleanup_upgrade_lock() { :; }

finish_upgrade() {
    code="$?"
    if [ "$code" -eq 0 ]; then
        write_status succeeded "Agent upgrade completed" 0
    elif [ "$code" -eq 75 ]; then
        write_status failed "Another Agent upgrade is already running" 75
    elif [ "$code" -eq 78 ]; then
        write_status failed "Installed Agent version did not match the target version" 78
    else
        write_status failed "Agent upgrade task failed" "$code"
    fi
    cleanup_upgrade_lock
    exit "$code"
}

trap finish_upgrade EXIT

write_status running "Agent upgrade is running" 0

# 互斥锁：优先使用 flock，降级为 mkdir 锁目录
if command -v flock > /dev/null 2>&1; then
    # shellcheck disable=SC2039
    exec 8> "$TASK_LOCK_FILE"
    flock -n 8 || exit 75
else
    LOCK_DIR="$TASK_LOCK_FILE.d"
    mkdir "$LOCK_DIR" || exit 75
    cleanup_upgrade_lock() { rmdir "$LOCK_DIR" > /dev/null 2>&1 || true; }
fi

# 等待当前请求处理完毕
sleep 2

# 确保工作区无未提交修改
test -z "$(git -C "$ROOT" status --porcelain --untracked-files=no)"

# 执行安装脚本
env BRANCH="$BRANCH" CHOICE=1 INSTALL_DIR="$ROOT" sh "$ROOT/scripts/install-agent.sh"

# 版本验证
actual_version="$(git -C "$ROOT" rev-parse --short HEAD)"
if [ -n "$TARGET_VERSION" ] && [ "$actual_version" != "$TARGET_VERSION" ]; then
    echo "Agent upgrade finished at $actual_version, expected $TARGET_VERSION" >&2
    exit 78
fi
