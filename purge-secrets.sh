#!/bin/bash
# 公開リポジトリに混入した秘密情報を、履歴ごと消すためのスクリプト。
#
# 2026-08-14、ashlynx/kumamotojishin に次の3ファイルが公開された状態になっていた。
#   untitled text.txt    … VPSのroot SSHログイン（IP付き）、X APIのclient secretとbearer、Telegramのトークンとchat id
#   untitled text 2.txt  … heteml（shindenryoku.jp）のSSHアカウントとパスワードらしき文字列
#   rakuten_session.json … 楽天の生きているセッションCookie 26個（8月7日から入ったまま）
#
# ★ このスクリプトを実行しても、漏れた認証情報は無効になりません。
#   公開リポジトリの秘密は、push された時点で機械が拾っています（今回GitGuardianが
#   気づいたのが証拠です）。GitHubは force-push のあとも、古いコミットをSHA指定で
#   しばらく参照できる状態で残します。
#   ＝ 失効・入れ替えを済ませるまでは、何も解決していません。先にそちらをやってください。
#     1. Telegram   BotFather で /revoke
#     2. X          Developer Portal で Keys and tokens を Regenerate
#     3. VPS        rootのパスワード変更＋SSH鍵の入れ替え（IPが出ているので最優先）
#     4. heteml     FTP/SSHパスワードの変更
#     5. 楽天       全端末からログアウト → パスワード変更
#
# 使い方（ご自身のターミナルで、リポジトリの中で実行）:
#   bash purge-secrets.sh
set -euo pipefail

cd "$(dirname "$0")"

if [ ! -d .git ]; then
  echo "エラー: gitリポジトリの中で実行してください" >&2
  exit 1
fi

echo "▼ 1/5 念のためバックアップを取ります"
BK="../kumamotojishin-backup-$(date +%Y%m%d-%H%M%S).bundle"
git bundle create "$BK" --all
echo "   $BK に保存しました（やり直したくなったらこれを使います）"

echo "▼ 2/5 作業ツリーからの削除ぶんをコミットします"
git add -A .gitignore
git rm --cached --ignore-unmatch -q "untitled text.txt" "untitled text 2.txt" rakuten_session.json || true
if ! git diff --cached --quiet; then
  git commit -q -m "秘密情報を含む3ファイルを追跡から外す

untitled text.txt / untitled text 2.txt（SSHログイン情報・X APIキー・Telegramトークン）
rakuten_session.json（楽天のセッションCookie）
.gitignore に untitled* / *.env / *session*.json を追加して再発を防ぐ。"
  echo "   コミットしました"
else
  echo "   コミットするものはありませんでした（すでに済み）"
fi

echo "▼ 3/5 全履歴から3ファイルを消します"
# git-filter-repo があればそちらのほうが速くて安全。なければ filter-branch を使う。
if command -v git-filter-repo >/dev/null 2>&1; then
  git filter-repo --force \
    --path "untitled text.txt" \
    --path "untitled text 2.txt" \
    --path rakuten_session.json \
    --invert-paths
  echo "   git-filter-repo で消しました"
  echo "   ※ filter-repo は origin を外します。次で付け直します。"
  git remote add origin https://github.com/ashlynx/kumamotojishin.git 2>/dev/null || true
else
  FILTER_BRANCH_SQUELCH_WARNING=1 git filter-branch --force --index-filter \
    'git rm --cached --ignore-unmatch -q "untitled text.txt" "untitled text 2.txt" rakuten_session.json' \
    --prune-empty --tag-name-filter cat -- --all
  echo "   git filter-branch で消しました"
fi

echo "▼ 4/5 手元の古い参照を捨てて、リポジトリを詰め直します"
rm -rf .git/refs/original || true
git reflog expire --expire=now --all
git gc --prune=now --aggressive --quiet

echo "▼ 5/5 確認"
if git log --all --oneline -- "untitled text.txt" "untitled text 2.txt" rakuten_session.json | grep -q .; then
  echo "   ✗ まだ履歴に残っています。中断します。" >&2
  exit 1
fi
echo "   ✓ 履歴から消えました"

cat <<'MSG'

--------------------------------------------------------------------
ここまでは手元だけの変更です。GitHubへ反映するには、次を実行します。

    git push --force --all origin
    git push --force --tags origin

force-push なので、他の場所にクローンがある場合はそちらが壊れます。
このリポジトリを触っているのが自分だけなら、そのまま進めて大丈夫です。

push が終わったら、最後にGitHubサポートへ依頼してください。
force-push のあとも、古いコミットはSHAを知っていれば見られる状態で残ります。
  https://support.github.com/contact
  「Please permanently remove cached views of commits in
   ashlynx/kumamotojishin (leaked credentials)」と伝えれば通じます。

くり返しますが、いちばん大事なのは認証情報の失効です。
このスクリプトは後片付けであって、対策ではありません。
--------------------------------------------------------------------
MSG
