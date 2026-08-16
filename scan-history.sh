#!/usr/bin/env bash
# 公開に戻す前に、全履歴を秘密情報で洗う。
#
# purge-secrets.sh は「分かっている3ファイル」を消すだけ。
# このスクリプトは逆で、「まだ気づいていない秘密がないか」を全コミットの全ファイルで探す。
# 公開は後戻りできない操作なので、押す前にこれを通すこと。
#
#   bash scan-history.sh
#
# 何も出なければ公開して大丈夫。出たら、そのコミットを調べてから判断する。
set -uo pipefail
cd "$(dirname "$0")"

command -v git >/dev/null || { echo "git がありません" >&2; exit 1; }
[ -d .git ] || { echo "gitリポジトリの中で実行してください" >&2; exit 1; }

echo "対象コミット数: $(git rev-list --all | wc -l | tr -d ' ')"
echo "走査中…（小さなリポジトリなら数十秒）"
echo

# 値は出さない。ヒットしたコミットとファイル名と行番号だけを出す。
PAT='[0-9]{6,12}:AA[A-Za-z0-9_-]{30,}'                      # Telegram bot token
PAT="$PAT"'|AAAAAAAAAAAAAAAAAAAAA[A-Za-z0-9%._-]{40,}'      # X bearer token
PAT="$PAT"'|\b(AKIA|ASIA)[A-Z0-9]{16}\b'                    # AWS access key
PAT="$PAT"'|BEGIN (RSA|OPENSSH|EC|DSA|PGP)? ?PRIVATE KEY'   # 秘密鍵
PAT="$PAT"'|(client_secret|api_key|apikey|access_token|bearer_token|password|passwd|secret_key)[[:space:]]*[=:][[:space:]]*["'"'"']?[A-Za-z0-9/_+.%-]{16,}'
PAT="$PAT"'|"(name|value)"[[:space:]]*:.*"(sessionid|JSESSIONID|LJIDSESS|_session)"'

HITS=$(git grep -nIE "$PAT" $(git rev-list --all) -- 2>/dev/null \
        | sed -E 's/[A-Za-z0-9/_+.%:-]{16,}$/<値は伏せています>/' \
        | sort -u)

if [ -n "$HITS" ]; then
  echo "───────────────────────────────────────────────"
  echo " 履歴に秘密らしき文字列が残っています。公開しないでください。"
  echo "───────────────────────────────────────────────"
  echo "$HITS" | head -40
  echo
  echo " 表示は「コミット:ファイル:行番号」です。"
  echo " 中身を見るには:  git show <コミット>:<ファイル>"
  echo " 消すには purge-secrets.sh の --path に、そのファイルを足して実行し直す。"
  exit 1
fi

echo "✓ 既知のパターンでは何も見つかりませんでした。"
echo
echo "  ただし、これは万能ではありません。独自形式のキーや、"
echo "  ただの英数字にしか見えないパスワードは検出できません。"
echo "  下の3ファイルが消えていることも、あわせて確認します。"
echo

STILL=$(git log --all --oneline -- "untitled text.txt" "untitled text 2.txt" rakuten_session.json 2>/dev/null)
if [ -n "$STILL" ]; then
  echo "✗ 3ファイルがまだ履歴に残っています。先に purge-secrets.sh を実行してください。"
  exit 1
fi
echo "✓ 流出した3ファイルは履歴から消えています。"
echo
echo "公開に戻して差し支えありません。"
