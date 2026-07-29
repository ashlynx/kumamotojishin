#!/usr/bin/env bash
#
# Cloudflare へのデプロイ。あなたのターミナルで実行してください。
#
#   ./deploy.sh            巡回 → サイトをデプロイ → data.json を R2 に配置
#   ./deploy.sh --setup    初回だけ。Pagesプロジェクトと R2バケットを作る
#   ./deploy.sh --site     サイトだけデプロイ（巡回しない）
#   ./deploy.sh --data     data.json だけ更新（巡回してR2へ）
#
set -euo pipefail

PROJECT="kumamotojishin"
BUCKET="kumamotojishin-data"
SITE_DIR="site"

cd "$(dirname "$0")"

c()  { printf '\033[1;36m%s\033[0m\n' "$*"; }
ok() { printf '\033[1;32m✓ %s\033[0m\n' "$*"; }
ng() { printf '\033[1;31m✗ %s\033[0m\n' "$*" >&2; }

need() {
  command -v "$1" >/dev/null 2>&1 || { ng "$1 が見つかりません。$2"; exit 1; }
}

need python3 "https://www.python.org/ からインストールするか、Xcode Command Line Tools を入れてください"
need npx     "Node.js を入れてください: https://nodejs.org/"

WRANGLER="npx --yes wrangler@latest"

crawl() {
  c "一次情報を巡回します（16サイト・1分ほどかかります）"
  python3 crawl.py --inject "$SITE_DIR/index.html" --verbose
  # R2 を用意する前でも動くよう、サイトと同じ場所にも置いておく
  cp out/data.json "$SITE_DIR/data.json"
  ok "巡回完了"
}

deploy_site() {
  c "サイトをデプロイします"
  $WRANGLER pages deploy "$SITE_DIR" --project-name "$PROJECT" --commit-dirty=true
  ok "デプロイ完了"
}

upload_data() {
  c "data.json を R2 に置きます"
  if $WRANGLER r2 object put "$BUCKET/data.json" \
      --file out/data.json \
      --content-type "application/json; charset=utf-8" \
      --cache-control "public, max-age=60, s-maxage=120" \
      --remote; then
    ok "R2 に配置しました"
  else
    ng "R2 への配置に失敗しました（バケット未作成なら ./deploy.sh --setup を先に）"
    echo "  サイト自身に置いた data.json で動くので、公開自体は問題ありません。"
  fi
}

setup() {
  c "初回セットアップ"

  echo
  c "1/3 Cloudflare にログイン"
  $WRANGLER whoami >/dev/null 2>&1 || $WRANGLER login
  $WRANGLER whoami
  ok "ログイン確認"

  echo
  c "2/3 Pages プロジェクトを作る"
  if $WRANGLER pages project list 2>/dev/null | grep -q "$PROJECT"; then
    ok "既にあります: $PROJECT"
  else
    $WRANGLER pages project create "$PROJECT" --production-branch main
    ok "作成しました: $PROJECT"
  fi

  echo
  c "3/3 R2 バケットを作る"
  if $WRANGLER r2 bucket list 2>/dev/null | grep -q "$BUCKET"; then
    ok "既にあります: $BUCKET"
  else
    $WRANGLER r2 bucket create "$BUCKET"
    ok "作成しました: $BUCKET"
  fi

  cat <<'MSG'

────────────────────────────────────────────────
ここから先はブラウザでの作業です（CLIではできません）

【1】R2 を公開する
  ダッシュボード → R2 → kumamotojishin-data → Settings
  → Public access → Custom Domains → Connect Domain
  → data.kumamotojishin.jp  を入力
  ※ r2.dev の公開URLはレート制限があるので本番では使わないこと

【2】R2 の CORS を設定する（同じ Settings 画面）
  CORS Policy に貼り付け:
  [
    {
      "AllowedOrigins": [
        "https://kumamotojishin.jp",
        "https://kumamotojishin.pages.dev"
      ],
      "AllowedMethods": ["GET", "HEAD"],
      "AllowedHeaders": ["*"],
      "MaxAgeSeconds": 3600
    }
  ]

【3】サイトに独自ドメインを当てる
  Workers & Pages → kumamotojishin → Custom domains
  → Set up a domain → kumamotojishin.jp

  ※ 先に Cloudflare にドメインを追加し、レジストラ側の
    ネームサーバーを Cloudflare のものに変更しておくこと

ドメインがまだなら【1】〜【3】は後回しで構いません。
kumamotojishin.pages.dev で公開されます。
────────────────────────────────────────────────
MSG
}

case "${1:-}" in
  --setup) setup ;;
  --site)  deploy_site ;;
  --data)  crawl; upload_data ;;
  "")      crawl; deploy_site; upload_data
           echo
           ok "完了しました"
           echo "  https://${PROJECT}.pages.dev を開いて確認してください" ;;
  *)       echo "使い方: $0 [--setup|--site|--data]"; exit 1 ;;
esac
