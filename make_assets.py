#!/usr/bin/env python3
"""告知用の素材をつくる。

  site/og.png        SNSやLINEでURLを共有したときに出るカード画像（1200×630）
  site/favicon.svg   ブラウザのタブに出るアイコン
  poster_a4.pdf      避難所に貼るA4のポスター（QRコード入り・白黒印刷でも読める）
  poster_a4.png      同じものの画像版

  python3 make_assets.py
"""
import os
from PIL import Image, ImageDraw, ImageFont
import qrcode
from qrcode.constants import ERROR_CORRECT_H

URL = "https://kumamotojishin.jp/"
SHOW = "kumamotojishin.jp"
BOLD = "/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc"
REG = "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc"
if not os.path.exists(REG):
    REG = "/usr/share/fonts/truetype/fonts-japanese-gothic.ttf"
if not os.path.exists(BOLD):
    BOLD = REG

INK = (21, 23, 28)
INK2 = (71, 78, 92)
INK3 = (109, 116, 132)
RED = (166, 32, 24)
GREEN = (15, 107, 92)
LINE = (210, 214, 220)


def f(path, size):
    try:
        return ImageFont.truetype(path, size, index=0)
    except Exception:
        return ImageFont.truetype(REG, size)


def wrap(draw, text, font, width):
    """日本語は単語で切れないので1文字ずつ測って折り返す。"""
    out, cur = [], ""
    for ch in text:
        if ch == "\n":
            out.append(cur); cur = ""; continue
        t = cur + ch
        if draw.textlength(t, font=font) > width and cur:
            out.append(cur); cur = ch
        else:
            cur = t
    if cur:
        out.append(cur)
    return out


# ------------------------------------------------------------------ OG画像
def make_og(path="site/og.png"):
    W, H = 1200, 630
    im = Image.new("RGB", (W, H), "white")
    d = ImageDraw.Draw(im)

    d.rectangle([0, 0, W, 14], fill=RED)

    d.text((64, 74), "令和8年熊本地震", font=f(BOLD, 78), fill=INK)
    d.text((64, 168), "情報まとめ", font=f(BOLD, 78), fill=RED)

    d.text((64, 288), "被災された方・支援したい方のための、公式情報リンク集", font=f(REG, 33), fill=INK2)

    items = [
        ("給水所・避難所の地図", GREEN),
        ("断水・停電・給油・道路", GREEN),
        ("罹災証明・支援制度", GREEN),
        ("ボランティア・寄付", GREEN),
    ]
    x, y = 64, 358
    fi = f(BOLD, 30)
    for t, c in items:
        w = d.textlength(t, font=fi) + 40
        if x + w > W - 64:
            x = 64; y += 62
        d.rounded_rectangle([x, y, x + w, y + 50], 10, fill=(240, 246, 244), outline=(198, 224, 217))
        d.text((x + 20, y + 8), t, font=fi, fill=c)
        x += w + 12

    d.line([64, 500, W - 64, 500], fill=LINE, width=2)
    d.text((64, 522), SHOW, font=f(BOLD, 52), fill=INK)
    d.text((64, 588), "有志による非公式サイト（公的機関ではありません）／運営：熊本電力合同会社",
           font=f(REG, 22), fill=INK3)

    os.makedirs(os.path.dirname(path), exist_ok=True)
    im.save(path, optimize=True)
    print(path, im.size, os.path.getsize(path), "bytes")


# ------------------------------------------------------------------ favicon
def make_favicon(path="site/favicon.svg"):
    svg = (
        '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 64 64">'
        '<rect width="64" height="64" rx="12" fill="#a62018"/>'
        '<path d="M32 13c-7.7 0-14 6.3-14 14 0 10.5 14 24 14 24s14-13.5 14-24c0-7.7-6.3-14-14-14z" fill="#fff"/>'
        '<circle cx="32" cy="27" r="5.6" fill="#a62018"/>'
        "</svg>"
    )
    open(path, "w", encoding="utf-8").write(svg)
    print(path, os.path.getsize(path), "bytes")


# ------------------------------------------------------------------ A4ポスター
def make_poster(png="poster_a4.png", pdf="poster_a4.pdf"):
    DPI = 300
    W, H = int(210 / 25.4 * DPI), int(297 / 25.4 * DPI)   # A4縦 2480×3507
    im = Image.new("RGB", (W, H), "white")
    d = ImageDraw.Draw(im)
    M = 150                                               # 左右の余白

    # 上の帯
    d.rectangle([0, 0, W, 190], fill=RED)
    d.text((M, 50), "令和8年熊本地震", font=f(BOLD, 82), fill="white")

    # 見出し
    d.text((M, 260), "断水・給水・避難所の", font=f(BOLD, 124), fill=INK)
    d.text((M, 410), "情報はこちら", font=f(BOLD, 124), fill=INK)

    # QRコード。誤り訂正を最高にして、汚れや折れでも読めるようにする
    q = qrcode.QRCode(version=None, error_correction=ERROR_CORRECT_H, box_size=10, border=2)
    q.add_data(URL)
    q.make(fit=True)
    qi = q.make_image(fill_color="black", back_color="white").convert("RGB")
    QS = 980
    qi = qi.resize((QS, QS), Image.NEAREST)
    qy, qx = 610, (W - QS) // 2
    d.rounded_rectangle([qx - 34, qy - 34, qx + QS + 34, qy + QS + 34], 26, outline=INK, width=6)
    im.paste(qi, (qx, qy))

    y = qy + QS + 100
    t = "カメラで読み取ってください"
    d.text(((W - d.textlength(t, font=f(REG, 58))) / 2, y), t, font=f(REG, 58), fill=INK2)

    # URL
    y += 110
    d.rounded_rectangle([M, y, W - M, y + 190], 22, fill=(245, 246, 248), outline=LINE, width=4)
    u, fu = SHOW, f(BOLD, 112)
    while d.textlength(u, font=fu) > W - 2 * M - 90:
        fu = f(BOLD, fu.size - 4)
    d.text(((W - d.textlength(u, font=fu)) / 2, y + 38), u, font=fu, fill=INK)

    # ローマ字が割れる話
    y += 232
    fn = f(REG, 50)
    for ln in wrap(d, "「じしん」のローマ字は jishin・zishin・jisin・zisin と分かれます。"
                      "口で伝えると別のサイトに行ってしまうことがあるため、QRコードで読み取ってください。",
                   fn, W - 2 * M):
        d.text((M, y), ln, font=fn, fill=INK2)
        y += 70

    # 中身
    y += 46
    d.text((M, y), "のせている情報", font=f(BOLD, 62), fill=GREEN)
    y += 96
    fl = f(REG, 54)
    for t in ["市町村ごとの給水所・避難所（地図と電話番号）",
              "断水・停電・給油所・道路の通行止め",
              "罹災証明の申請と、使える支援制度",
              "ボランティアと寄付の受付状況",
              "気象庁の地震情報（自動で更新）"]:
        d.ellipse([M + 10, y + 20, M + 30, y + 40], fill=GREEN)
        d.text((M + 56, y), t, font=fl, fill=INK)
        y += 82

    # 緊急。footer の位置から逆算して置き、はみ出さないようにする
    FT = H - 250
    by = FT - 250
    d.rounded_rectangle([M, by, W - M, by + 190], 18, fill=(253, 236, 234), outline=RED, width=5)
    d.text((M + 46, by + 26), "命の危険があるときは 119 / 110 へ", font=f(BOLD, 68), fill=RED)
    d.text((M + 46, by + 118), "このサイトでは救助要請を受け付けられません。", font=f(REG, 44), fill=RED)

    # footer
    d.line([M, FT, W - M, FT], fill=LINE, width=3)
    for i, ln in enumerate([
        "有志が運営する非公式サイトです。公的機関ではありません。",
        "掲載内容はすべて国・県・市町村の公式ページへのリンクです。",
        "運営：熊本電力合同会社（熊本県菊陽町）／ 誤りのご指摘：info@kumamotojishin.jp",
    ]):
        d.text((M, FT + 40 + i * 60), ln, font=f(REG, 40), fill=INK3)

    im.save(png, dpi=(DPI, DPI), optimize=True)
    im.save(pdf, "PDF", resolution=DPI)
    print(png, im.size, os.path.getsize(png), "bytes  最後の項目の下端 y=%d / 緊急枠 %d / footer %d" % (y, by, FT))
    print(pdf, os.path.getsize(pdf), "bytes")


# ------------------------------------------------------------------ robots / sitemap
def make_robots(path="site/robots.txt"):
    open(path, "w", encoding="utf-8").write(
        "# 令和8年熊本地震 情報まとめ（非公式）\n"
        "User-agent: *\n"
        "Allow: /\n"
        "\n"
        "Sitemap: https://kumamotojishin.jp/sitemap.xml\n"
    )
    print(path)


def make_sitemap(path="site/sitemap.xml", lastmod=None):
    import datetime
    lm = lastmod or datetime.datetime.now(
        datetime.timezone(datetime.timedelta(hours=9))).strftime("%Y-%m-%d")
    open(path, "w", encoding="utf-8").write(
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">\n'
        "  <url>\n"
        "    <loc>https://kumamotojishin.jp/</loc>\n"
        "    <lastmod>%s</lastmod>\n"
        "    <changefreq>hourly</changefreq>\n"
        "    <priority>1.0</priority>\n"
        "  </url>\n"
        "</urlset>\n" % lm
    )
    print(path)


if __name__ == "__main__":
    make_og()
    make_favicon()
    make_robots()
    make_sitemap()
    make_poster()
