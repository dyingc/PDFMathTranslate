#!/usr/bin/env bash
# Pull upstream pdf2zh changes into this fork, then replay our webapp work on top.
#
#   ./webapp/sync-upstream.sh [--branch NAME] [--no-rebase] [--push]
#
#   --branch NAME  额外变基到 main 的功能分支（默认：当前分支；在 main 上则跳过）
#   --no-rebase    只更新 main
#   --push         成功后推送 main（功能分支用 --force-with-lease）
#
# 我们的改动就住在 main 上（几乎全在 webapp/ 下）。上游用 merge 并入，这样永远
# 不需要 force-push 默认分支。
set -euo pipefail

UPSTREAM_URL="https://github.com/PDFMathTranslate/PDFMathTranslate.git"
UPSTREAM=upstream
MAIN=main

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_DIR"

branch=""
do_rebase=1
do_push=0
while [ $# -gt 0 ]; do
    case "$1" in
        --branch)    branch="$2"; shift 2 ;;
        --no-rebase) do_rebase=0; shift ;;
        --push)      do_push=1; shift ;;
        -h|--help)   sed -n '2,10p' "${BASH_SOURCE[0]}"; exit 0 ;;
        *) echo "未知参数: $1（用 -h 查看用法）" >&2; exit 2 ;;
    esac
done

[ -n "$branch" ] || branch="$(git rev-parse --abbrev-ref HEAD)"

# Refuse to touch a dirty tree — a failed rebase on top of uncommitted work is a
# genuinely painful mess to unwind.
if [ -n "$(git status --porcelain)" ]; then
    echo "错误：工作区有未提交的改动，请先 commit 或 stash。" >&2
    git status --short >&2
    exit 1
fi

original_branch="$(git rev-parse --abbrev-ref HEAD)"
restore() { git checkout -q "$original_branch" 2>/dev/null || true; }
trap restore EXIT

git remote get-url "$UPSTREAM" >/dev/null 2>&1 || {
    echo "添加 upstream: $UPSTREAM_URL"
    git remote add "$UPSTREAM" "$UPSTREAM_URL"
}

before="$(git rev-parse "$UPSTREAM/$MAIN" 2>/dev/null || echo "")"

echo "==> 拉取 $UPSTREAM"
git fetch --prune "$UPSTREAM"

echo "==> 合并 $UPSTREAM/$MAIN 到 $MAIN"
git checkout -q "$MAIN"
# Merge, not rebase: main carries our own commits, and rebasing it would mean
# force-pushing the fork's default branch on every sync. A merge commit is a
# cheap price for never rewriting published history.
if ! git merge --no-edit "$UPSTREAM/$MAIN"; then
    echo >&2
    echo "合并冲突。解决后执行 git commit，或 git merge --abort 放弃本次同步。" >&2
    trap - EXIT          # leave the merge in progress for the user
    exit 1
fi
after="$(git rev-parse "$UPSTREAM/$MAIN")"

if [ -z "$before" ] || [ "$before" = "$after" ]; then
    echo "已是最新，上游没有新提交。"
else
    echo "上游新提交："
    git --no-pager log --oneline "$before..$after" | sed 's/^/    /'
fi

if [ "$do_rebase" = 1 ] && [ "$branch" != "$MAIN" ]; then
    echo "==> 将 $branch 变基到 $MAIN"
    git checkout -q "$branch"
    if ! git rebase "$MAIN"; then
        echo >&2
        echo "变基出现冲突。解决后执行 git rebase --continue，" >&2
        echo "或执行 git rebase --abort 回到变基前的状态。" >&2
        trap - EXIT          # leave the rebase in progress for the user
        exit 1
    fi
fi

# A conflict-free rebase does not mean the app still works: it depends on a few
# pdf2zh internals that upstream can change without ever touching webapp/.
smoke_ok=1
if [ -x .venv/bin/python ]; then
    echo "==> 冒烟测试"
    .venv/bin/python -m webapp.smoke_test || smoke_ok=0
    if [ "$smoke_ok" = 0 ]; then
        echo "冒烟测试未通过——上游改动可能已破坏本应用，请先修复再推送。" >&2
    fi
else
    echo "（跳过冒烟测试：未找到 .venv）"
fi

if [ "$do_push" = 1 ] && [ "$smoke_ok" = 0 ]; then
    echo "因冒烟测试失败，已跳过推送。" >&2
    exit 1
fi

if [ "$do_push" = 1 ]; then
    echo "==> 推送 origin"
    git push origin "$MAIN"
    if [ "$do_rebase" = 1 ] && [ "$branch" != "$MAIN" ]; then
        # Rebase rewrote the branch; --force-with-lease refuses to clobber
        # commits pushed from elsewhere in the meantime.
        git push --force-with-lease origin "$branch"
    fi
else
    echo
    echo "未推送。确认无误后："
    echo "    git push origin $MAIN"
    if [ "$branch" != "$MAIN" ]; then
        echo "    git push --force-with-lease origin $branch"
    fi
fi

echo "完成。"
