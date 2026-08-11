#!/usr/bin/env bash
# Pull upstream pdf2zh changes into this fork, then replay our webapp work on top.
#
#   ./webapp/sync-upstream.sh [--branch NAME] [--no-rebase] [--push]
#
#   --branch NAME  要变基的功能分支（默认：当前分支）
#   --no-rebase    只更新 main，不动功能分支
#   --push         成功后推送 main，并用 --force-with-lease 推送功能分支
#
# 为什么是 rebase 而不是 merge：我们的改动几乎全部在 webapp/ 下，是上游历史之上
# 的一薄层。保持线性能让"我们改了什么"始终一目了然，也让将来给上游提 PR 更容易。
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

echo "==> 拉取 $UPSTREAM"
git fetch --prune "$UPSTREAM"

before="$(git rev-parse "$MAIN")"
echo "==> 更新 $MAIN → $UPSTREAM/$MAIN"
git checkout -q "$MAIN"
# Fast-forward only: main is meant to be a clean mirror of upstream. If this
# fails, someone committed onto main directly and that needs a human decision.
if ! git merge --ff-only "$UPSTREAM/$MAIN"; then
    echo "错误：$MAIN 无法快进到 $UPSTREAM/$MAIN（本地有独立提交）。" >&2
    echo "请手动处理，例如：git rebase $UPSTREAM/$MAIN" >&2
    exit 1
fi
after="$(git rev-parse "$MAIN")"

if [ "$before" = "$after" ]; then
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
