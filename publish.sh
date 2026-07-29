#!/usr/bin/env bash
#
# サイトの変更を公開する。これ1本で完結します。
#
#   ./publish.sh                 変更をコミットして push（GitHub Actions が巡回とデプロイを実行）
#   ./publish.sh "断水戸数を更新"  コミットメッセージを指定
#
# push すると GitHub Actions が動き、
#   1. 一次情報サイトを巡回
#   2. data.json を R2 に配置
#   3. サイトを Cloudflare Pages にデプロイ
# まで自動で行われます。手元での wrangler 実行は不要です。
#
set -euo pipefail
cd "$(dirname "$0")"

MSG="${1:-サイトを更新}"

if [ -f .git/index.lock ]; then
  echo "古いロックファイルを削除します"
  rm -f .git/index.lock
fi

if git diff --quiet && git diff --cached --quiet && [ -z "$(git status --porcelain)" ]; then
  echo "変更がありません。"
  exit 0
fi

git add -A
git commit -m "$MSG"
git push

echo
echo "push しました。GitHub Actions が巡回とデプロイを実行します（2分ほど）。"
echo "進行状況:  gh run watch"
echo "公開先:    https://kumamotojishin.jp"
