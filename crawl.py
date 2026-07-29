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

# ----------------------------------------------------------------------------
# 設定
# ----------------------------------------------------------------------------

# 連絡先を必ず自分のものに書き換えてください。
# 迷惑をかけた際に相手から連絡が来る余地を残すのが礼儀であり、遮断の回避にもなります。
CONTACT = "https://example.org/kumamoto-info (contact@example.org)"
UA = f"KumamotoQuakeInfoBot/1.0 (+{CONTACT})"

REQUEST_INTERVAL = 3.0      # 各リクエストの間隔（秒）
TIMEOUT = 20
MAX_ITEMS_PER_SOURCE = 30
KEEP_UPDATES = 200          # data.json に残す更新件数の上限
KEEP_PER_SOURCE = 10        # 各ソースから最低限確保する件数（多弁なソースに埋もれさせない）
KEEP_QUAKES = 40

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

    # --- 報道（一次情報ではないので区別して扱う） ---------------------------
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

def crawl(verbose=False):
    state = load_state()
    now = dt.datetime.now(JST)

    updates, quakes, errors, sources_status = [], [], [], []
    prev = {}
    if os.path.exists(DATA_PATH):
        try:
            with open(DATA_PATH, encoding="utf-8") as f:
                prev = json.load(f)
        except Exception:
            prev = {}
    prev_updates = {u["url"]: u for u in prev.get("updates", []) if u.get("url")}

    for i, src in enumerate(SOURCES):
        if i:
            time.sleep(REQUEST_INTERVAL)
        sid = src["id"]
        entry = state.get(sid, {})
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
            elif kind == "misato_json":
                items = parse_misato_json(body_text, src)
            elif kind == "uki_html":
                items = parse_uki_html(body_text, src)
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

    data = {
        "generated_at": now.isoformat(),
        "quakes": quakes,
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
    for e in data["errors"]:
        print(f"  失敗: {e['label']} — {e['error']}", file=sys.stderr)

    if args.inject:
        if inject(args.inject, data):
            print(f"{args.inject} に埋め込みました")


if __name__ == "__main__":
    main()
