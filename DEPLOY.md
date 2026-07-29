# 公開手順（Cloudflare）

想定: **月額 $0**（ドメイン代のみ）。所要 30〜60分。

---

## 構成と、なぜこの形にしたか

```
  ブラウザ
    ├── kumamotojishin.jp        → Cloudflare Pages（静的HTML・無料・帯域無制限）
    └── data.kumamotojishin.jp   → Cloudflare R2（10分ごとに更新されるJSON・エグレス無料）
                                        ▲
                                        │ 10分おきに書き込み
                                   GitHub Actions（パブリックリポジトリは無料）
                                        │ 巡回
                                        ▼
                              気象庁・各自治体・消防庁・内閣府（16サイト）
```

**なぜ Workers の Cron を使わないのか。** Cloudflare Workers 無料プランは1実行あたり **CPU 10ms** が上限です。16サイト分のXML/JSONをパースする処理は確実に超えます（Cloudflare自身のドキュメントに「大きなペイロードのパースは通常10〜20ms」と明記があります）。超えると Error 1102 で実行が打ち切られます。Workersでやるなら **有料プラン $5/月** が最低ライン。GitHub Actions はパブリックリポジトリなら実行時間が無料なので、そちらに出しています。

**なぜ Pages を10分おきに再デプロイしないのか。** Pages 無料プランのビルドは **月500回** まで。10分おきだと月4,320回で足りません。静的HTMLは変更時だけデプロイし、頻繁に変わるデータだけ R2 から配信します。R2 は書き込み（Class A）月100万回・ストレージ10GBまで無料、**エグレスは完全無料**なので、この使い方なら課金されません。

**サイトは R2 が読めなくても壊れません。** HTMLには最後に巡回したデータが埋め込まれており、起動後に R2 を取りに行って取れたら差し替える2段構えです。R2が落ちても、埋め込み済みのデータで表示が続きます。

---

## 1. ドメインを取る

**Cloudflare Registrar は .jp を扱っていません**（公式のTLD一覧に .jp / .co.jp の記載なし）。日本のレジストラで取って、ネームサーバーだけCloudflareに向けます。

**ムームードメインが最安**（2026年7月29日時点・税込）:

| | 初年度 | 更新 |
|---|---|---|
| .jp | 990円 | 3,344円/年 |

お名前.comは更新が7,568円/年と高く、長期運用には向きません。

**`kumamotojishin.jp`（ハイフンなし）**を使う前提で全ファイルを設定済みです。変える場合は `grep -rln kumamotojishin . | xargs sed -i '' 's/kumamotojishin/新しい名前/g'` で一括置換できます。

ハイフンなしを選んだのは口頭伝達のためです。避難所の呼びかけや電話の伝言で「ハイフン」という一語が入るたびに伝わらなくなります。

なお「じしん」のローマ字は `jishin` / `zishin` / `jisin` / `zisin` の4通りに割れます。訓令式で覚えた人は `zisin` 側に流れるため、**URLは口頭ではなくQRコードで配るのが確実**です。予算に余裕があれば `kumamotojishin.com` も押さえて301転送しておくと、偽の募金ページを置かれる余地を潰せます（Cloudflare の Redirect Rules で無料・10本まで）。

取得したら必ず:

- **自動更新をON**にする
- **登録メールアドレスを個人のGmailにしない**（担当者が変わると連絡が届かず失効します）
- **カードの有効期限切れに注意**。ドメイン失効の最大の原因です

> 災害情報サイトのドメインは、役目を終えたあとも手放さないでください。自治体のキャンペーンサイトが失効後に第三者に取得され、オンラインカジノや公序良俗に反するサイトに転用された事例が日本国内で何件も起きています（和歌山県のGoToEatサイト、神戸市須磨海浜水族園など）。災害情報サイトは信頼と被リンクが急速に溜まるぶん、標的としての価値も高くなります。年3,344円は保険料として安いです。

## 2. Cloudflare にドメインを追加

1. Cloudflare ダッシュボード → **Add a site** → `kumamotojishin.jp` → **Free** プラン
2. 表示された2つのネームサーバー（`xxx.ns.cloudflare.com`）を控える
3. ムームードメインの管理画面 → ドメイン操作 → **ネームサーバ設定変更** → 「GMOペパボ以外のネームサーバを使用する」→ 控えたNSを入力
4. 反映まで数分〜数時間。Cloudflare側が「Active」になれば完了

## 3. GitHubリポジトリを作る

```bash
cd ~/Downloads/kumamoto-support
git add -A
git commit -m "サイトと巡回クローラーを追加"
gh repo create kumamotojishin --public --source=. --push
```

**パブリックにしてください。** プライベートだとGitHub Actionsの無料分（月2,000分）を10分おきの実行で使い切り、超過分が月$14ほどかかります。パブリックなら無料です。災害情報サイトはソースが公開されているほうが信頼されます。

> パブリックリポジトリのスケジュール実行は、60日間まったく活動がないと自動停止します。運用中は問題になりませんが、収束後に放置すると止まります。

## 4. Cloudflare の APIトークンを作る

ダッシュボード → 右上のアイコン → **My Profile** → **API Tokens** → **Create Token** → **Create Custom Token**

権限は次の3つだけ:

| 種別 | 対象 | 権限 |
|---|---|---|
| Account | Workers R2 Storage | Edit |
| Account | Cloudflare Pages | Edit |
| Account | Account Settings | Read |

**Account Resources** で自分のアカウントを指定。作成後に表示されるトークンは一度しか見られないのでコピーしておきます。

Account ID はダッシュボードの右サイドバー、またはURLの `dash.cloudflare.com/<ここ>` の部分です。

GitHubリポジトリ → Settings → Secrets and variables → Actions → **New repository secret**:

- `CLOUDFLARE_API_TOKEN` … 上で作ったトークン
- `CLOUDFLARE_ACCOUNT_ID` … アカウントID

## 5. R2 バケットを作る

```bash
npx wrangler login
npx wrangler r2 bucket create kumamotojishin-data
```

ダッシュボード → **R2** → `kumamotojishin-data` → **Settings** → **Public access**:

1. **Custom Domains** → **Connect Domain** → `data.kumamotojishin.jp` を入力
2. DNSレコードは自動で作られます

> `r2.dev` の公開URLはレート制限があり本番向きではありません。必ずカスタムドメインを使ってください。

**CORSの設定**（サイトからJSONを取りに行くために必要）。R2バケットの Settings → **CORS Policy**:

```json
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
```

## 6. サイトを公開する

```bash
# まず手元で巡回して、最新データを埋め込む
python3 crawl.py --inject site/index.html

# Pages プロジェクトを作って公開
npx wrangler pages project create kumamotojishin --production-branch main
npx wrangler pages deploy site --project-name kumamotojishin
```

`https://kumamotojishin.pages.dev` が出るので開いて確認します。

独自ドメインを当てる: ダッシュボード → **Workers & Pages** → `kumamotojishin` → **Custom domains** → **Set up a domain** → `kumamotojishin.jp`。`www` も付けるなら同じ手順でもう一度。

## 7. 巡回を動かす

```bash
# 初回は手動で実行して動作確認
gh workflow run crawl.yml
gh run watch
```

Actions のログに「巡回完了 ／ ソース 16/16 成功」と出れば成功です。以降は10分おきに自動で回ります。

`https://data.kumamotojishin.jp/data.json` を開いて中身が見えれば配信もできています。

> GitHub Actions のスケジュール実行は、混雑時に数分〜十数分遅れます。分単位の速報性が必要なら、あなたのMacで launchd を回す方法（下記）に切り替えてください。

---

## Macで直接回す場合（GitHubを使わないとき）

`~/Library/LaunchAgents/jp.kumamotojishin.crawl.plist`:

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>jp.kumamotojishin.crawl</string>
  <key>ProgramArguments</key>
  <array>
    <string>/bin/bash</string>
    <string>-lc</string>
    <string>cd ~/Downloads/kumamoto-support &amp;&amp; /usr/bin/python3 crawl.py &amp;&amp; npx wrangler r2 object put kumamotojishin-data/data.json --file out/data.json --content-type "application/json; charset=utf-8" --remote</string>
  </array>
  <key>StartInterval</key><integer>600</integer>
  <key>RunAtLoad</key><true/>
  <key>StandardOutPath</key><string>/tmp/kumamoto-crawl.log</string>
  <key>StandardErrorPath</key><string>/tmp/kumamoto-crawl.err</string>
</dict>
</plist>
```

```bash
launchctl load ~/Library/LaunchAgents/jp.kumamotojishin.crawl.plist
```

Macがスリープすると止まります。本番運用には向きません。動作確認用と考えてください。

---

## かかるお金

| 項目 | 費用 |
|---|---|
| Cloudflare Pages（静的配信・帯域無制限） | $0 |
| Cloudflare R2（JSON配信・エグレス無料） | $0 |
| GitHub Actions（パブリックリポジトリ） | $0 |
| ドメイン .jp | 初年度990円／更新3,344円 |
| **合計** | **年3,344円（初年度990円）** |

課金が始まる境界:

- R2 の Class B（読み取り）が月1,000万回超 → 超過分 $0.36/100万。`Cache-Control: max-age=60` を付けているのでエッジで大半が吸収されます。1日50万PVでも月1,500万リクエスト、うちエッジヒットを除けば無料枠に収まる想定です
- R2 ストレージ 10GB超 → data.json は200KB程度なので無関係
- GitHub Actions をプライベートリポジトリにした場合 → 月$14ほど

---

## 公開前に必ず

- [ ] **運営主体・連絡先・利用規約・プライバシーポリシー**をサイトに追記する。匿名運営の災害情報サイトは信用されません
- [ ] `crawl.py` の `CONTACT` を自分の連絡先に書き換える。巡回先の管理者が問題に気づいたときの連絡先です
- [ ] **サイトの終了日を決めて掲出する**。「〇月〇日で更新を終了し、熊本県のポータルへ引き継ぎます」。閉じ方を決めていないサイトは必ず放置されます
- [ ] 各自治体の防災担当に「こういうサイトを出しました」と一報を入れる
- [ ] 気象庁データの**出典表記**（サイトに実装済み）を消さない
