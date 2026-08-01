#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
令和8年熊本地震 情報まとめサイト — 一次情報巡回クローラー

公式サイトのRSS / JSON / Atomフィードを定期巡回し、更新を検知して data.json に出力する。
Web検索は一切使わず、発信元の一次情報のみを取得する。

使い方:
    python3 crawl.py                        # 巡回して out/data.json を更新
    python3 crawl.py --inject index.html    # HTMLにデータを埋め込んで単一ファイル化
    python3 crawl.py --once --verbose       # 1回だけ詳細ログ付きで実行

cron 例（10分おき）:
    */10 * * * * cd /path/to/site && /usr/bin/python3 crawl.py --inject index.html >> crawl.log 2>&1

設計方針:
  - ETag / Last-Modified による条件付きGET。304が返ればボディを取得しない
    （気象庁は1日10GB超のダウンロードでIP遮断されるため、これは必須）
  - リクエスト間に必ずインターバルを置く
  - 1つのソースが落ちても全体は止めない。errors に記録して続行
  - 取得元・取得時刻を必ず保持し、サイト側で「いつの情報か」を表示できるようにする

出典表記の義務:
  気象庁のデータを使う場合、サイト上に「出典：気象庁ホームページ」の表示が必要。
  警報・予報の本質を変える編集は気象業務法で禁止されている。震度やマグニチュードの
  表示整形は問題ないが、内容の書き換えは絶対に行わないこと。
"""

import json
import os
import re
import sys
import time
import gzip
import hashlib
import argparse
import datetime as dt
import urllib.request
import urllib.error
import xml.etree.ElementTree as ET
import html as html_mod

# ----------------------------------------------------------------------------
# 設定
# ----------------------------------------------------------------------------

# 連絡先を必ず自分のものに書き換えてください。
# 迷惑をかけた際に相手から連絡が来る余地を残すのが礼儀であり、遮断の回避にもなります。
CONTACT = "https://kumamotojishin.jp/ (info@kumamotojishin.jp)"
UA = f"KumamotoQuakeInfoBot/1.0 (+{CONTACT})"

REQUEST_INTERVAL = 3.0      # 各リクエストの間隔（秒）
TIMEOUT = 20
PDF_TIMEOUT = 30            # 断水PDFのみ大きいので長め（それでも上限は設ける）
TIME_BUDGET = 300           # 巡回全体の上限（秒）。超えたら残りを諦めて、取れた分だけ出力する。
                            # 1件の応答待ちが積み上がって全体が落ちるのを防ぐための安全装置。
MAX_ITEMS_PER_SOURCE = 30
KEEP_UPDATES = 200          # data.json に残す更新件数の上限
KEEP_PER_SOURCE = 10        # 各ソースから最低限確保する件数（多弁なソースに埋もれさせない）
KEEP_QUAKES = 40

# 有感地震の回数を時間ごとに積み上げる。気象庁の一覧は直近しか持たないため、
# 巡回のたびに新しい地震だけを足していく。前回の集計は out/data.json から引き継ぐ。
QUAKE_HIST_FROM = "2026-07-28"      # この日より前の地震は数えない（今回の地震活動の開始日）
QUAKE_HOURS_KEEP = 24 * 21          # 3週間分だけ保持する

# 震源の位置。気象庁のJSONは直近しか持たず、過去分をたどる手段が公開されていないため、
# 気象庁の発表を蓄積している P2P地震情報 のJSON APIから取る（商用・非商用問わず無償で、
# 二次利用可。https://www.p2pquake.net/develop/json_api_v2/ ）。
# 無償提供でサーバー増強が難しいと明言されているので、取得は1回の巡回で1ページだけにし、
# 7/28まで遡れていないときだけページをたどる。閲覧者のブラウザからは叩かず、
# こちらが集めたものを data.json 経由で配る。
P2P_URL = "https://api.p2pquake.net/v2/history?codes=551&limit=100&offset={offset}"
P2P_PAGES_BACKFILL = 6
HYPO_BBOX = (30.8, 34.2, 129.2, 132.2)   # 南限, 北限, 西限, 東限（九州中部）
HYPO_KEEP = 3000

# 都市ガスの供給停止戸数は、内閣府「令和8年熊本地震に係る被害状況等について」から取る。
# 経済産業省の情報が本文に文章で書かれており、日ごとに更新される。PDFに文字情報が入って
# いるので機械で読める（消防庁の被害報は画像のみで読めない）。
CAO_INDEX = "https://www.bousai.go.jp/updates/r8kumamoto_jishin/index.html"
CAO_PDF = "https://www.bousai.go.jp/updates/r8kumamoto_jishin/pdf/r8kumamoto_jishin_%s.pdf"
# パーサーを直したら必ず増やすこと。前回の結果をそのまま使い回して、
# 直したはずの誤りが残り続けるのを防ぐための番号。
CAO_PARSER = 2

HERE = os.path.dirname(os.path.abspath(__file__)) or "."
OUT_DIR = os.path.join(HERE, "out")
STATE_PATH = os.path.join(HERE, "state.json")
DATA_PATH = os.path.join(OUT_DIR, "data.json")

JST = dt.timezone(dt.timedelta(hours=9))

# 絞り込みキーワード。
#   disaster … 災害そのものに関する語。自治体フィードはこれだけで絞る
#              （自治体名を含めると、その自治体の記事が全部通ってしまうため）
#   area     … 被災地を指す語。全国向けフィードは disaster と area の両方を要求する
DISASTER_KW = re.compile(
    r"地震|震度|余震|津波|断水|給水|避難|罹災|り災|被災|災害|停電|通行止|全面通行止|"
    r"ライフライン|倒壊|ボランティア|義援|支援金|支援物資|応急|仮設|災害ごみ|災害廃棄物|"
    r"休館|休園|休校|運休|中止|閉館|見舞|安否|デマ|偽情報"
)
AREA_KW = re.compile(
    r"熊本|八代|宇城|宇土|氷川|益城|美里|嘉島|甲佐|山都|宇土半島|日奈久|不知火|松橋|九州"
)

# ----------------------------------------------------------------------------
# 巡回対象（すべて発信元の一次情報。2026-07-29 に実在とレスポンス形式を確認済み）
# ----------------------------------------------------------------------------

SOURCES = [
    # --- 地震そのものの情報（気象庁） -------------------------------------
    {
        "id": "jma_quake_list",
        "label": "気象庁 地震情報",
        "kind": "jma_quake_json",
        "url": "https://www.jma.go.jp/bosai/quake/data/list.json",
        "group": "quake",
        "note": "気象庁ホームページが内部で使用しているJSON。公式APIとして案内されているものではないため、"
                "スキーマ変更に備えて eqvol.xml も併用する。",
    },
    {
        "id": "jma_eqvol_feed",
        "label": "気象庁 防災情報XML（地震火山・高頻度）",
        "kind": "atom",
        "url": "https://www.data.jma.go.jp/developer/xml/feed/eqvol.xml",
        "group": "official_feed",
        "note": "気象庁が公式に公開しているPULL型フィード。こちらが正。毎分更新。",
    },

    # --- 熊本県 -----------------------------------------------------------
    {
        "id": "pref_kumamoto_urgent",
        "label": "熊本県 緊急・重要なお知らせ",
        "kind": "rss",
        "url": "https://www.pref.kumamoto.jp/rss/10/list3.xml",
        "group": "pref",
        "area": "熊本県",
    },
    {
        "id": "pref_kumamoto_new",
        "label": "熊本県 新着情報",
        "kind": "rss",
        "url": "https://www.pref.kumamoto.jp/rss/10/list1.xml",
        "group": "pref",
        "area": "熊本県",
        "filter": "disaster",
    },

    # --- 市町村 -----------------------------------------------------------
    {
        "id": "kumamoto_city",
        "label": "熊本市",
        "kind": "rss",
        "url": "https://www.city.kumamoto.jp/new_list.xml",
        "group": "city",
        "area": "熊本市",
        "filter": "disaster",
    },
    {
        "id": "yatsushiro",
        "label": "八代市",
        "kind": "rss",
        "url": "https://www.city.yatsushiro.lg.jp/new_list.xml",
        "group": "city",
        "area": "八代市",
        "filter": "disaster",
    },
    {
        "id": "uto",
        "label": "宇土市",
        "kind": "rss",
        "url": "https://www.city.uto.lg.jp/rss/nrss.xml",
        "group": "city",
        "area": "宇土市",
        "filter": "disaster",
    },
    {
        "id": "hikawa",
        "label": "氷川町",
        "kind": "rss",
        "url": "https://www.town.hikawa.kumamoto.jp/new_list.xml",
        "group": "city",
        "area": "氷川町",
        "filter": "disaster",
    },
    {
        "id": "mashiki",
        "label": "益城町",
        "kind": "rss",
        "url": "https://www.town.mashiki.lg.jp/new_list.xml",
        "group": "city",
        "area": "益城町",
        "filter": "disaster",
    },
    {
        "id": "misato",
        "label": "美里町",
        "kind": "misato_json",
        "url": "https://www.town.kumamoto-misato.lg.jp/index.update.json",
        "group": "city",
        "area": "美里町",
        "filter": "disaster",
        "note": "新着一覧がJavaScriptで描画されるため、描画元のJSONを直接読む。",
    },
    {
        "id": "hikawa_kinkyu",
        "label": "氷川町 緊急情報",
        "kind": "kinkyu_html",
        "url": "https://www.town.hikawa.kumamoto.jp/kinkyu.html",
        "group": "city",
        "area": "氷川町",
        "note": "氷川町は地震情報を新着RSSに載せず、緊急情報ページにのみ掲載する。"
                "町の情報源はここ一本なので必ず巡回する。",
    },
    {
        "id": "mashiki_kinkyu",
        "label": "益城町 緊急情報",
        "kind": "kinkyu_html",
        "url": "https://www.town.mashiki.lg.jp/kinkyu.html",
        "group": "city",
        "area": "益城町",
        "note": "氷川町と同じCMS。緊急情報は時系列で1ページに積まれる。",
    },
    {
        "id": "uki",
        "label": "宇城市",
        "kind": "uki_html",
        "url": "https://www.city.uki.kumamoto.jp/toppage/kinkyu",
        "group": "city",
        "area": "宇城市",
        "note": "宇城市だけRSS・ETag・Last-Modifiedのいずれも提供がないため、HTMLから抽出して"
                "本文ハッシュで差分を見る。WAF配下なので巡回間隔は長めに。",
    },

    # --- 国 ---------------------------------------------------------------
    {
        "id": "fdma_disaster",
        "label": "総務省消防庁 災害情報",
        "kind": "rss",
        "url": "https://www.fdma.go.jp/disaster/info/index.xml",
        "group": "gov",
        "base": "https://www.fdma.go.jp",
        "note": "消防庁が公式に案内しているRSS。link が相対パスなのでドメインを補う。",
        "filter": "local",
    },
    {
        "id": "cao_bousai",
        "label": "内閣府 防災情報のページ 新着",
        "kind": "rss",
        "url": "https://www.bousai.go.jp/news.xml",
        "group": "gov",
        "filter": "local",
        "note": "pubDate がRFC822に準拠していない（月名がJuly、TZが+1700など）ため日付は緩くパースする。",
    },

    {
        "id": "kyuden_power",
        "label": "九州電力送配電 停電情報",
        "kind": "kyuden_power",
        "url": "https://www.kyuden.co.jp/td_teiden/xml/00.xml",
        "group": "stat",
        "note": "停電情報ページが内部で読んでいるXML。県別の停電戸数と復旧見込みが入っている。"
                "熊本県の地方別内訳は c43.xml 側にあるので、必要なときだけ追加で取得する。"
                "公式APIとして案内されているものではないため、形式変更に備えて失敗しても止まらないようにする。",
    },
    {
        "id": "mlit_water",
        "label": "国土交通省 被害状況（断水）",
        "kind": "mlit_water",
        "url": "https://www.mlit.go.jp/saigai/saigai_260728.html",
        "group": "stat",
        "note": "断水戸数はPDFでしか公表されないため、最新報のPDFを取得して数値を抜き出す。"
                "pdfplumber が入っていない環境では前回値を保持してスキップする。",
    },

    # --- 報道（一次情報ではないので区別して扱う） ---------------------------
    {
        "id": "kvc_kumamoto",
        "label": "熊本県災害ボランティアセンター",
        "kind": "kvc_html",
        "url": "https://www.fukushi-kumamoto.or.jp/kvc/",
        "group": "pref",
        "area": "熊本県",
        "note": "県社協が運営する災害ボランティア情報サイト。市町村の災害VCがいつ立ち上がるかは"
                "被災者にも支援したい人にも影響が大きい。RSSが無いためHTMLを読む。",
    },
    {
        "id": "nhk_shakai",
        "label": "NHKニュース（社会）",
        "kind": "rss",
        "url": "https://www3.nhk.or.jp/rss/news/cat1.xml",
        "group": "news",
        "filter": "local",
        "note": "NHKに災害専用カテゴリは存在しないため、社会カテゴリをキーワードで絞り込む。",
    },
]


# ----------------------------------------------------------------------------
# HTTP
# ----------------------------------------------------------------------------

def load_state():
    if os.path.exists(STATE_PATH):
        try:
            with open(STATE_PATH, encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return {}


def save_state(state):
    with open(STATE_PATH, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=1)


def fetch(url, state_entry, verbose=False):
    """条件付きGET。(status, body_bytes, new_state_entry) を返す。304なら body は None。"""
    req = urllib.request.Request(url, headers={
        "User-Agent": UA,
        "Accept-Encoding": "gzip",
        "Accept": "*/*",
    })
    if state_entry.get("etag"):
        req.add_header("If-None-Match", state_entry["etag"])
    if state_entry.get("last_modified"):
        req.add_header("If-Modified-Since", state_entry["last_modified"])

    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as res:
            raw = res.read()
            if res.headers.get("Content-Encoding") == "gzip":
                raw = gzip.decompress(raw)
            new_entry = dict(state_entry)
            if res.headers.get("ETag"):
                new_entry["etag"] = res.headers["ETag"]
            if res.headers.get("Last-Modified"):
                new_entry["last_modified"] = res.headers["Last-Modified"]
            return res.status, raw, new_entry
    except urllib.error.HTTPError as e:
        if e.code == 304:
            if verbose:
                print(f"    304 変更なし")
            return 304, None, state_entry
        raise


def decode(raw):
    """文字コードを推定してデコード。自治体サイトにShift_JISが残っていることがある。"""
    head = raw[:200].decode("ascii", errors="ignore").lower()
    m = re.search(r'encoding=["\']([\w-]+)["\']', head)
    enc = m.group(1) if m else None
    for cand in [enc, "utf-8", "cp932", "euc-jp"]:
        if not cand:
            continue
        try:
            return raw.decode(cand)
        except (UnicodeDecodeError, LookupError):
            continue
    return raw.decode("utf-8", errors="replace")


# ----------------------------------------------------------------------------
# パーサ
# ----------------------------------------------------------------------------

NS = {
    "atom": "http://www.w3.org/2005/Atom",
    "rss1": "http://purl.org/rss/1.0/",
    "dc": "http://purl.org/dc/elements/1.1/",
    "dcterms": "http://purl.org/dc/terms/",
    "rdf": "http://www.w3.org/1999/02/22-rdf-syntax-ns#",
}

MONTHS = {m: i + 1 for i, m in enumerate(
    ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"])}


def parse_date_loose(s):
    """RFC822 / ISO8601 / 和暦混じり を緩くパースして ISO文字列(JST)を返す。失敗したら None。"""
    if not s:
        return None
    s = s.strip()

    # ISO 8601
    m = re.search(r"(\d{4})-(\d{2})-(\d{2})[T ](\d{2}):(\d{2})(?::(\d{2}))?"
                  r"(Z|[+-]\d{2}:?\d{2})?", s)
    if m:
        y, mo, d, h, mi = (int(m.group(i)) for i in range(1, 6))
        sec = int(m.group(6) or 0)
        tzs = m.group(7)
        base = dt.datetime(y, mo, d, h, mi, sec)
        if tzs in (None, "", "Z"):
            tz = dt.timezone.utc if tzs == "Z" else JST
        else:
            sign = 1 if tzs[0] == "+" else -1
            tzs = tzs[1:].replace(":", "")
            off = int(tzs[:2]) * 60 + int(tzs[2:4])
            # +1700 のような明らかな誤記は +0900 とみなす（内閣府のフィード対策）
            if off > 14 * 60:
                off = 9 * 60
                sign = 1
            tz = dt.timezone(dt.timedelta(minutes=sign * off))
        return base.replace(tzinfo=tz).astimezone(JST).isoformat()

    # 日付のみ（2026-07-30 / 2026/7/30）。時刻が無いCMSはJSTの0時として扱う
    m = re.fullmatch(r"(\d{4})[-/](\d{1,2})[-/](\d{1,2})", s)
    if m:
        y, mo, d = (int(m.group(i)) for i in range(1, 4))
        return dt.datetime(y, mo, d, tzinfo=JST).isoformat()

    # 日本語表記
    m = re.search(r"(\d{4})年\s*(\d{1,2})月\s*(\d{1,2})日", s)
    if m:
        y, mo, d = (int(m.group(i)) for i in range(1, 4))
        hm = re.search(r"(\d{1,2})時\s*(\d{1,2})分", s)
        h, mi = (int(hm.group(1)), int(hm.group(2))) if hm else (0, 0)
        return dt.datetime(y, mo, d, h, mi, tzinfo=JST).isoformat()

    # RFC822（壊れているものも含む）
    m = re.search(r"(\d{1,2})\s+([A-Za-z]{3,9})\s+(\d{4})(?:\s+(\d{2}):(\d{2})(?::(\d{2}))?)?", s)
    if m:
        d = int(m.group(1))
        mo = MONTHS.get(m.group(2)[:3].lower())
        y = int(m.group(3))
        h = int(m.group(4) or 0)
        mi = int(m.group(5) or 0)
        if mo:
            return dt.datetime(y, mo, d, h, mi, tzinfo=JST).isoformat()
    return None


def text_of(el):
    return "".join(el.itertext()).strip() if el is not None else ""


def parse_rss(xml_text, src):
    """RSS 1.0 (RDF) / RSS 2.0 / Atom をまとめて処理する。"""
    items = []
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError:
        # 実体参照の不正など。壊れた箇所を落として再挑戦する
        cleaned = re.sub(r"&(?!amp;|lt;|gt;|quot;|apos;|#)", "&amp;", xml_text)
        root = ET.fromstring(cleaned)

    base = src.get("base", "")

    def add(title, link, date_s, desc=""):
        title = (title or "").strip()
        if not title:
            return
        if link and link.startswith("/"):
            link = base + link
        items.append({
            "title": title,
            "url": link or "",
            "published": parse_date_loose(date_s),
            "desc": (desc or "").strip()[:160],
        })

    # RSS 2.0（名前空間なし）と RSS 1.0/RDF（既定名前空間が purl.org/rss/1.0）の両方
    item_els = root.findall(".//item") + root.findall(".//rss1:item", NS)
    for it in item_els:
        def pick(*paths):
            for p in paths:
                el = it.find(p, NS) if ":" in p else it.find(p)
                if el is not None:
                    return el
            return None
        t = pick("title", "rss1:title")
        l = pick("link", "rss1:link")
        d = pick("pubDate", "dc:date", "dcterms:date")
        de = pick("description", "rss1:description")
        add(text_of(t), text_of(l), text_of(d), text_of(de))

    # Atom
    if not items:
        for e in root.findall("atom:entry", NS):
            link_el = e.find("atom:link", NS)
            href = link_el.get("href") if link_el is not None else ""
            add(text_of(e.find("atom:title", NS)), href,
                text_of(e.find("atom:updated", NS)),
                text_of(e.find("atom:content", NS)))

    return items[:MAX_ITEMS_PER_SOURCE]


def parse_misato_json(body, src):
    data = json.loads(body)
    rows = data if isinstance(data, list) else data.get("pages") or data.get("data") or []
    items = []
    for r in rows:
        if not isinstance(r, dict):
            continue
        title = r.get("page_name") or r.get("title") or ""
        url = r.get("url") or ""
        if url.startswith("/"):
            url = "https://www.town.kumamoto-misato.lg.jp" + url
        elif url.startswith("//"):
            url = "https:" + url
        items.append({
            "title": title.strip(),
            "url": url,
            "published": parse_date_loose(r.get("publish_datetime") or r.get("update_datetime") or ""),
            "desc": "",
        })
    items = [i for i in items if i["title"]]
    items.sort(key=lambda x: x["published"] or "", reverse=True)
    return items[:MAX_ITEMS_PER_SOURCE]


def parse_uki_html(html, src):
    """宇城市：RSSもETagもないためHTMLから記事リンクと更新日を抜く。"""
    items = []
    # 属性値が引用符なしで出力されるCMSなので [\"']? を挟んでおく
    #   <a href="/toppage/kinkyu/2606699"><p class=art-text>
    #     <span class=art-date>2026年7月29日</span><span class=art-title>…</span></p></a>
    pat = re.compile(
        r'<a[^>]+href=["\']?(/[^"\'\s>]+)["\']?[^>]*>.*?'
        r'class=["\']?(?:br-)?art-date["\']?[^>]*>([^<]*)<.*?'
        r'class=["\']?(?:br-)?art-title["\']?[^>]*>([^<]*)<',
        re.S)
    for m in pat.finditer(html):
        items.append({"title": m.group(3).strip(),
                      "url": "https://www.city.uki.kumamoto.jp" + m.group(1),
                      "published": parse_date_loose(m.group(2)), "desc": ""})
    if not items:
        # 構造が変わった場合のフォールバック：緊急情報配下のリンクだけでも拾う
        for m in re.finditer(
            r'<a[^>]+href=["\']?(/toppage/kinkyu/\d+)["\']?[^>]*>(.{4,120}?)</a>', html, re.S
        ):
            title = re.sub(r"<[^>]+>", " ", m.group(2))
            title = re.sub(r"\s+", " ", title).strip()
            if title:
                items.append({"title": title,
                              "url": "https://www.city.uki.kumamoto.jp" + m.group(1),
                              "published": None, "desc": ""})
    # 重複除去
    seen, uniq = set(), []
    for i in items:
        if i["url"] in seen:
            continue
        seen.add(i["url"])
        uniq.append(i)
    return uniq[:MAX_ITEMS_PER_SOURCE]


def parse_kinkyu_html(html, src):
    """氷川町・益城町の緊急情報ページ。1ページに見出しと本文が時系列で積まれる同一CMS。

      <h2 class="kinkyuTitle"><span id="kid51">…見出し…</span></h2>
      <div class="updDate"><time datetime="2026-07-29T09:26:13+09:00">…</time></div>
      <div class="kinkyuNaiyo">…本文…</div>
    """
    items = []
    base = src["url"]
    blocks = re.split(r'<h2[^>]*class=["\']?kinkyuTitle', html)[1:]
    for b in blocks:
        am = re.search(r'id=["\']?(kid\d+)', b)
        anchor = f"#{am.group(1)}" if am else ""
        tm = re.search(r'>([^<]{2,200}?)</span>', b)
        if not tm:
            tm = re.search(r'>([^<]{2,200}?)</h2>', b)
        if not tm:
            continue
        title = re.sub(r"\s+", " ", tm.group(1)).strip()
        # このCMSは datetime 属性に記事の初回公開時刻を入れたまま、表示テキストだけを
        # 更新時刻に差し替える。実際の更新時刻は表示テキスト側なので、そちらを優先する。
        dm = re.search(r'<time[^>]*>([^<]+)</time>', b)
        if not (dm and parse_date_loose(dm.group(1))):
            dm = re.search(r'<time[^>]+datetime=["\']([^"\']+)["\']', b)
        body = ""
        bm = re.search(r'class=["\']?kinkyuNaiyo["\']?[^>]*>(.{0,600}?)</div>', b, re.S)
        if bm:
            body = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", bm.group(1))).strip()
        items.append({
            "title": title,
            "url": base + anchor,
            "published": parse_date_loose(dm.group(1)) if dm else None,
            "desc": body[:160],
        })
    seen, uniq = set(), []
    for i in items:
        k = i["url"] + i["title"]
        if k in seen:
            continue
        seen.add(k)
        uniq.append(i)
    uniq.sort(key=lambda x: x["published"] or "", reverse=True)
    return uniq[:MAX_ITEMS_PER_SOURCE]


def parse_kvc_html(html, src):
    """熊本県社会福祉協議会（県災害ボランティアセンター）のお知らせ一覧。

    RSSが無いCMSなので、1件ぶんのブロックを id="block<ブロックID>-<記事ID>" で切って読む。

      <div id="block4334-2211" ... data-record-id="2211">
        <div class="... date" data-switch="date">2026-07-30</div>
        <a href="/pages/291/detail=1/b_id=4334/r_id=2211#...">
          <span class="title ..." data-switch="title">市町村災害ボランティアセンターについて</span>

    市町村の災害ボランティアセンターがいつ立ち上がるかは、被災者にも支援したい人にも
    影響が大きい。ここが更新された瞬間に拾えるようにしておく。
    """
    items = []
    blocks = re.split(r'(?=<div\s+id="block\d+-\d+")', html)
    for b in blocks:
        tm = re.search(r'data-switch="title"[^>]*>([^<]{2,200})<', b)
        dm = re.search(r'data-switch="date"[^>]*>.*?(\d{4}-\d{2}-\d{2})', b, re.S)
        hm = re.search(r'href="(/pages/\d+/detail=1/b_id=\d+/r_id=\d+)', b)
        # 日付と記事リンクの両方があるものだけを記事とみなす。
        # 同じCMSでよくある質問（Q&A）も data-switch="title" を持つので、これで弾く。
        if not (tm and dm and hm):
            continue
        title = re.sub(r"\s+", " ", tm.group(1)).strip()
        items.append({
            "title": title,
            "url": "https://www.fukushi-kumamoto.or.jp" + hm.group(1),
            "published": parse_date_loose(dm.group(1)),
            "desc": "",
        })
    seen, uniq = set(), []
    for i in items:
        if i["url"] in seen:
            continue
        seen.add(i["url"])
        uniq.append(i)
    uniq.sort(key=lambda x: x["published"] or "", reverse=True)
    return uniq[:MAX_ITEMS_PER_SOURCE]


KYUDEN_REGION_URL = "https://www.kyuden.co.jp/td_teiden/xml/c43.xml"


def parse_kyuden_power(xml_text, src, verbose=False):
    """九州電力送配電の停電情報XMLから熊本県の停電戸数を取り出す。

      <DATA><PREF_NAME>熊本県</PREF_NAME><BLACKOUT_COUNT>約22,870戸</BLACKOUT_COUNT>
            <RESTORATION>確認中</RESTORATION><PC_COMMENT>…</PC_COMMENT></DATA>

    戸数は「約22,870戸」「0戸」のような文字列で入っているので数値に直す。
    復旧見込みは県内で新たな停電が起きると「確認中」に戻る仕様なので、そのまま出す。
    """
    def to_int(t):
        m = re.search(r"([\d,]+)", t or "")
        return int(m.group(1).replace(",", "")) if m else None

    root = ET.fromstring(xml_text)
    out = {}

    rd = root.findtext("./HEADER/RELEASE_DATE") or ""
    m = re.fullmatch(r"(\d{4})(\d{2})(\d{2})(\d{2})(\d{2})(\d{2})", rd.strip())
    if m:
        y, mo, d, h, mi, sec = (int(x) for x in m.groups())
        out["as_of"] = dt.datetime(y, mo, d, h, mi, sec, tzinfo=JST).isoformat()

    for data in root.findall("./DATA"):
        if (data.findtext("PREF_NAME") or "").strip() != "熊本県":
            continue
        out["current"] = to_int(data.findtext("BLACKOUT_COUNT"))
        r = (data.findtext("RESTORATION") or "").strip()
        if r:
            out["restoration"] = r
        c = (data.findtext("PC_COMMENT") or "").strip()
        if c:
            out["comment"] = re.sub(r"\s+", " ", c)
        break

    if out.get("current") is None:
        raise ValueError("熊本県の停電戸数が見つかりません")

    # 地方別の内訳。取れなくても県の合計は出せるので、失敗しても握りつぶす。
    if out["current"] > 0:
        try:
            time.sleep(1.0)
            sub = ET.fromstring(decode(fetch_bytes(KYUDEN_REGION_URL)))
            regions = []
            for r in sub.findall(".//REGION"):
                n = to_int(r.findtext("BLACKOUT_COUNT"))
                nm = (r.findtext("REGION_NAME") or "").strip()
                if nm and n:
                    regions.append({"name": nm, "now": n})
            if regions:
                out["regions"] = sorted(regions, key=lambda x: -x["now"])
        except Exception as e:
            if verbose:
                print(f"    地方別内訳は取得できず（{type(e).__name__}）")

    out["source_url"] = "https://www.kyuden.co.jp/td_teiden/kyushu.html"
    return out


def parse_mlit_water(html, src, state_entry, verbose=False):
    """国土交通省の被害状況PDFから断水戸数を取り出す。

    断水戸数は国交省がPDFでしか公表しておらず、報が更新されるたびにPDFのURLが変わる。
    そこで一覧ページのリンク文字列から最新の報を選び、そのPDFだけを取得して数値を読む。

      <a href="/common/002014179.pdf">令和８年熊本地震による被害状況等について（第5報）2026年7月29日 14:00時点</a>

    PDFの解析には pdfplumber が必要。入っていない環境では何もせず、
    呼び出し側が前回値を保持する（サイトの表示が空になるのを防ぐ）。
    """
    try:
        import pdfplumber  # noqa: F401
    except ImportError:
        if verbose:
            print("    pdfplumber 未導入のためスキップ（前回値を保持）")
        return None

    reports = []
    for m in re.finditer(
        r'<a[^>]+href="(/common/(\d+)\.pdf)"[^>]*>(.{0,160}?)</a>', html, re.S
    ):
        label = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", "", m.group(3))).strip()
        rm = re.search(r"被害状況等について\s*（\s*第\s*(\d+)\s*報\s*）", label)
        if rm:
            reports.append((int(rm.group(1)), "https://www.mlit.go.jp" + m.group(1), label))
    if not reports:
        raise ValueError("被害状況の報PDFが一覧に見つかりません")

    no, url, label = max(reports, key=lambda x: x[0])

    # 同じ報を何度も落とさない（気象庁と同様、無駄な再取得を避ける）
    if state_entry.get("water_pdf_url") == url and state_entry.get("stat"):
        if verbose:
            print(f"    第{no}報は取得済み")
        return state_entry["stat"]

    pdf_bytes = fetch_bytes(url)
    stat = extract_water_from_pdf(pdf_bytes)
    if not stat.get("current") and not stat.get("peak"):
        raise ValueError(f"第{no}報のPDFから断水戸数を抽出できませんでした")

    stat["report_no"] = no
    stat["source_url"] = url
    stat["source_page"] = src["url"]
    stat["source_label"] = label
    return stat


def fetch_bytes(url):
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "*/*"})
    with urllib.request.urlopen(req, timeout=PDF_TIMEOUT) as res:
        return res.read()


def extract_water_from_pdf(pdf_bytes):
    """PDFの「■水道」セクションから断水戸数を読む。

    表の行は空白が列区切りなので、空白の潰し方を要約文と表とで分けている。
    要約文では「約 84,000 戸」のように数字が分断されるため数字間の空白だけ詰める。
    """
    import io
    import pdfplumber

    txt = ""
    with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
        for page in pdf.pages:
            txt += (page.extract_text() or "") + "\n"

    rows = [re.sub(r"[ \t　]+", " ", l.strip()) for l in txt.split("\n")]
    joined = re.sub(r"(?<=[0-9])\s+(?=[0-9,])", "", " ".join(rows))

    out = {}
    m = re.search(r"■\s*水道\s*（\s*(\d{1,2})/(\d{1,2})\s*(\d{1,2}):(\d{2})\s*時点\s*）", joined)
    if m:
        out["as_of"] = f"{int(m.group(1))}月{int(m.group(2))}日 {m.group(3)}:{m.group(4)}"
    m = re.search(r"(\d+)\s*県\s*（\s*(\d+)\s*自治体\s*）\s*において\s*約?\s*([\d,]+)\s*戸が断水中", joined)
    if m:
        out["current"] = int(m.group(3).replace(",", ""))
        out["current_pref"], out["current_muni"] = int(m.group(1)), int(m.group(2))
    m = re.search(r"最大断水戸数\s*約?\s*([\d,]+)\s*戸", joined)
    if m:
        out["peak"] = int(m.group(1).replace(",", ""))
    m = re.search(r"(\d+)\s*県\s*（\s*(\d+)\s*自治体\s*）\s*において断水", joined)
    if m:
        out["peak_pref"], out["peak_muni"] = int(m.group(1)), int(m.group(2))

    start = next((i for i in range(len(rows)) if re.match(r"■\s*水道", rows[i])), None)
    if start is None:
        return out
    end = next((i for i in range(start + 1, len(rows)) if rows[i].startswith("■")), len(rows))
    # 表の見出し行を探す。「○…（最大断水戸数」という要約文にも同じ語が出るので箇条書きは除く。
    t0 = next((i for i in range(start, end)
               if "断水戸数" in rows[i] and not rows[i].startswith("○")), start)
    # 同じ水道セクション内に給水車の派遣台数の表があり、形が同じで列の意味が違うため手前で切る。
    t1 = next((i for i in range(t0 + 1, end) if rows[i].startswith("○")), end)

    muni, in_kumamoto = [], False
    for l in rows[t0:t1]:
        if re.fullmatch(r"【.{2,6}県】", l):
            in_kumamoto = ("熊本県" in l)
            continue
        if not in_kumamoto:
            continue
        mm = re.match(r"^([^\s0-9]{1,7}[市町村])\s+約?([\d,]+)\s+約?([\d,]+)(?:\s|$)", l)
        if mm:
            muni.append({"name": mm.group(1),
                         "max": int(mm.group(2).replace(",", "")),
                         "now": int(mm.group(3).replace(",", ""))})
    if muni:
        out["municipalities"] = sorted(muni, key=lambda x: -x["now"])
    return out


SCALE = {"1": "1", "2": "2", "3": "3", "4": "4",
         "5-": "5弱", "5+": "5強", "6-": "6弱", "6+": "6強", "7": "7"}


def parse_jma_quake(body, src):
    """気象庁の地震情報一覧。震源・震度情報と震度速報だけを拾い、同一地震はまとめる。"""
    rows = json.loads(body)
    out, seen = [], set()
    for r in rows:
        if not isinstance(r, dict):
            continue
        if r.get("ttl") not in ("震源・震度情報", "震度速報", "震源に関する情報"):
            continue
        eid = r.get("eid")
        if not eid or eid in seen:
            continue
        seen.add(eid)
        cod = r.get("cod") or ""
        depth = ""
        dm = re.search(r"-(\d+)/", cod)
        if dm:
            depth = f"{int(dm.group(1))//1000}km"
        out.append({
            "at": parse_date_loose(r.get("at") or r.get("rdt") or ""),
            "place": r.get("anm") or "",
            "mag": r.get("mag") or "",
            "maxi": SCALE.get(r.get("maxi") or "", r.get("maxi") or ""),
            "depth": depth,
            "title": r.get("ttl") or "",
            "eid": eid,
        })
    out.sort(key=lambda x: x["at"] or "", reverse=True)
    return out[:KEEP_QUAKES]


# ----------------------------------------------------------------------------
# メイン
# ----------------------------------------------------------------------------

def fetch_hypocenters(prev, verbose=False):
    """震源（緯度・経度・深さ・規模・最大震度）を集める。

    返すのは {"updated_at":..., "items":[[時刻, 緯度, 経度, 深さkm, M, 最大震度コード], ...]}。
    最大震度コードは P2P地震情報 の値そのまま（10=震度1、45=5弱、70=7、-1=不明）。
    同じ地震は「時刻＋緯度＋経度」で重複を除きます。
    """
    old = (prev.get("hypo") or {})
    items = {}
    for it in (old.get("items") or []):
        if isinstance(it, list) and len(it) >= 6:
            items[f"{it[0]}|{it[1]}|{it[2]}"] = it

    oldest = min((it[0] for it in items.values()), default="9999")
    need_backfill = oldest > "2026-07-28T16:27"
    pages = P2P_PAGES_BACKFILL if need_backfill else 1
    added, fetched = 0, 0

    for page in range(pages):
        url = P2P_URL.format(offset=page * 100)
        try:
            _st, raw, _ = fetch(url, {})
        except Exception as e:
            if verbose:
                print(f"    震源の取得に失敗: {e}")
            break
        if not raw:
            break
        fetched += 1
        try:
            rows = json.loads(raw.decode("utf-8", "replace"))
        except Exception:
            break
        if not rows:
            break

        page_oldest = "9999"
        for r in rows:
            eq = (r or {}).get("earthquake") or {}
            hy = eq.get("hypocenter") or {}
            t = str(eq.get("time") or "")            # "2026/07/30 21:12:00"
            m = re.match(r"(\d{4})/(\d{2})/(\d{2}) (\d{2}):(\d{2})", t)
            if not m:
                continue
            ts = f"{m.group(1)}-{m.group(2)}-{m.group(3)}T{m.group(4)}:{m.group(5)}"
            page_oldest = min(page_oldest, ts)
            if ts < "2026-07-28T00:00":
                continue
            try:
                lat, lon = float(hy.get("latitude")), float(hy.get("longitude"))
            except (TypeError, ValueError):
                continue
            if lat == 0 and lon == 0:
                continue
            s1, n1, w1, e1 = HYPO_BBOX
            if not (s1 <= lat <= n1 and w1 <= lon <= e1):
                continue
            try:
                mag = float(hy.get("magnitude"))
            except (TypeError, ValueError):
                mag = -1.0
            try:
                dep = int(float(hy.get("depth")))
            except (TypeError, ValueError):
                dep = -1
            row = [ts, round(lat, 3), round(lon, 3), dep, round(mag, 1),
                   int(eq.get("maxScale") if eq.get("maxScale") is not None else -1)]
            key = f"{row[0]}|{row[1]}|{row[2]}"
            if key not in items:
                added += 1
            items[key] = row

        # このページで7/28より前まで戻れたら、これ以上たどる必要はない
        if page_oldest < "2026-07-28T00:00":
            break

    out = sorted(items.values(), key=lambda x: x[0])[-HYPO_KEEP:]
    if verbose:
        print(f"  震源: 新規 {added}件 ／ 保持 {len(out)}件 ／ 取得ページ {fetched}")
    return {
        "items": out,
        "count": len(out),
        "added": added,
        "backfilled": bool(out) and out[0][0] <= "2026-07-28T16:27",
    }


# --- Yahoo!くらし 防災情報（自治体のLINE/防災メールと同じ配信の公開ミラー） -------
#
# 自治体が Yahoo! と災害協定を結んで流している「自治体からの緊急情報」が、
# ログイン不要のウェブページとして残っている。八代市はLINEに流すのと同じ本文を
# ここにも同時に投げていて、実測ではLINEより早く出た（8/1 給水所追加＝Yahoo 10:56 / LINE 11:09）。
# 他の自治体はLアラート由来の「避難所開設情報」が中心で、手書きのお知らせは流れてこない。
YAHOO_BASE = "https://kurashi.yahoo.co.jp"
YAHOO_MUNI = [
    ("43202", "八代市"),   # 自由文のお知らせが全文流れてくる。ここが本命
    ("43468", "氷川町"),
    ("43211", "宇土市"),
    ("43213", "宇城市"),
    ("43100", "熊本市"),
    ("43443", "益城町"),
    ("43348", "美里町"),
]
YAHOO_KEEP = 120          # 本文の保管件数（自治体ごとではなく全体）
YAHOO_DETAIL_PER_RUN = 12  # 1回の実行で本文を取りにいく上限。取りこぼしても次回拾う


def _yahoo_get(url):
    req = urllib.request.Request(url, headers={
        "User-Agent": UA,
        "Accept": "text/html,application/xhtml+xml",
        "Accept-Language": "ja",
    })
    with urllib.request.urlopen(req, timeout=20) as res:
        return decode(res.read())


def _yahoo_strip(html):
    """本文だけを抜く。<main> の中の、日時のあとから「共有」までを本文とみなす。"""
    html = re.sub(r"(?is)<(script|style|noscript)[^>]*>.*?</\1>", " ", html)
    html = re.sub(r"(?i)<br\s*/?>", "\n", html)
    html = re.sub(r"(?i)</(p|div|li|h\d)>", "\n", html)
    text = re.sub(r"(?s)<[^>]+>", "", html)
    text = html_mod.unescape(text)
    lines = [l.strip() for l in text.split("\n")]
    # 「更新:2026/8/1(土) 10:56」の次の行から「共有」の手前まで
    start = end = None
    for i, l in enumerate(lines):
        if start is None and re.match(r"^更新[:：]\s*\d{4}/\d{1,2}/\d{1,2}", l):
            start = i + 1
        elif start is not None and l in ("共有", "情報提供", "この情報を共有する"):
            end = i
            break
        elif start is not None and l.startswith("情報提供"):
            end = i
            break
    if start is None:
        return ""
    body = [l for l in lines[start:end if end is not None else start + 60] if l]
    return "\n".join(body).strip()


def fetch_yahoo_bousai(prev, verbose=False):
    """自治体の防災配信（Yahoo!くらし）を集める。

    返すのは {"updated_at":..., "items":[{muni,id,url,title,at,body}, ...]}。
    一覧は毎回引くが、本文は初めて見たIDのぶんだけ取りにいく。
    本文が取れなくても見出しと時刻は残す（次回また本文を試す）。
    """
    old = prev.get("yahoo") or {}
    known = {it["id"]: it for it in (old.get("items") or []) if it.get("id")}

    listed, detail_budget = [], YAHOO_DETAIL_PER_RUN
    for code, name in YAHOO_MUNI:
        url = f"{YAHOO_BASE}/kumamoto/{code}/incidents/bousai/history"
        try:
            html = _yahoo_get(url)
        except Exception as e:
            if verbose:
                print(f"    Yahoo {name} 一覧の取得に失敗 {type(e).__name__}: {e}")
            continue
        # <a href="/kumamoto/43202/incidents/bousai/398846">タイトル…2026/8/1(土) 10:56</a>
        for m in re.finditer(
                r'href="(/kumamoto/' + code + r'/incidents/bousai/(\d+))"[^>]*>(.*?)</a>',
                html, re.S):
            href, iid, inner = m.group(1), m.group(2), m.group(3)
            txt = html_mod.unescape(re.sub(r"(?s)<[^>]+>", " ", inner))
            txt = re.sub(r"\s+", " ", txt).strip()
            dm = re.search(r"(\d{4})/(\d{1,2})/(\d{1,2}).*?(\d{1,2}):(\d{2})", txt)
            at = ""
            if dm:
                y, mo, da, h, mi = (int(x) for x in dm.groups())
                at = dt.datetime(y, mo, da, h, mi, tzinfo=JST).isoformat()
                txt = txt[:dm.start()].strip()
            if not txt:
                continue
            listed.append({"muni": name, "id": iid, "url": YAHOO_BASE + href,
                           "title": txt, "at": at})
        time.sleep(REQUEST_INTERVAL)

    out = []
    for it in listed:
        prev_it = known.get(it["id"])
        body = (prev_it or {}).get("body", "")
        if not body and detail_budget > 0:
            detail_budget -= 1
            try:
                body = _yahoo_strip(_yahoo_get(it["url"]))
            except Exception as e:
                if verbose:
                    print(f"    Yahoo 本文の取得に失敗 {it['id']} {type(e).__name__}: {e}")
                body = ""
            time.sleep(REQUEST_INTERVAL)
        it = dict(it)
        it["body"] = body[:1800]
        out.append(it)

    # 一覧から消えたものも、本文を持っているぶんは残す（過去分を失わないため）
    seen = {it["id"] for it in out}
    for iid, it in known.items():
        if iid not in seen:
            out.append(it)

    out.sort(key=lambda x: x.get("at") or "", reverse=True)
    out = out[:YAHOO_KEEP]
    if verbose:
        withbody = sum(1 for x in out if x.get("body"))
        print(f"  Yahoo!くらし: {len(out)}件（本文あり {withbody}件）")
    return {"updated_at": dt.datetime.now(JST).isoformat(),
            "items": out, "count": len(out)}


def fetch_cao_gas(prev, verbose=False):
    """内閣府の被害状況PDFから都市ガスの供給停止戸数を読む。

    住家被害はここから取っていません。消防庁経由の集計で、市町村からの報告が
    集まるまで数日かかるため、熊本県が自ら発表している戸数と桁が違うからです
    （7/30時点で内閣府2棟に対し、県の発表は7/31時点で1,507戸）。数字が小さい
    ほうを載せると被害を過小に見せることになるので、住家被害は手作業のままに
    しています。
    """
    import io
    import pdfplumber

    _st, raw, _ = fetch(CAO_INDEX, {})
    if not raw:
        raise ValueError("内閣府のページを取得できませんでした")
    idx = decode(raw)
    days = sorted(set(re.findall(r"pdf/r8kumamoto_jishin_(\d{8})\.pdf", idx)))
    if not days:
        raise ValueError("内閣府のPDFが見つかりませんでした")
    day = days[-1]

    old = prev.get("gas") or {}
    reusable = (old.get("pdf_day") == day
                and old.get("parser") == CAO_PARSER
                and old.get("current") is not None)
    if reusable:
        if verbose:
            print(f"    内閣府PDF {day} は取得済み（{old.get('current'):,}戸）")
        return old

    pdf_url = CAO_PDF % day
    body = fetch_bytes(pdf_url)
    with pdfplumber.open(io.BytesIO(body)) as pdf:
        txt = "\n".join((pg.extract_text() or "") for pg in pdf.pages[:8])

    out = {"pdf_day": day, "parser": CAO_PARSER, "source_url": pdf_url,
           "source_page": CAO_INDEX, "label": "内閣府（経済産業省情報）"}

    tight = re.sub(r"\s+", "", txt)   # 空白も改行も詰める。PDFは行の途中で折り返すため。
    m = re.search(r"令和８年(\d{1,2})月(\d{1,2})日(\d{1,2})時(\d{1,2})分現在", tight)
    if not m:
        m = re.search(r"(\d{1,2})月(\d{1,2})日(\d{1,2}):(\d{2})時点", tight)
    if m:
        out["as_of"] = f"{int(m.group(1))}月{int(m.group(2))}日 {int(m.group(3))}:{m.group(4).zfill(2)}"

    # 改行も詰める。PDFは行の途中で折り返すため（「8月3日に全戸再\n開予定」など）、
    # 改行を残したまま正規表現をかけると拾えない。
    i = tight.find("ア都市ガス")
    j = tight.find("イＬＰガス", i + 1)
    seg = tight[i:j if j > i else i + 900] if i >= 0 else ""

    # 「約8,892戸供給停止」「8,892戸供給停止」どちらの書き方もある（7/31に書式が変わった）
    stops = re.findall(r"約?([\d,]+)戸供給停止", seg)
    if stops:
        out["current"] = int(stops[0].replace(",", ""))
    else:
        # 読み取れなかったときは0にしない。0にすると「解消した」と表示され、
        # 実際にはガスが止まっているのに解消済みだと誤って伝えてしまう。
        out["current"] = None
        out["parse_failed"] = True

    # 地区名。「●八代」のような見出し、または「八代市で、」の両方に対応する
    cities = re.findall(r"●([一-龥]{2,6})", seg) or re.findall(r"([^\s。、]{2,6}市)で、", seg)
    out["cities"] = [c if c[-1] in "市町村" else c + "市" for c in cities[:2]]

    out["cleared"] = re.findall(r"([一-龥]{2,4}市)の供給支障(?:について)?は(?:すべて)?解消済み", seg)

    m2 = re.search(r"(\d{1,2})月(\d{1,2})日に全戸再開予定", seg)
    if m2:
        out["restore"] = f"{int(m2.group(1))}月{int(m2.group(2))}日に全戸再開予定"
    if verbose:
        cur = out["current"]
        print(f"    都市ガス {('%s戸' % f'{cur:,}') if cur is not None else '読み取れず'}"
              f"（{out.get('as_of','時点不明')}）／{'・'.join(out['cities']) or '地区不明'}"
              f"／解消: {'、'.join(out['cleared']) or 'なし'}"
              f"／{out.get('restore','見通しの記載なし')}")
    return out


def merge_quake_hours(prev, quakes):
    """時間ごとの有感地震回数を積み上げる。

    返すのは {"2026-07-30T20": [その時間の回数, うち震度3以上の回数], ...} と、
    処理済みの最大eid。eidは yyyymmddhhmmss なので、これより大きいものだけを
    新しい地震として数える。あとから小さいeidで追加・訂正された地震は数え落と
    しますが、二重に数えるより少なく数えるほうが安全なのでこの方式にしています。
    数え落としがある前提で、サイト側には「当サイト集計の速報値」と明記します。
    """
    hours = {}
    for k, v in (prev.get("quake_hours") or {}).items():
        if isinstance(v, list) and len(v) == 2:
            hours[k] = [int(v[0]), int(v[1])]
    max_eid = str(prev.get("quake_max_eid") or "")

    added = 0
    for q in quakes:
        eid = str(q.get("eid") or "")
        at = q.get("at") or ""
        if not eid or not at or at[:10] < QUAKE_HIST_FROM:
            continue
        if max_eid and eid <= max_eid:
            continue
        row = hours.setdefault(at[:13], [0, 0])       # "2026-07-30T20"
        row[0] += 1
        m = re.match(r"(\d)", str(q.get("maxi") or ""))
        if m and int(m.group(1)) >= 3:
            row[1] += 1
        added += 1

    for q in quakes:
        eid = str(q.get("eid") or "")
        if eid > max_eid:
            max_eid = eid

    for k in sorted(hours)[:-QUAKE_HOURS_KEEP]:
        del hours[k]
    return hours, max_eid, added


def crawl(verbose=False):
    state = load_state()
    now = dt.datetime.now(JST)

    updates, quakes, errors, sources_status = [], [], [], []
    stats = {}
    prev = {}
    if os.path.exists(DATA_PATH):
        try:
            with open(DATA_PATH, encoding="utf-8") as f:
                prev = json.load(f)
        except Exception:
            prev = {}
    prev_updates = {u["url"]: u for u in prev.get("updates", []) if u.get("url")}

    started = time.time()
    skipped = []

    for i, src in enumerate(SOURCES):
        sid = src["id"]
        entry = state.get(sid, {})

        # 予算を使い切ったら、残りは前回値を持ち越して打ち切る。
        # 全部を諦めるより、取れたところまで出して更新を止めないほうがよい。
        if time.time() - started > TIME_BUDGET:
            skipped.append(src["label"])
            updates.extend(entry.get("items", []))
            if src["group"] == "quake" and entry.get("quakes"):
                quakes = entry["quakes"]
            if src["group"] == "stat" and entry.get("stat"):
                stats[sid] = entry["stat"]
            sources_status.append({"id": sid, "label": src["label"],
                                   "status": "時間切れ（前回値）", "checked_at": now.isoformat()})
            continue

        if i:
            time.sleep(REQUEST_INTERVAL)
        if verbose:
            print(f"[{sid}] {src['url']}")
        try:
            status, raw, new_entry = fetch(src["url"], entry, verbose)
            if status == 304:
                sources_status.append({"id": sid, "label": src["label"],
                                       "status": "変更なし", "checked_at": now.isoformat()})
                # 前回このソースから採った結果をそのまま引き継ぐ。
                # 出力側(data.json)ではなく state 側に持たせているのは、出力は件数上限で
                # 切り詰められるため、そこから復元すると切られた分が永久に消えるから。
                updates.extend(entry.get("items", []))
                if src["group"] == "quake" and entry.get("quakes"):
                    quakes = entry["quakes"]
                if src["group"] == "stat" and entry.get("stat"):
                    stats[sid] = entry["stat"]
                continue

            body_text = decode(raw)
            body_hash = hashlib.sha256(raw).hexdigest()
            changed = body_hash != entry.get("hash")
            new_entry["hash"] = body_hash
            new_entry["last_ok"] = now.isoformat()
            state[sid] = new_entry

            kind = src["kind"]
            if kind == "jma_quake_json":
                quakes = parse_jma_quake(body_text, src)
                new_entry["quakes"] = quakes
                state[sid] = new_entry
                sources_status.append({"id": sid, "label": src["label"],
                                       "status": f"{len(quakes)}件", "checked_at": now.isoformat()})
                if verbose:
                    print(f"    地震 {len(quakes)}件")
                continue
            elif kind == "kyuden_power":
                stat = parse_kyuden_power(body_text, src, verbose)
                stat["fetched_at"] = now.isoformat()
                stats[sid] = stat
                new_entry["stat"] = stat
                state[sid] = new_entry
                label = f"{stat['current']:,}戸"
                sources_status.append({"id": sid, "label": src["label"],
                                       "status": label, "checked_at": now.isoformat()})
                if verbose:
                    print(f"    停電 {label}")
                continue
            elif kind == "mlit_water":
                stat = parse_mlit_water(body_text, src, entry, verbose)
                if stat:
                    stat["fetched_at"] = now.isoformat()
                    stats[sid] = stat
                    new_entry["stat"] = stat
                    new_entry["water_pdf_url"] = stat.get("source_url")
                    label = (f"第{stat.get('report_no')}報／"
                             f"{stat.get('current', 0):,}戸")
                elif entry.get("stat"):
                    stats[sid] = entry["stat"]
                    new_entry["stat"] = entry["stat"]
                    label = "前回値を保持"
                else:
                    label = "取得できず"
                state[sid] = new_entry
                sources_status.append({"id": sid, "label": src["label"],
                                       "status": label, "checked_at": now.isoformat()})
                if verbose:
                    print(f"    {label}")
                continue
            elif kind == "misato_json":
                items = parse_misato_json(body_text, src)
            elif kind == "uki_html":
                items = parse_uki_html(body_text, src)
            elif kind == "kvc_html":
                items = parse_kvc_html(body_text, src)
            elif kind == "kinkyu_html":
                items = parse_kinkyu_html(body_text, src)
            else:
                items = parse_rss(body_text, src)

            kept, taken = 0, []
            for it in items:
                mode = src.get("filter")
                if mode:
                    text = it["title"] + " " + it.get("desc", "")
                    if not DISASTER_KW.search(text):
                        continue
                    if mode == "local" and not AREA_KW.search(text):
                        continue
                key = it["url"] or (sid + it["title"])
                it2 = dict(it)
                it2["source_id"] = sid
                it2["source"] = src["label"]
                it2["group"] = src["group"]
                it2["area"] = src.get("area", "")
                # 初めて見た日時を控える（公開日が取れないソース用）
                it2["first_seen"] = prev_updates.get(key, {}).get("first_seen") or now.isoformat()
                updates.append(it2)
                taken.append(it2)
                kept += 1

            new_entry["items"] = taken
            state[sid] = new_entry

            sources_status.append({
                "id": sid, "label": src["label"],
                "status": ("更新あり" if changed else "変更なし") + f"／{kept}件",
                "checked_at": now.isoformat(),
            })
            if verbose:
                print(f"    {len(items)}件取得 → {kept}件採用 ({'更新あり' if changed else '内容同一'})")

        except Exception as e:
            msg = f"{type(e).__name__}: {e}"
            errors.append({"id": sid, "label": src["label"], "error": msg,
                           "at": now.isoformat()})
            sources_status.append({"id": sid, "label": src["label"],
                                   "status": "取得失敗", "checked_at": now.isoformat()})
            # 前回分を残す（落ちた瞬間に情報が消えるのを防ぐ）
            updates.extend(entry.get("items", []))
            if src["group"] == "quake" and entry.get("quakes"):
                quakes = entry["quakes"]
            if src["group"] == "stat" and entry.get("stat"):
                stats[sid] = entry["stat"]
            if verbose:
                print(f"    失敗 {msg}")

    # 重複除去して新しい順に
    seen, uniq = set(), []
    for u in updates:
        if u.get("group") == "official_feed":
            # 気象庁の防災情報XMLは地震一覧の裏取り用。同じ見出しが並ぶので更新欄には出さない
            continue
        k = u.get("url") or (u.get("source_id", "") + u.get("title", ""))
        if k in seen:
            continue
        seen.add(k)
        uniq.append(u)

    def recency(x):
        return x.get("published") or x.get("first_seen") or ""

    uniq.sort(key=recency, reverse=True)

    # 同じ見出しの繰り返しを畳む。自治体の緊急情報ページは地震のたびに「地震情報」という
    # 同一見出しの記事を積むため、そのままだと一覧がそれで埋まる。地震そのものは
    # 気象庁の一覧で見られるので、同一見出しは最新の1件だけ残す。
    title_seen, folded = set(), []
    for u in uniq:
        k = (u.get("source_id", ""), u.get("title", ""))
        if k in title_seen:
            continue
        title_seen.add(k)
        folded.append(u)
    uniq = folded

    # NHKのように件数の多いソースに、更新の少ない自治体が押し流されるのを防ぐ。
    # まず各ソースの上位 KEEP_PER_SOURCE 件を確保し、残り枠を新しい順で埋める。
    per, picked = {}, []
    for u in uniq:
        sid = u.get("source_id", "")
        if per.get(sid, 0) < KEEP_PER_SOURCE:
            per[sid] = per.get(sid, 0) + 1
            picked.append(u)
    picked_ids = {id(x) for x in picked}
    for u in uniq:
        if len(picked) >= KEEP_UPDATES:
            break
        if id(u) not in picked_ids:
            picked.append(u)
    picked.sort(key=recency, reverse=True)

    if skipped:
        errors.append({"id": "_budget", "label": "時間切れ",
                       "error": f"{TIME_BUDGET}秒を超えたため未取得: " + "、".join(skipped),
                       "at": now.isoformat()})

    try:
        gas = fetch_cao_gas(prev, verbose)
        sources_status.append({"id": "cao_gas", "label": "内閣府 被害状況（都市ガス）",
                               "status": f"{gas.get('current', 0):,}戸", "checked_at": now.isoformat()})
    except Exception as e:
        gas = prev.get("gas") or {}
        errors.append({"id": "cao_gas", "label": "内閣府 被害状況（都市ガス）",
                       "error": str(e), "at": now.isoformat()})
        sources_status.append({"id": "cao_gas", "label": "内閣府 被害状況（都市ガス）",
                               "status": "取得失敗", "checked_at": now.isoformat()})

    try:
        yahoo = fetch_yahoo_bousai(prev, verbose)
        sources_status.append({"id": "yahoo_bousai", "label": "Yahoo!くらし 自治体の防災配信",
                               "status": f"{yahoo['count']}件", "checked_at": now.isoformat()})
    except Exception as e:
        yahoo = prev.get("yahoo") or {"items": [], "count": 0}
        errors.append({"id": "yahoo_bousai", "label": "Yahoo!くらし 自治体の防災配信",
                       "error": str(e), "at": now.isoformat()})
        sources_status.append({"id": "yahoo_bousai", "label": "Yahoo!くらし 自治体の防災配信",
                               "status": "取得失敗", "checked_at": now.isoformat()})

    try:
        hypo = fetch_hypocenters(prev, verbose)
        sources_status.append({"id": "p2p_hypo", "label": "P2P地震情報（震源の位置）",
                               "status": f"{hypo['count']}件", "checked_at": now.isoformat()})
    except Exception as e:
        hypo = prev.get("hypo") or {"items": [], "count": 0}
        errors.append({"id": "p2p_hypo", "label": "P2P地震情報（震源の位置）",
                       "error": str(e), "at": now.isoformat()})
        sources_status.append({"id": "p2p_hypo", "label": "P2P地震情報（震源の位置）",
                               "status": "取得失敗", "checked_at": now.isoformat()})

    quake_hours, quake_max_eid, quake_added = merge_quake_hours(prev, quakes)
    if verbose:
        print(f"  地震の回数集計: 新規 {quake_added}件 ／ 保持 {len(quake_hours)}時間分")

    data = {
        "generated_at": now.isoformat(),
        "quakes": quakes,
        "gas": gas,
        "quake_hours": quake_hours,
        "hypo": hypo,
        "yahoo": yahoo,
        "quake_max_eid": quake_max_eid,
        "stats": stats,
        "updates": picked[:KEEP_UPDATES],
        "sources": sources_status,
        "errors": errors,
    }

    os.makedirs(OUT_DIR, exist_ok=True)
    with open(DATA_PATH, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=1)
    save_state(state)
    return data


MARK_START = "/*__CRAWLED_DATA_START__*/"
MARK_END = "/*__CRAWLED_DATA_END__*/"


def inject(html_path, data):
    """HTMLのマーカー間にデータを埋め込み、単一ファイルのまま最新化する。"""
    with open(html_path, encoding="utf-8") as f:
        html = f.read()
    if MARK_START not in html or MARK_END not in html:
        print(f"警告: {html_path} にマーカーが見つかりません。埋め込みをスキップします。",
              file=sys.stderr)
        return False
    payload = "window.__CRAWLED__ = " + json.dumps(data, ensure_ascii=False) + ";"
    start = html.index(MARK_START) + len(MARK_START)
    end = html.index(MARK_END)
    html = html[:start] + "\n" + payload + "\n" + html[end:]
    with open(html_path, "w", encoding="utf-8") as f:
        f.write(html)
    return True


def main():
    ap = argparse.ArgumentParser(description="令和8年熊本地震 一次情報巡回クローラー")
    ap.add_argument("--inject", metavar="HTML", help="巡回結果を指定HTMLに埋め込む")
    ap.add_argument("--verbose", "-v", action="store_true")
    ap.add_argument("--once", action="store_true", help="（既定）1回だけ実行")
    args = ap.parse_args()

    t0 = time.time()
    data = crawl(verbose=args.verbose)
    ok = sum(1 for s in data["sources"] if s["status"] != "取得失敗")
    print(f"巡回完了 {dt.datetime.now(JST).strftime('%Y-%m-%d %H:%M:%S')} "
          f"／ ソース {ok}/{len(data['sources'])} 成功"
          f"／ 更新 {len(data['updates'])}件 ／ 地震 {len(data['quakes'])}件"
          f"／ {time.time()-t0:.1f}秒")
    budget_ng = [s for s in data["sources"] if s["status"] == "時間切れ（前回値）"]
    if budget_ng:
        print(f"  時間切れで未取得（前回値を表示）: " +
              "、".join(s["label"] for s in budget_ng), file=sys.stderr)
    for e in data["errors"]:
        print(f"  失敗: {e['label']} — {e['error']}", file=sys.stderr)

    if args.inject:
        if inject(args.inject, data):
            print(f"{args.inject} に埋め込みました")


if __name__ == "__main__":
    main()
