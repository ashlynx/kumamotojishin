#!/usr/bin/env python3
"""site/index.html の CITIES に書いた住所を、地図のピン用の座標に変換する。

使い方:
    python3 geocode.py site/index.html          # 足りない住所だけ変換して書き戻す
    python3 geocode.py site/index.html --check   # 書き換えず、足りないものを表示するだけ
    python3 geocode.py site/index.html --all     # 既存の座標も取り直す

避難所や給水所を CITIES に追加したあと、これを1回走らせれば地図に出るようになります。
変換は国土地理院の住所検索APIを使います（無料・登録不要）。標準ライブラリだけで動きます。

住所に番地の座標が登録されていない地域では、町名（大字）までの概略位置になります。
その場合は精度を 0 として記録し、サイト側でピンのふちどりを点線にして区別します。
施設名しか公表されていない（住所が無い）施設は、推測で座標を作らずピンにしません。
間違った場所へ人を向かわせるほうが、地図に出ないことよりも危険なためです。
"""
import json
import re
import sys
import time
import urllib.parse
import urllib.request

API = "https://msearch.gsi.go.jp/address-search/AddressSearch?q="
UA = "KumamotoQuakeInfoBot/1.0 (+https://kumamotojishin.jp/ info@kumamotojishin.jp)"
INTERVAL = 0.8          # 相手のサーバーに負荷をかけない間隔
TIMEOUT = 20

START = "const GEO = {"
END = "};"


def fetch(q):
    req = urllib.request.Request(API + urllib.parse.quote(q), headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=TIMEOUT) as f:
        return json.loads(f.read().decode("utf-8"))


def variants(addr):
    """番地まで見つからないときに、少しずつ短くして再挑戦する。"""
    yield addr
    a = re.sub(r"[-－‐]\d+$", "", addr)
    if a != addr:
        yield a
    b = re.sub(r"\d+[-－‐]?\d*$", "", addr).rstrip()
    if b not in (addr, a):
        yield b


def geocode(addr):
    for v in variants(addr):
        try:
            hits = fetch(v)
        except Exception as e:
            print("  ! 取得できませんでした:", v, e, file=sys.stderr)
            time.sleep(INTERVAL)
            continue
        if hits:
            lon, lat = hits[0]["geometry"]["coordinates"][:2]
            title = hits[0]["properties"].get("title", "")
            # 「…番地」「…番」「…号」で終わっていれば番地まで一致している
            acc = 1 if re.search(r"(番地|番|号)$", title) else 0
            return round(lat, 6), round(lon, 6), acc, title
        time.sleep(INTERVAL)
    return None


def addresses_in(html):
    """CITIES の中に書かれた住所（a:"..." と office の a）を、書かれた順に集める。"""
    i = html.index("const CITIES = [")
    j = html.index("\n];", i)
    body = html[i:j]
    found = []
    for m in re.finditer(r'\ba:"([^"]*)"', body):
        a = m.group(1).strip()
        if a and a not in found:
            found.append(a)
    return found


def parse_geo(html):
    i = html.find(START)
    if i < 0:
        return {}, -1, -1
    j = html.index("\n" + END, i)
    table = {}
    for m in re.finditer(r'"([^"]+)":\[([-\d.]+),([-\d.]+),(\d)\]', html[i:j]):
        table[m.group(1)] = [float(m.group(2)), float(m.group(3)), int(m.group(4))]
    return table, i, j + 1 + len(END)


def render_geo(table):
    lines = []
    for a in sorted(table):
        lat, lon, acc = table[a]
        lines.append('  "%s":[%s,%s,%d]' % (a, lat, lon, acc))
    return START + "\n" + ",\n".join(lines) + "\n" + END


def main():
    args = [x for x in sys.argv[1:] if not x.startswith("--")]
    flags = {x for x in sys.argv[1:] if x.startswith("--")}
    path = args[0] if args else "site/index.html"

    html = open(path, encoding="utf-8").read()
    addrs = addresses_in(html)
    table, i, j = parse_geo(html)
    if i < 0:
        print("GEO の定義が見つかりません。index.html の const GEO = {...} を消していませんか。", file=sys.stderr)
        return 1

    todo = addrs if "--all" in flags else [a for a in addrs if a not in table]
    stale = [a for a in table if a not in addrs]

    print("住所 %d件 ／ 座標あり %d件 ／ 変換が必要 %d件" % (len(addrs), len(table), len(todo)))
    if stale:
        print("CITIES から消えた住所 %d件（そのまま残します）: %s" % (len(stale), "、".join(stale)))

    if "--check" in flags:
        for a in todo:
            print("  未変換:", a)
        return 0
    if not todo:
        print("変換するものはありません。")
        return 0

    ng = []
    for a in todo:
        r = geocode(a)
        if r:
            lat, lon, acc, title = r
            table[a] = [lat, lon, acc]
            print("  OK %s → %s (%s, %s) %s" % (a, title, lat, lon, "番地まで" if acc else "町名までの概略"))
        else:
            ng.append(a)
            print("  NG %s（座標が見つかりません。ピンには出ません）" % a)
        time.sleep(INTERVAL)

    open(path, "w", encoding="utf-8").write(html[:i] + render_geo(table) + html[j:])
    print("%s を更新しました（%d件追加）。" % (path, len(todo) - len(ng)))
    if ng:
        print("見つからなかった住所は地図に出ません。表記を公式発表どおりに直すか、そのままにしてください。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
