#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""検索の受け皿になる入口ページを作る。

なぜ作るか
----------
本体（site/index.html）は1ファイルにタブを詰めた作りで、Googleから見ると
「1ページのサイト」でしかない。人が打つのは「熊本地震 罹災証明」「熊本地震 詐欺」
のような具体的な語なので、それに対応するURLが無いと土俵に上がれない。

2026年8月9日の書き直し
----------------------
初版（8/5）は8ページとも「クロール済み - インデックス未登録」のまま4日間動かなかった。
本番を調べたところ canonical・title・description はすべて正しく noindex も無い。
技術的な問題ではなく、**本文が薄すぎた**（/mizu/ で953文字）。
しかも「日ごとに変わる情報は本体サイトで」と外へ送る構成で、
単体で読む価値のないページとGoogleに判定されていた。

そこで、
  * 本文をページあたり3,000〜5,000字に増やした（中身は pages_content.py）
  * よくある質問を各ページに置き、FAQPage の構造化データにも出した
  * 目次を付けた
  * 関連ページのリンクに、何が書いてあるかの一文を添えた
  * パンくずと Article の構造化データを入れた

方針は変えていない。**日ごとに変わる値（給水所の場所、避難所の数、待ち時間）は
一切書かない。** 古い数字が検索結果に残って人を誤らせるのを避けるため。
書くのは「制度のしくみ」「順番を間違えると損すること」「当分変わらないこと」。
"""

import os
import sys
import json
import datetime as dt

from pages_content import PAGES

JST = dt.timezone(dt.timedelta(hours=9))
SITE = "https://kumamotojishin.jp"
SITE_NAME = "令和8年熊本地震 情報まとめ（非公式）"

TAB_LABEL = {
    "life": "ライフライン", "guide": "手続き・支援制度", "vol": "支援したい方へ",
    "easy": "やさしい日本語", "city": "市町村別", "map": "地図",
}

# 関連ページのリンクに添える一文。チップだけだと何のページか分からなかった。
NAV_NOTE = {
    "mizu":      ("給水所と断水", "持ち物、生活用水と飲み水の違い、家の中だけ水が出ないとき"),
    "risai":     ("り災証明書", "写真の撮り方、被害認定調査、判定に納得できないとき"),
    "sagi":      ("詐欺・悪質商法", "屋根の訪問販売、保険金請求代行、クーリング・オフ"),
    "volunteer": ("ボランティア", "申し込み、保険、募集範囲、持ち物"),
    "furo":      ("お風呂・入浴支援", "4つの種類、持ち物、市外の方も使えること"),
    "shien":     ("使える支援制度", "支援金、応急修理、税と医療費、被災ローン減免"),
    "checklist": ("やることリスト", "今日じゅうに・3日以内に・2週間以内に"),
    "yasashii":  ("やさしい にほんご", "かんたんな 日本語の ページ"),
}


def esc(s):
    """構造化データに入れる前にタグと引用符を落とす。"""
    out = []
    skip = False
    for ch in s:
        if ch == "<":
            skip = True
        elif ch == ">":
            skip = False
        elif not skip:
            out.append(ch)
    return "".join(out).replace("　", " ").strip()


TMPL = """<!doctype html>
<html lang="ja">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<title>{title}｜令和8年熊本地震 情報まとめ</title>
<meta name="description" content="{desc}">
<link rel="canonical" href="{site}/{slug}/">
<link rel="icon" href="/favicon.svg" type="image/svg+xml">
<meta name="theme-color" content="#a62018">
<meta name="robots" content="index,follow,max-image-preview:large,max-snippet:-1">
<meta property="og:type" content="article">
<meta property="og:url" content="{site}/{slug}/">
<meta property="og:site_name" content="{sitename}">
<meta property="og:title" content="{title}">
<meta property="og:description" content="{desc}">
<meta property="og:image" content="{site}/og.png">
<meta property="og:locale" content="ja_JP">
<meta name="twitter:card" content="summary_large_image">
<meta name="twitter:title" content="{title}">
<meta name="twitter:description" content="{desc}">
<meta name="twitter:image" content="{site}/og.png">
<style>
  :root{{--ink:#1c1f23;--ink-2:#3d434a;--ink-3:#6b7480;--line:#e2e6ea;
    --bg:#f6f7f9;--surface:#fff;--accent:#0d6b52;--accent-soft:#e8f3ef;--red:#b3261e}}
  *{{box-sizing:border-box}}
  body{{margin:0;background:var(--bg);color:var(--ink);line-height:1.95;
    font-family:-apple-system,BlinkMacSystemFont,"Hiragino Sans","Noto Sans JP",sans-serif;
    font-size:16px;-webkit-text-size-adjust:100%}}
  .wrap{{max-width:720px;margin:0 auto;padding:0 16px 40px}}
  header.hd{{background:#fff;border-bottom:1px solid var(--line);padding:14px 0}}
  header.hd .in{{max-width:720px;margin:0 auto;padding:0 16px}}
  header.hd a.home{{color:var(--accent);font-weight:700;text-decoration:none;font-size:.9rem}}
  .warn{{background:#fdecea;border-bottom:1px solid #f5c6c2;color:#8c1d18;font-size:.84rem;padding:10px 0}}
  .warn .in{{max-width:720px;margin:0 auto;padding:0 16px}}
  h1{{font-size:1.42rem;line-height:1.65;margin:24px 0 8px}}
  .lead{{color:var(--ink-2);font-size:.95rem;margin:0 0 6px}}
  .stamp{{color:var(--ink-3);font-size:.78rem;margin:0 0 18px}}
  h2{{font-size:1.07rem;line-height:1.6;margin:30px 0 8px;padding-left:11px;
    border-left:4px solid var(--accent);scroll-margin-top:12px}}
  h3{{font-size:.97rem;margin:20px 0 6px}}
  .card{{background:var(--surface);border:1px solid var(--line);border-radius:12px;
    padding:14px 16px;margin:0 0 14px;font-size:.93rem;color:var(--ink-2)}}
  .toc{{background:var(--surface);border:1px solid var(--line);border-radius:12px;
    padding:14px 16px;margin:0 0 20px;font-size:.9rem}}
  .toc b{{display:block;margin-bottom:6px;font-size:.88rem}}
  .toc ol{{margin:0;padding-left:1.3em;color:var(--ink-2)}}
  .toc li{{margin:3px 0}}
  .toc a{{color:var(--ink-2)}}
  .cta{{display:block;background:var(--accent);color:#fff;text-align:center;font-weight:700;
    text-decoration:none;border-radius:12px;padding:15px;margin:24px 0;font-size:1rem}}
  .cta span{{display:block;font-weight:400;font-size:.79rem;opacity:.92;margin-top:3px}}
  .faq{{background:var(--surface);border:1px solid var(--line);border-radius:12px;
    padding:4px 16px;margin:0 0 14px}}
  .faq dt{{font-weight:700;font-size:.93rem;margin:14px 0 4px}}
  .faq dd{{margin:0 0 14px;font-size:.91rem;color:var(--ink-2)}}
  nav.other{{margin:28px 0 0}}
  nav.other ul{{list-style:none;padding:0;margin:0}}
  nav.other li{{border-top:1px solid var(--line)}}
  nav.other a{{display:block;padding:11px 2px;text-decoration:none;color:var(--accent);
    font-weight:700;font-size:.93rem}}
  nav.other a small{{display:block;color:var(--ink-3);font-weight:400;font-size:.8rem;margin-top:1px}}
  footer{{border-top:1px solid var(--line);margin-top:30px;padding:18px 0 0;
    font-size:.78rem;color:var(--ink-3)}}
  footer a{{color:var(--ink-3)}}
  b{{color:var(--ink)}}
</style>
<script type="application/ld+json">{jsonld}</script>
</head>
<body>
<div class="warn"><div class="in">
  <b>有志が運営する非公式サイトです。</b>公的機関ではありません。数字や場所は必ずリンク先の公式ページでご確認ください。
</div></div>
<header class="hd"><div class="in">
  <a class="home" href="/">← 令和8年熊本地震 情報まとめ</a>
</div></header>

<main class="wrap">
  <h1>{h1}</h1>
  <p class="lead">{lead}</p>
  <p class="stamp">最終更新：{today}／令和8年熊本地震（2026年7月28日）</p>

  <a class="cta" href="/#{tab}">最新の情報を見る（{tablabel}）
    <span>{ctanote}</span></a>

  <div class="toc"><b>このページの内容</b>
    <ol>
{toc}
    </ol>
  </div>

{body}

  <h2 id="faq">よくある質問</h2>
  <dl class="faq">
{faqhtml}
  </dl>

  <a class="cta" href="/#{tab}">最新の情報を見る（{tablabel}）
    <span>{ctanote}</span></a>

  <nav class="other">
    <h2>ほかのページ</h2>
    <ul>
{others}
    </ul>
  </nav>

  <footer>
    <p><b>このページには、日ごとに変わる数字を載せていません。</b>
      給水所の場所と時間、避難所の開設状況、仮置場の待ち時間、ボランティアの受入状況は毎日変わるため、
      本体サイトの該当タブでご確認ください。古い数字が検索結果に残って、
      読んだ方を誤らせないようにするためです。</p>
    <p>{sitename}／
      <a href="/">kumamotojishin.jp</a>　運営とお問い合わせ：info@kumamotojishin.jp</p>
  </footer>
</main>
</body>
</html>
"""


def build(out_dir):
    os.makedirs(out_dir, exist_ok=True)
    today = dt.datetime.now(JST).strftime("%Y-%m-%d")
    made, stats = [], []

    for slug, tab, title, desc, h1, lead, ctanote, blocks, faqs in PAGES:
        # 本文
        body_parts, toc_parts = [], []
        for i, (h, t) in enumerate(blocks, 1):
            aid = f"s{i}"
            body_parts.append(f'  <h2 id="{aid}">{h}</h2>\n  <div class="card">{t}</div>')
            toc_parts.append(f'      <li><a href="#{aid}">{esc(h)}</a></li>')
        toc_parts.append('      <li><a href="#faq">よくある質問</a></li>')

        faq_parts = [f"    <dt>{q}</dt>\n    <dd>{a}</dd>" for q, a in faqs]

        others = []
        for s2, _, _, _, _, _, _, _, _ in PAGES:
            if s2 == slug:
                continue
            lbl, note = NAV_NOTE[s2]
            others.append(f'      <li><a href="/{s2}/">{lbl}<small>{note}</small></a></li>')

        # 構造化データ：パンくず・記事・FAQ
        url = f"{SITE}/{slug}/"
        jsonld = [
            {"@context": "https://schema.org", "@type": "BreadcrumbList",
             "itemListElement": [
                 {"@type": "ListItem", "position": 1, "name": "令和8年熊本地震 情報まとめ", "item": SITE + "/"},
                 {"@type": "ListItem", "position": 2, "name": esc(title), "item": url},
             ]},
            {"@context": "https://schema.org", "@type": "Article",
             "headline": esc(title), "description": esc(desc),
             "inLanguage": "ja", "datePublished": "2026-08-05", "dateModified": today,
             "mainEntityOfPage": {"@type": "WebPage", "@id": url},
             "isPartOf": {"@type": "WebSite", "name": SITE_NAME, "url": SITE + "/"},
             "about": {"@type": "Event", "name": "令和8年熊本地震", "startDate": "2026-07-28"},
             "publisher": {"@type": "Organization", "name": SITE_NAME, "url": SITE + "/"}},
            {"@context": "https://schema.org", "@type": "FAQPage",
             "mainEntity": [
                 {"@type": "Question", "name": esc(q),
                  "acceptedAnswer": {"@type": "Answer", "text": esc(a)}}
                 for q, a in faqs]},
        ]

        html = TMPL.format(
            site=SITE, sitename=SITE_NAME, slug=slug, title=title, desc=desc,
            h1=h1, lead=lead, ctanote=ctanote, today=today,
            tab=tab, tablabel=TAB_LABEL.get(tab, "本体サイト"),
            toc="\n".join(toc_parts), body="\n".join(body_parts),
            faqhtml="\n".join(faq_parts), others="\n".join(others),
            jsonld=json.dumps(jsonld, ensure_ascii=False, separators=(",", ":")),
        )

        d = os.path.join(out_dir, slug)
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, "index.html"), "w", encoding="utf-8") as f:
            f.write(html)
        made.append(slug)

        chars = sum(len(esc(h)) + len(esc(t)) for h, t in blocks)
        chars += sum(len(esc(q)) + len(esc(a)) for q, a in faqs)
        stats.append((slug, len(blocks), len(faqs), chars, len(html)))

    urls = "".join(
        f"  <url>\n    <loc>{SITE}/{s}/</loc>\n    <lastmod>{today}</lastmod>\n"
        f"    <changefreq>weekly</changefreq>\n    <priority>0.8</priority>\n  </url>\n"
        for s in made)
    sitemap = ('<?xml version="1.0" encoding="UTF-8"?>\n'
               '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">\n'
               f'  <url>\n    <loc>{SITE}/</loc>\n    <lastmod>{today}</lastmod>\n'
               '    <changefreq>hourly</changefreq>\n    <priority>1.0</priority>\n  </url>\n'
               f'{urls}</urlset>\n')
    with open(os.path.join(out_dir, "sitemap.xml"), "w", encoding="utf-8") as f:
        f.write(sitemap)
    return made, stats


if __name__ == "__main__":
    out = sys.argv[1] if len(sys.argv) > 1 else "site"
    made, stats = build(out)
    print(f"{len(made)}ページ生成、sitemap.xml に {len(made)+1} URL\n")
    print(f"{'slug':<11}{'節':>4}{'Q&A':>5}{'本文字数':>10}{'HTML':>9}")
    for s, nb, nf, ch, hb in stats:
        print(f"{s:<11}{nb:>4}{nf:>5}{ch:>10,}{hb:>9,}")
    print(f"\n合計 本文 {sum(x[3] for x in stats):,} 字")
