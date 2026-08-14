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

MSG="サイトを更新"
for a in "$@"; do
  case "$a" in
    --allow-new) ;;                 # フラグはメッセージにしない
    *) MSG="$a"; break ;;
  esac
done

# site/ に本体以外のHTMLが紛れていないか確認する。
# ダウンロードしたファイルが混ざると、そのまま公開されて
# 「古い内容の別URL」が生き続けてしまうため、ここで止める。
# 404.html は Cloudflare Pages が「見つからないURL」に使う正規のファイルなので除外する。
# これを置かないと、存在しないパスすべてに index.html が 200 で返り、
# ゴミURLがそのまま重複ページとして検索に拾われてしまう。
STRAY=$(find site -maxdepth 1 -name "*.html" ! -name "index.html" ! -name "404.html" 2>/dev/null || true)
if [ -n "$STRAY" ]; then
  echo "site/ に index.html 以外のHTMLがあります。このまま公開すると、古い内容のページが"
  echo "別のURLで見られる状態になります。移動または削除してから再実行してください。"
  echo
  echo "$STRAY" | sed 's/^/  /'
  exit 1
fi

if [ -f .git/index.lock ]; then
  echo "古いロックファイルを削除します"
  rm -f .git/index.lock
fi

if git diff --quiet && git diff --cached --quiet && [ -z "$(git status --porcelain)" ]; then
  echo "変更がありません。"
  exit 0
fi

# 見覚えのない新規ファイルが混ざっていないか確認する。
#
# 2026年8月14日、この行がなかったせいで事故を起こした。
# エディタの「名称未設定」メモ2つ（untitled text.txt / untitled text 2.txt）が
# このフォルダに保存されていて、下の git add -A がまとめて拾い、
# VPSのroot SSHログイン・X APIのキー・Telegramのトークンが公開リポジトリに出た。
# 楽天のセッションCookie（rakuten_session.json）も、同じ経路で8月7日から1週間出ていた。
#
# 通常の更新で新しいファイルが増えることはめったにない。増えたときは必ず目で見る。
# 意図して追加するときだけ、--allow-new を付けて実行する。
ALLOW_NEW=0
for a in "$@"; do [ "$a" = "--allow-new" ] && ALLOW_NEW=1; done

UNTRACKED=$(git ls-files --others --exclude-standard)
if [ -n "$UNTRACKED" ] && [ "$ALLOW_NEW" -eq 0 ]; then
  echo "追跡されていないファイルがあります。公開リポジトリなので、中身を確かめてください。"
  echo
  echo "$UNTRACKED" | sed 's/^/  /'
  echo
  echo "秘密情報（パスワード・APIキー・トークン・セッション）が入っていないか見てから、"
  echo "問題なければ次のどちらかにしてください。"
  echo "  ・公開してよいファイル   →  ./publish.sh \"$MSG\" --allow-new"
  echo "  ・公開したくないファイル →  リポジトリの外へ移動するか .gitignore に追記"
  exit 1
fi

git add -A
git commit -m "$MSG"
git push

echo
echo "push しました。GitHub Actions が巡回とデプロイを実行します（2分ほど）。"
echo "進行状況:  gh run watch"
echo "公開先:    https://kumamotojishin.jp"
