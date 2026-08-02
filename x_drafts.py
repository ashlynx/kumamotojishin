#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Xの投稿候補をつくる。

考え方
------
リポストされるのは「サイトの紹介」ではなく**情報そのもの**なので、
本文を主役にして、URLは末尾に1つだけ添える。

**要約はしない。** 自治体の発表文を機械が言い換えると、意味が変わったときに
気づけない。だから元の文から**文単位で削る**だけにして、書き換えはしない。
280字（重み付き）に収まらなければ、後ろの文から落とす。それでも入らなければ捨てる。

**全自動で投稿しない。** 出力はあくまで候補で、人が選んで出す。
誤情報を自分のアカウント名で流すのが、このサイトにとって一番大きな損失になる。

使い方
------
    python3 x_drafts.py                     # out/data.json から候補を作る
    python3 x_drafts.py --url               # R2 の data.json を直接読む
    python3 x_drafts.py --n 8               # 出す本数（既定5）
    python3 x_drafts.py --all               # 既出のものも含めて出す
"""

import os
import re
import sys
import json
import argparse
import unicodedata
import datetime as dt
import urllib.request

JST = dt.timezone(dt.timedelta(hours=9))
HERE = os.path.dirname(os.path.abspath(__file__))
DATA_PATH = os.path.join(HERE, "out", "data.json")
DATA_URL = "https://data.kumamotojishin.jp/data.json"
STATE_PATH = os.path.join(HERE, "x_state.json")
SITE = "https://kumamotojishin.jp/"
LIMIT = 280

# 地名。X内検索で拾われる要なので、本文になければ先頭に足す。
AREAS = ["八代市", "宇土市", "宇城市", "氷川町", "益城町", "美里町", "熊本市",
         "上天草市", "天草市", "御船町", "甲佐町", "嘉島町"]

# 時間に意味のあるもの＝いま出す価値があるもの。上ほど強い。
TOPICS = [
    (100, re.compile(r"給水|断水|飲料水")),
    (95,  re.compile(r"入浴|風呂|洗濯|シャワー")),
    (90,  re.compile(r"り災証明|罹災証明|被災証明")),
    (85,  re.compile(r"ボランティア")),
    (80,  re.compile(r"避難所|車中泊|仮設")),
    (75,  re.compile(r"災害ごみ|災害ゴミ|廃棄物|仮置")),
    (70,  re.compile(r"支援金|義援金|見舞金|減免|支援制度")),
    (65,  re.compile(r"停電|ガス|水道|通行止|運休")),
    (60,  re.compile(r"熱中症|感染症|健康|医療|診療")),
    (55,  re.compile(r"炊き出し|物資|相談")),
]
# 出しても意味の薄いもの
SKIP = re.compile(r"ダム放流|訓練|募集は終了|テスト")


def weighted(text):
    """Xの文字数。全角2・半角1・URLは長さに関係なく23。"""
    text = re.sub(r"https?://\S+", "U" * 23, text)
    n = 0
    for ch in text:
        if ch == "U":
            n += 1
            continue
        n += 2 if unicodedata.east_asian_width(ch) in ("F", "W", "A") else 1
    return n


def sentences(body):
    """文に割る。箇条書きの行はそのまま1文として扱う。"""
    out = []
    for line in body.split("\n"):
        line = line.strip()
        if not line:
            continue
        if line.startswith(("・", "○", "●", "※", "【")) or "：" in line or ":" in line:
            out.append(line)
            continue
        parts = re.split(r"(?<=。)", line)
        out.extend(p.strip() for p in parts if p.strip())
    return out


MIN_LEN = 90                       # これより短い投稿は中身がない
MAX_AGE_H = 30                     # これより古い発表は出さない（1日で状況が変わるため）
# 「どこで」「いつ」に当たる部分。削った結果これが消えたら、その投稿は捨てる。
DETAIL = re.compile(r"[・○●]|\d{1,2}[:：]\d{2}|\d{1,2}時|\d+丁目|\d+番地|[市町村]\S*\d")
TRUNCATED = re.compile(r"(…|\.\.\.)\s*$")
# 挨拶だけの文。これしか残らなかった候補は捨てる——
# 「心より感謝申し上げます」だけを流しても、読んだ人は何もできない。
GREETING = re.compile(r"お見舞い|感謝申し上げ|御礼|お礼申し上げ|ご協力(を)?(お願い|賜り)|"
                      r"心より|謹んで|ご迷惑をおかけ|ご理解とご協力")


# 落としてはいけない行。箇条書き（場所の一覧）と、
# 自治体のお知らせで定型になっている項目名（時間・場所・日時など）。
# ここが消えると「いつ、どこへ行けばいいか」が分からない投稿になる。
BULLET = re.compile(r"^[・○●]|^(時間|時刻|場所|日時|期間|受付|対象|配布)")


def fit(head, body, tail):
    """head＋本文＋tail が280に収まるよう、本文を削る。

    削るだけで、書き換えはしない。ただし削る順番が重要で、
    **箇条書きの行（・○●）は絶対に落とさない。** 自治体のお知らせでは、
    そこが「どこへ行けばいいか」＝場所の一覧だからです。
    実際、最初の版は「1箇所追加します」だけを残して
    「・八代市役所(庁舎西側)」を落としてしまい、行き先の分からない投稿になりました。

    箇条書きを全部残しても入らないときは、その候補ごと捨てます。
    中途半端に削るより、出さないほうがましなので。
    """
    ss = [x for x in sentences(body) if not TRUNCATED.search(x)]
    total = len(ss)
    if not ss:
        return None, 0, 0

    def render(keep):
        return (head + "\n\n" if head else "") + "\n".join(keep) + "\n\n" + tail

    keep = list(ss)
    # 後ろから落とすが、箇条書きは飛ばす
    while weighted(render(keep)) > LIMIT:
        drop = None
        for i in range(len(keep) - 1, -1, -1):
            if not BULLET.match(keep[i]):
                drop = i
                break
        if drop is None:
            return None, 0, total      # 箇条書きだけになっても入らない
        keep.pop(drop)
        if not keep:
            return None, 0, total

    text = render(keep)
    if weighted(text) < MIN_LEN:
        return None, 0, total
    # 「いつ」も「どこ」も残っていない投稿は出さない
    if any(DETAIL.search(x) for x in ss) and not any(DETAIL.search(x) for x in keep):
        return None, 0, total
    # 挨拶しか残らなかった投稿も出さない
    if not any(not GREETING.search(x) for x in keep):
        return None, 0, total
    return text, len(keep), total


def score(muni, title, body, at):
    """出す価値の見積り。話題の強さ＋新しさ＋具体性。"""
    text = (title or "") + " " + (body or "")
    if SKIP.search(text):
        return 0
    s = 0
    for w, pat in TOPICS:
        if pat.search(text):
            s = max(s, w)
    if not s:
        return 0
    # 古いものは出さない。給水所も避難所も1日で変わるので、
    # 昨日の発表をいま流すのは、正しくない情報を流すのとほぼ同じになる。
    try:
        age_h = (dt.datetime.now(JST) - dt.datetime.fromisoformat(at)).total_seconds() / 3600
    except Exception:
        age_h = 999
    if age_h > MAX_AGE_H:
        return 0
    s *= max(0.2, 0.5 ** (age_h / 12))       # 新しいほど強い（12時間で半減）
    # 時刻や住所が入っているものは具体的で、読んだ人がすぐ動ける
    if re.search(r"\d{1,2}[:：]\d{2}|\d{1,2}時", text):
        s += 12
    if re.search(r"[町丁目]\d|\d+番地|\d-\d", text):
        s += 8
    # 「変更」「追加」「締切」は時限性が高い
    if re.search(r"変更|追加|締切|締め切|中止|延期|開始|解消", text):
        s += 10
    return s


# スレッドの2本目。1本目は公式へのリンクのままにして、当サイトへの導線はここに置く。
# 1本目でなりすましにならず、2本目で「他の市町村もまとめてある」と言える。
# リポストされるのは1本目だが、そこから来た人はスレッドを開く。
REPLIES = [
    (re.compile(r"給水|断水"),
     "県内の給水所は、現在地から近い順に地図で並べています。\n市町村をまたいで探せます。\n\n" + SITE),
    (re.compile(r"入浴|風呂|洗濯|シャワー"),
     "入浴できる場所は、熊本市の無料開放23か所と、八代港の自衛隊・海上保安庁の支援を\n"
     "1か所にまとめています。持ち物と時間の注意も書いています。\n\n" + SITE),
    (re.compile(r"ボランティア"),
     "市町村ごとの受入状況を一覧にしています。募集範囲も締切も申し込み方法も\nばらばらなので、"
     "行く前に比べてください。\n\n" + SITE),
    (re.compile(r"り災証明|罹災証明|被災証明"),
     "り災証明の申請と、片付ける前の写真の撮り方をまとめています。\n"
     "マイナポータルの同意確認には、チェックすると一部損壊で確定してしまう項目があります。\n\n"
     + SITE + "risai"),
    (re.compile(r"避難所|車中泊"),
     "開設中の避難所は、市町村ごとに地図で見られます。\n\n" + SITE),
    (re.compile(r"災害ごみ|廃棄物|仮置"),
     "災害ごみの出し方は市町村ごとに違います。仮置場と必要な書類をまとめています。\n\n" + SITE),
]
DEFAULT_REPLY = ("県内8市町村の給水所・避難所・支援制度を1か所にまとめています。\n"
                 "公式発表を10分おきに巡回しています。\n\n" + SITE)


def reply_for(text, muni):
    """1本目に付けるリプライ（当サイトへの導線）を選ぶ。"""
    for pat, r in REPLIES:
        if pat.search(text):
            return r if weighted(r) <= LIMIT else DEFAULT_REPLY
    return DEFAULT_REPLY


def summary_posts(data):
    """当サイトにしかない「またぎ」の投稿。ここは堂々と当サイトへ送る。

    単発の発表と違って、複数の発表を並べ直したものは公式に同じページが無い。
    それが当サイトの存在理由なので、リンク先を迷う必要がない。
    """
    out = []
    st = data.get("stats") or {}
    water = sum(len((c or {}).get("items") or []) for c in [])  # サイト側の数はHTMLにあるため使わない
    n_yahoo = len([x for x in (data.get("yahoo") or {}).get("items", []) if x.get("body")])

    w = (st.get("mlit_water") or {})
    if w.get("current"):
        out.append({
            "score": 60, "key": "s:water:" + str(w.get("report_no")), "muni": "まとめ",
            "at": dt.datetime.now(JST).isoformat(), "kind": "当サイトのまとめ",
            "src": SITE + "mizu", "kept": 1, "total": 1, "reply": "",
            "text": f"熊本県内の断水は約{w['current']:,}戸（国土交通省 第{w.get('report_no','?')}報・"
                    f"{w.get('as_of','')}時点）。\n\n"
                    "給水所は日ごとに場所も時間も変わります。市町村をまたいで、現在地から近い順に"
                    "並べた地図を用意しています。容器の持参をお忘れなく。\n\n" + SITE + "mizu"})

    g = (st.get("cao_gas") or data.get("gas") or {})
    if g.get("current"):
        out.append({
            "score": 55, "key": "s:gas:" + str(g.get("as_of")), "muni": "まとめ",
            "at": dt.datetime.now(JST).isoformat(), "kind": "当サイトのまとめ",
            "src": SITE, "kept": 1, "total": 1, "reply": "",
            "text": f"八代地区の都市ガスは約{g['current']:,}戸が供給停止のままです"
                    f"（{g.get('as_of','')}時点）。\n\n"
                    "ガスは電気や水道と違って、送れば戻るものではありません。"
                    "一軒ずつ開栓して器具を点検するため、各戸の立会いが要ります。\n\n" + SITE})

    out.append({
        "score": 50, "key": "s:vol", "muni": "まとめ",
        "at": dt.datetime.now(JST).isoformat(), "kind": "当サイトのまとめ",
        "src": SITE + "volunteer", "kept": 1, "total": 1, "reply": "",
        "text": "災害ボランティアは市町村ごとに条件がばらばらです。募集範囲も、締切も、"
                "申し込み方法も違います。\n\n"
                "どこも事前申込と活動保険の加入が必要で、当日いきなり行っても活動できません。\n\n"
                "受入状況を一覧にしています。\n" + SITE + "volunteer"})

    if n_yahoo:
        out.append({
            "score": 45, "key": "s:line", "muni": "まとめ",
            "at": dt.datetime.now(JST).isoformat(), "kind": "当サイトのまとめ",
            "src": SITE, "kept": 1, "total": 1, "reply": "",
            "text": "市町村がLINEで流しているお知らせを、そのままの本文で自動取得しています。\n\n"
                    "ホームページより先に出ることが多く、実測では八代市の給水所の追加が"
                    "LINEより13分早く公開されていました。\n\n" + SITE})

    return [o for o in out if weighted(o["text"]) <= LIMIT]


# 公式Xアカウントのある自治体。ここの発表は**リポストか引用リポストで済ませられる**。
# 自分で書き直すより安全で速く、なりすましにもならない。
# 逆にここに無い自治体（氷川町・益城町・美里町・上天草市・天草市・御船町・甲佐町）は、
# X上に発表そのものが存在しない。「氷川町 給水」で検索しても何も出てこない。
# **リポストできない自治体の情報こそ、当サイトが出す価値がある。**
OFFICIAL_X = {
    "八代市": "@yatsushiro0801",
    "熊本市": "@kumamotocity_",
    "宇土市": "@uto_city（宇土市公式・稼働を確認済み）",
    "宇城市": "@uki_bousai（宇城市防災・未確認）",
}


def how_to_post(muni):
    """その市町村の発表を、どう出すのがいいかを返す。

    注意：**アカウントがあっても、サイトの更新がXに流れるとは限りません。**
    実測したところ、熊本市の公式Xは給水所・地震情報・上下水道が中心で、
    「無料入浴の協力施設追加」のようなサイトの記事更新は流れていませんでした。
    当サイトのクローラーはサイトを見ているので、**X上に存在しない情報を持っています。**
    だから「アカウントがある＝引用リポストで済む」ではなく、
    **まず公式Xを見て、無ければ独自に出す**が正しい順番です。
    """
    acct = OFFICIAL_X.get(muni)
    if acct:
        return ("まず公式Xを確認", f"**{acct} を開いて、同じ発表があるか確かめてください。**\n"
                f"あれば引用リポスト（元投稿が埋め込まれるので出典の問題が消えます）。\n"
                f"**無ければ下の本文で独自に出してください。** "
                f"自治体はサイトに載せてもXには流さないことが多く、"
                f"その差ぶんが当サイトの持ち場です。")
    return ("独自に出す", "この市町村に公式Xアカウントはありません。"
            "**X上にこの発表は存在しないので、当サイトが出す価値があります。** "
            "下の本文をそのまま使えます（出典のリンク付き）。")


def load_state():
    try:
        with open(STATE_PATH, encoding="utf-8") as f:
            return set(json.load(f).get("seen", []))
    except Exception:
        return set()


def save_state(seen):
    with open(STATE_PATH, "w", encoding="utf-8") as f:
        json.dump({"seen": sorted(seen)[-800:]}, f, ensure_ascii=False)


def build(data, want, use_all):
    seen = set() if use_all else load_state()
    cands = []

    # (1) 自治体がLINE・防災メールに流した本文。いちばん具体的で、いちばん速い。
    for it in (data.get("yahoo") or {}).get("items", []):
        body = (it.get("body") or "").strip()
        if not body:
            continue
        key = "y:" + it.get("id", "")
        if key in seen:
            continue
        muni, title, at = it.get("muni", ""), it.get("title", ""), it.get("at", "")
        sc = score(muni, title, body, at)
        if sc <= 0:
            continue
        # 発表文はそのまま使う（言い換えると意味が変わるため）。
        # そのかわり、
        #   ・冒頭に「どこの、いつの発表か」を必ず書く
        #   ・末尾のリンクは公式の出典にする（当サイトのURLは付けない）
        # この2つを外すと、自治体の発表を借りて自分に誘導する形になり、
        # 読む人からは当サイトが市の公式チャンネルに見えてしまう。
        # 給水所を探している人が行きたいのは、当サイトではなく市の発表そのもの。
        head = f"{muni}の発表（{int(at[5:7])}/{int(at[8:10])} {at[11:16]}）"
        text, kept, total = fit(head, body, it.get("url", ""))
        if not text:
            continue
        cands.append({"score": sc, "key": key, "muni": muni, "at": at,
                      "text": text, "kept": kept, "total": total,
                      "src": it.get("url", ""), "kind": "自治体の配信",
                      "reply": reply_for(text, muni)})

    # (2) 巡回で拾った各機関の更新。本文は持たないので、見出しと出典で組む。
    for u in data.get("updates", []):
        url = u.get("url") or ""
        key = "u:" + url
        if not url or key in seen:
            continue
        title = (u.get("title") or "").strip()
        desc = (u.get("desc") or "").strip()
        area = u.get("area") or ""
        src = u.get("source") or ""
        if u.get("group") == "news":
            continue        # 報道はそのまま流さない。一次情報だけを出す
        sc = score(area, title, desc, u.get("published") or u.get("first_seen") or "")
        if sc <= 0:
            continue
        sc -= 25            # 本文がないぶん、自治体の配信より弱い
        head = f"【{area or src}】{title}"
        text, kept, total = fit(head, desc, url)
        if not text:
            text, kept, total = fit("", head, url)
        if not text:
            continue
        cands.append({"score": sc, "key": key, "muni": area or src,
                      "at": u.get("published") or u.get("first_seen") or "",
                      "text": text, "kept": kept, "total": total,
                      "src": url, "kind": src, "reply": reply_for(text, area)})

    # 当サイトにしかない「またぎ」の投稿を混ぜる。
    # リレーばかりだと、当サイトを見に来る理由が伝わらない。
    for c in summary_posts(data):
        if c["key"] not in seen:
            cands.append(c)

    cands.sort(key=lambda c: -c["score"])

    # 同じ市町村ばかりにならないよう、1回の出力では1市町村2本までにする
    per, picked = {}, []
    for c in cands:
        m = c["muni"]
        if per.get(m, 0) >= 2:
            continue
        per[m] = per.get(m, 0) + 1
        picked.append(c)
        if len(picked) >= want:
            break
    return picked, seen


def render(picked, data):
    now = dt.datetime.now(JST)
    out = [f"# Xの投稿候補（{now:%Y-%m-%d %H:%M} 時点）", "",
           f"巡回の最終更新: {data.get('generated_at','?')[:16].replace('T',' ')}", "",
           "**そのまま出さないでください。** 必ず出典を開いて、いま出しても正しいかを確かめてから。",
           "給水所の時間や締切は、数時間で変わります。", "",
           "画像は該当タブのスクリーンショットを添えると伸びます。", "",
           "---", ""]
    if not picked:
        out += ["候補なし。前回から新しい発表がないか、条件に合うものがありませんでした。", ""]
    for i, c in enumerate(picked, 1):
        w = weighted(c["text"])
        cut = "" if c["kept"] >= c["total"] else f"／元の本文{c['total']}文のうち{c['kept']}文"
        way, note = ("独自に出す", "") if c["kind"] == "当サイトのまとめ" else how_to_post(c["muni"])
        out += [f"## {i}. {c['muni']}（{c['kind']}）　→ **{way}**",
                f"**{w} / 280**　発表 {c['at'][:16].replace('T',' ')}{cut}", ""]
        if note:
            out += [note, ""]
        out += ["```", c["text"], "```", "",
                f"出典: {c['src']}", ""]
        if c.get("reply"):
            out += ["**スレッドの2本目（リプライ）に添える案。1本目は公式リンクのままにしてください。**", "",
                    "```", c["reply"], "```",
                    f"（{weighted(c['reply'])} / 280）", ""]
        out += ["---", ""]
    return "\n".join(out)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", action="store_true", help="R2のdata.jsonを読む")
    ap.add_argument("--n", type=int, default=5, help="出す本数")
    ap.add_argument("--all", action="store_true", help="既出も含める")
    ap.add_argument("--out", default="x_candidates.md")
    a = ap.parse_args()

    if a.url:
        req = urllib.request.Request(DATA_URL, headers={"User-Agent": "x_drafts"})
        with urllib.request.urlopen(req, timeout=30) as r:
            data = json.load(r)
    else:
        with open(DATA_PATH, encoding="utf-8") as f:
            data = json.load(f)

    picked, seen = build(data, a.n, a.all)
    text = render(picked, data)
    with open(a.out, "w", encoding="utf-8") as f:
        f.write(text)
    if not a.all:
        save_state(seen | {c["key"] for c in picked})
    print(text)


if __name__ == "__main__":
    main()
