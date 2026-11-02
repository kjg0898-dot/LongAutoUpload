"""썸네일 자동 만들기: 채널 틀 + 재료 사진 + 사용자가 쓴 문구 (+ 위키미디어 공용 실제 사진).
글자는 AI가 그리지 않고 폰트로 찍는다 (한글이 깨지지 않게). 채널 마크는 항상 같은 파일을 같은 자리에 그대로 붙인다.
매번 조금씩 다르게: 글씨 색 조합·글자 쪽(왼/오른)·크기·사진 기울기가 틀이 정한 범위 안에서 바뀐다."""
import io
import csv
import json
import os
import random
import re
import sys
import threading
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter, ImageFont, ImageStat

import core

TEMPLATE_DIR = core.PROG / "썸네일틀"
SIZE = (1280, 720)

# 틀.json이 없을 때 쓰는 기본 틀. 박스는 [x, y, 너비, 높이], 글자가 '왼쪽'일 때 기준 (오른쪽은 좌우를 뒤집어 씀)
DEFAULT_TEMPLATE = {
    "font": "",  # 틀 폴더의 폰트 파일 이름. 비우면 맑은 고딕 굵게
    "text": {"box": [56, 150, 700, 470], "valign": "center", "size_max": 150, "size_min": 54, "max_lines": 3,
             "stroke": 12, "line_gap": 0.06, "sides": ["left", "right"]},
    "styles": [{"fill": "#FFFFFF", "accent": "#FFE400", "stroke": "#000000"},
               {"fill": "#FFE400", "accent": "#FF3B30", "stroke": "#000000"},
               {"fill": "#FFFFFF", "accent": "#FF5A1F", "stroke": "#111111"}],
    "shade": 0.6,  # 글자 쪽을 어둡게 (0~1)
    "inset": {"box": [850, 70, 370, 270], "border": 8, "border_color": "#FFFFFF", "tilt": 3},
    "logo": {"file": "로고.png", "pos": [40, 28], "width": 0},  # width 0이면 로고 파일 크기 그대로
    "jitter": 0.04,  # 글자 크기·위치를 이만큼 안에서 매번 조금씩 바꿈
}
WINDOWS_FONTS = [Path("C:/Windows/Fonts/malgunbd.ttf"), Path("C:/Windows/Fonts/malgun.ttf")]
OTHER_FONTS = [Path("/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc"), Path("/System/Library/Fonts/AppleSDGothicNeo.ttc")]


def template_dir(ch):
    return TEMPLATE_DIR / ch["folder"]


def load_template(ch):
    """채널 틀: 기본 틀 위에 썸네일틀/<채널 폴더>/틀.json 을 덮어쓴다."""
    tpl = json.loads(json.dumps(DEFAULT_TEMPLATE))
    path = template_dir(ch) / "틀.json"
    if path.exists():
        for k, v in json.loads(core.read_text(path)).items():
            tpl[k] = tpl[k] | v if isinstance(v, dict) and isinstance(tpl.get(k), dict) else v
    return tpl


def fonts(ch, tpl):
    """고를 수 있는 글씨체들: 틀 폴더에 넣은 폰트들, 없으면 틀이 정한 폰트들 (같은 파일이 여러 번이면 더 자주)."""
    own = [p for p in sorted(template_dir(ch).glob("*")) if p.suffix.lower() in (".ttf", ".otf", ".ttc")]
    given = [template_dir(ch) / f for f in (tpl.get("fonts") or ([tpl["font"]] if tpl.get("font") else []))]
    return own or [p for p in given if p.exists()]


def font_path(ch, tpl, rnd=None, pick=None):
    """글씨체: 틀 폴더에 넣은 폰트들 → 틀이 정한 폰트들(프로그램과 함께 오는 무료 폰트) → 맑은 고딕 굵게.
    여럿이면 pick(이름)이 있으면 그것, 없으면 매번 하나를 골라 비슷하지만 조금씩 다르게."""
    group = fonts(ch, tpl)
    if group:
        return next((p for p in group if p.name == pick), None) or (rnd or random).choice(group)
    for p in WINDOWS_FONTS if sys.platform == "win32" else OTHER_FONTS:
        if p.exists():
            return p
    raise FileNotFoundError("한글 폰트를 못 찾았습니다. 썸네일틀/<채널 폴더>/ 에 폰트 파일(.ttf/.otf)을 넣어주세요.")


def missing_glyphs(font, text):
    """폰트에 없는 글자 (그대로 찍으면 □로 깨짐). 없는 글자 모양(.notdef)과 똑같이 그려지면 없는 것."""
    def shape(ch):
        m = font.getmask(ch)
        return m.size, bytes(m)
    notdef = shape("\U000F0000")  # 개인용 영역 글자: 어떤 폰트에도 없음
    return sorted({c for c in text if not c.isspace() and shape(c) == notdef})


def _image(x):
    return (x if isinstance(x, Image.Image) else Image.open(x)).convert("RGB")


def _cover(img, w, h):
    """비율을 지키며 꽉 채우고 넘치는 쪽을 가운데 기준으로 자른다."""
    s = max(w / img.width, h / img.height)
    img = img.resize((max(w, round(img.width * s)), max(h, round(img.height * s))), Image.LANCZOS)
    x, y = (img.width - w) // 2, (img.height - h) // 2
    return img.crop((x, y, x + w, y + h))


def _spot(entry, i=0):
    """틀의 사진 자리 하나: (이름, 자리 [x,y,w,h], 붙는 쪽). 예전 틀처럼 [x,y,w,h]만 있어도 된다.
    붙는 쪽: tr 오른쪽 위, tl 왼쪽 위, bl 왼쪽 아래, br 오른쪽 아래, t 위 가운데 (사진이 자리보다 작으면 그쪽에 붙임)"""
    if isinstance(entry, dict):
        return entry.get("name", f"{i}번"), entry["box"], entry.get("anchor", "tr")
    return f"{i}번", entry, "tr"


def _spots(tpl):
    inset = tpl.get("inset")
    return (inset.get("boxes") or [inset["box"]]) if inset else []


MIN_PHOTO = 110_000  # 실제 사진 최소 넓이(px²): 화면의 약 12%. 이보다 작으면 썸네일이 작게 보일 때 무엇인지 안 보임


def _place(region, anchor, photo_ratio, size):
    """자리 안에 사진 비율대로 (세로 사진은 세로로 길게, 가로 사진은 넓게) size배 크기로 놓을 칸 [x,y,w,h]."""
    rx, ry, rw, rh = region
    ar = min(1.9, max(0.75, photo_ratio))  # 너무 길쭉한 사진은 이만큼만 잘라 맞춤
    w = rw * size
    h = w / ar
    if h > rh * size:
        h = rh * size
        w = h * ar
    grow = min((MIN_PHOTO / (w * h)) ** 0.5, rw / w, rh / h)  # 휴대폰 작은 화면에서도 알아보게 최소 크기까지 (자리 안에서)
    if grow > 1:
        w, h = w * grow, h * grow
    x = rx + rw - w if anchor in ("tr", "br") else rx + (rw - w) / 2 if anchor == "t" else rx
    y = ry + rh - h if anchor in ("bl", "br") else ry
    return [round(x), round(y), round(w), round(h)]


def _box_xyxy(box):
    x, y, w, h = box
    return (x, y, x + w, y + h)


def _mirror(box, side):
    x, y, w, h = box
    return [SIZE[0] - x - w, y, w, h] if side == "right" else [x, y, w, h]


def _segments(line, accents):
    """한 줄을 [(글자, 강조?)] 로. 강조할 말은 줄 안에 그대로 있는 것만."""
    marks = [False] * len(line)
    for a in accents:
        a = a.strip()
        start = line.find(a) if a else -1
        while start >= 0:
            marks[start:start + len(a)] = [True] * len(a)
            start = line.find(a, start + len(a))
    out = []
    for ch, m in zip(line, marks):
        if out and out[-1][1] == m:
            out[-1][0] += ch
        else:
            out.append([ch, m])
    return out


def _even(lines):
    """줄 길이가 고른 정도: 가장 짧은 줄 ÷ 가장 긴 줄 (한 줄이면 1)."""
    n = [len(s.replace(" ", "")) for s in lines if s.strip()] or [1]
    return min(n) / max(n)


def _squash(lines):
    return re.sub(r"[\s/]+", "", "".join(lines))


def _split(line):
    """가운데에 가까운 띄어쓰기에서 두 줄로."""
    spaces = [i for i, ch in enumerate(line) if ch == " "]
    cut = min(spaces, key=lambda i: abs(i - len(line) / 2))
    return [line[:cut].strip(), line[cut:].strip()]


def _fit(lines, font_file, box, t, stroke):
    """박스에 들어가는 가장 큰 글자 크기."""
    for size in range(t["size_max"], t["size_min"] - 1, -2):
        font = ImageFont.truetype(str(font_file), size)
        widths = [font.getbbox(line, stroke_width=stroke)[2] for line in lines]
        height = size * len(lines) * (1 + t["line_gap"]) + stroke * 2
        if max(widths) <= box[2] and height <= box[3]:
            return font
    raise ValueError(f"문구가 너무 깁니다 (가장 작은 글자 {t['size_min']}로도 안 들어감). 줄을 '/'로 나누거나 줄여주세요.")


def compose(ch, base, text, plan=None, photo=None, rnd=None):
    """썸네일 그림. plan: {'side': 'left'|'right'|'center', 'accents': [...]} (없으면 무작위·마지막 줄 강조).
    틀에서 고를 수 있는 것: 글자 가운데 정렬(sides ["center"]), 줄마다 색(styles.line_fills), 바깥 테두리(border),
    아래쪽 그늘(shade_dir "bottom"), 실제 사진 자리 여러 개(inset.boxes, 매번 하나), 기본 로고(로고_기본.png)."""
    rnd = rnd or random.Random()
    tpl, plan = load_template(ch), plan or {}
    t = dict(tpl["text"])
    lines = [s.strip() for s in (text or "").replace("\\n", "/").split("/") if s.strip()]  # 비면 글자 없이 (후보 미리보기)
    if plan.get("lines") and _squash(plan["lines"]) == _squash(lines) and _even(plan["lines"]) >= min(_even(lines), 0.6):
        # Claude가 나눈 줄: 글자는 그대로이고, 한 줄만 너무 짧지 않을 때만
        lines = [s.strip() for s in plan["lines"] if s.strip()]
    if len(lines) > t["max_lines"]:
        raise ValueError(f"썸네일 문구가 {len(lines)}줄입니다 (이 채널은 {t['max_lines']}줄까지).")
    layouts, spots_ok = tpl.get("layouts"), None
    if layouts:  # 글자 자리가 여럿인 틀: Claude가 고른 자리, 없으면 앞서 쓴 자리를 피해 무게대로
        lay = next((x for x in layouts if x["name"] == plan.get("layout")), None)
        if not lay:
            pool = [x for x in layouts if x["name"] not in plan.get("avoid", [])] or layouts
            lay = rnd.choices(pool, weights=[x.get("weight", 1) * plan.get("boost", {}).get(x["name"], 1) for x in pool])[0]
        t.update({k: lay[k] for k in ("box", "valign") if k in lay})
        t["sides"], spots_ok = [lay.get("align", "center")], lay.get("spots")
        plan = plan | {"side": t["sides"][0], "layout": lay["name"]}
    if isinstance(plan.get("size"), (int, float)):  # 글자 크기 조금씩 (0.85~1.1배)
        f = min(1.1, max(0.85, float(plan["size"])))
        t["size_max"], t["size_min"] = int(t["size_max"] * f), int(t["size_min"] * min(f, 1))
    font_file = font_path(ch, tpl, rnd, plan.get("font"))
    lost = missing_glyphs(ImageFont.truetype(str(font_file), 40), "".join(lines))
    if lost:
        raise ValueError(f"폰트({font_file.name})에 없는 글자가 있어 깨집니다: {' '.join(lost)} — 문구에서 빼거나 다른 폰트를 쓰세요.")
    side = plan.get("side") if plan.get("side") in t["sides"] else rnd.choice(t["sides"])
    si = plan["style"] if isinstance(plan.get("style"), int) and 0 <= plan["style"] < len(tpl["styles"]) \
        else rnd.randrange(len(tpl["styles"]))
    style = tpl["styles"][si]
    jit = tpl.get("jitter", 0)
    mirror = not layouts  # 글자 자리가 정해진 틀은 좌우를 뒤집지 않는다

    canvas = _cover(_image(base), *SIZE).convert("RGBA")
    if tpl.get("shade"):  # 글자 쪽으로 갈수록 어두워지는 그늘 (글자가 잘 읽히게)
        way = (lay.get("shade") if layouts else None) or ("bottom" if tpl.get("shade_dir") == "bottom" or side == "center" else side)
        vertical = way in ("bottom", "top")
        shade = Image.new("L", SIZE)
        n = SIZE[1] if vertical else SIZE[0]
        for i in range(n):
            k = i / n if way in ("bottom", "right") else 1 - i / n
            band = (0, i, SIZE[0], i + 1) if vertical else (i, 0, i + 1, SIZE[1])
            shade.paste(int(255 * tpl["shade"] * max(0.0, k * 1.6 - 0.6)), band)
        canvas = Image.composite(Image.new("RGBA", SIZE, (0, 0, 0, 255)), canvas, shade)

    logo = tpl.get("logo") or {}
    logo_file = next((f for f in (template_dir(ch) / logo.get("file", ""), template_dir(ch) / "로고_기본.png")
                      if logo.get("file") and f.is_file()), None)  # 내가 넣은 로고.png가 먼저, 없으면 채널에서 따 온 기본 로고
    mark = None
    if logo_file:
        mark = Image.open(logo_file).convert("RGBA")
        if logo.get("width"):
            mark = mark.resize((logo["width"], round(mark.height * logo["width"] / mark.width)), Image.LANCZOS)

    inset = tpl.get("inset")
    spots = _spots(tpl)
    allowed = [i for i in range(len(spots)) if spots_ok is None or i in spots_ok]  # 글자 자리에 맞는 사진 자리
    photo_info = None
    if photo and allowed:  # 실제 사진: 글자를 피한 자리에 사진 비율대로, 크기도 편마다 (틀에 따라 흰 테두리·그림자·기울임)
        src = _image(photo)
        size = plan["photo_size"] if isinstance(plan.get("photo_size"), (int, float)) else rnd.uniform(0.8, 1.0)
        size = min(1.0, max(0.7, float(size)))
        fits = [k for k in allowed if (lambda b: b[2] * b[3])(_place(*_spot(spots[k], k)[1:], src.width / src.height, 1.0))
                >= 0.9 * MIN_PHOTO]  # 이 사진 비율로 넣으면 너무 작아지는 자리는 뺌 (세로 사진 + 작은 자리)
        allowed = fits or allowed
        if plan.get("spot") in allowed:  # Claude가 배경을 보고 고른 자리
            i = plan["spot"]
        else:  # 없으면 배경이 덜 복잡한 곳(하늘·땅 등, 얼굴·주인공이 아닐 가능성이 큰 곳)을 더 자주
            edges = canvas.convert("L").filter(ImageFilter.FIND_EDGES)
            busy = [ImageStat.Stat(edges.crop(_box_xyxy(_place(_spot(spots[k], k)[1], _spot(spots[k], k)[2],
                                                                src.width / src.height, size)))).mean[0] for k in allowed]
            i = rnd.choices(allowed, weights=[1 / (b + 4) ** 2 for b in busy])[0]
        spot_name, region, anchor = _spot(spots[i], i)
        spot = _place(region, anchor, src.width / src.height, size)
        x, y, w, h = _mirror(spot, side) if mirror and side != "center" else spot  # 사진 칸은 '글자 왼쪽' 기준
        photo_info = {"spot": spot_name, "size": round(size, 2), "box": [x, y, w, h]}
        if mark:  # 채널 마크를 가리지 않게 마크 아래로
            lx, ly = logo["pos"]
            if x < lx + mark.width and lx < x + w and y < ly + mark.height and ly < y + h:
                y = ly + mark.height + 24
        pic = _cover(src, w, h)
        b = inset.get("border", 0)
        framed = Image.new("RGBA", (w + 2 * b, h + 2 * b), inset.get("border_color", "#FFFFFF"))
        framed.paste(pic, (b, b))
        framed = framed.rotate(rnd.uniform(-1, 1) * inset.get("tilt", 0), expand=True, resample=Image.BICUBIC)
        if inset.get("shadow", True):
            shadow = Image.new("RGBA", framed.size, (0, 0, 0, 0))
            shadow.paste((0, 0, 0, 150), mask=framed.split()[3])
            canvas.alpha_composite(shadow.filter(ImageFilter.GaussianBlur(10)), (x - b + 10, y - b + 12))
        canvas.alpha_composite(framed, (x - b, y - b))

    box = [round(v * (1 + rnd.uniform(-jit, jit))) if i < 2 else v for i, v in enumerate(t["box"])]
    box = _mirror(box, side) if mirror and side != "center" else box
    stroke = t["stroke"]
    font = None
    while lines:  # 안 들어가면 가장 긴 줄을 가운데 가까운 띄어쓰기에서 나눠 다시 (/ 없이 쓴 긴 문구)
        try:
            font = _fit(lines, font_file, box, t, stroke)
            break
        except ValueError:
            longest = max(range(len(lines)), key=lambda i: len(lines[i]))
            spaces = [i for i, ch in enumerate(lines[longest]) if ch == " "]
            if len(lines) >= t["max_lines"] or not spaces:
                raise
            lines[longest:longest + 1] = _split(lines[longest])
    while font and len(lines) < t["max_lines"]:  # 두 줄로 나누면 글자가 꽤 커지면 나눈다 (큰 글씨 두 줄이 기본인 채널)
        longest = max(range(len(lines)), key=lambda i: len(lines[i]))
        if " " not in lines[longest]:
            break
        trial = lines[:longest] + _split(lines[longest]) + lines[longest + 1:]
        try:
            bigger = _fit(trial, font_file, box, t, stroke)
        except ValueError:
            break
        if bigger.size < font.size * 1.15:
            break
        lines, font = trial, bigger
    glow = t.get("glow", 0)
    layer = Image.new("RGBA", SIZE, (0, 0, 0, 0)) if glow and lines else None
    draw = ImageDraw.Draw(layer or canvas)
    step = font.size * (1 + t["line_gap"]) if font else 0
    total = step * len(lines)
    y = box[1] + {"top": 0, "bottom": box[3] - total}.get(t["valign"], (box[3] - total) / 2)
    fills = style.get("line_fills")  # 줄마다 색 (예: 첫 줄 흰색, 둘째 줄 노랑). 있으면 강조는 Claude가 고른 말만
    accents = plan.get("accents") or ([] if fills else lines[-1:])
    for n, line in enumerate(lines):
        width = font.getbbox(line, stroke_width=stroke)[2]
        x = {"left": box[0], "center": box[0] + (box[2] - width) / 2}.get(side, box[0] + box[2] - width)
        base_fill = fills[min(n, len(fills) - 1)] if fills else style["fill"]
        for seg, accent in _segments(line, accents):
            if layer:  # 번짐용: 굵은 검은 글자를 따로 그려 흐리게
                draw.text((x, y + 4), seg, font=font, fill=(0, 0, 0, 230), stroke_width=stroke + glow // 2, stroke_fill=(0, 0, 0, 230))
            else:
                draw.text((x + 6, y + 8), seg, font=font, fill=(0, 0, 0, 140), stroke_width=stroke, stroke_fill=(0, 0, 0, 140))
            x += font.getlength(seg)
        y += step
    if layer:
        canvas.alpha_composite(layer.filter(ImageFilter.GaussianBlur(glow)))
    draw = ImageDraw.Draw(canvas)
    y = box[1] + {"top": 0, "bottom": box[3] - total}.get(t["valign"], (box[3] - total) / 2)
    boxes = []
    for n, line in enumerate(lines):
        width = font.getbbox(line, stroke_width=stroke)[2]
        x = {"left": box[0], "center": box[0] + (box[2] - width) / 2}.get(side, box[0] + box[2] - width)
        base_fill = fills[min(n, len(fills) - 1)] if fills else style["fill"]
        boxes.append(draw.textbbox((x, y), line, font=font, stroke_width=stroke))
        for seg, accent in _segments(line, accents):
            draw.text((x, y), seg, font=font, fill=style["accent"] if accent else base_fill,
                      stroke_width=stroke, stroke_fill=style["stroke"])
            x += font.getlength(seg)
        y += step

    border = tpl.get("border")
    if border and border.get("width"):  # 바깥 테두리 (채널 마크는 그 위에)
        ImageDraw.Draw(canvas).rectangle((0, 0, SIZE[0] - 1, SIZE[1] - 1), outline=border["color"], width=border["width"])

    if mark:  # 채널 마크: 늘 같은 파일·같은 자리·같은 크기, 맨 위에
        canvas.alpha_composite(mark, tuple(logo["pos"]))
    out = canvas.convert("RGB")
    out.info["layout"] = {"lines": lines, "boxes": boxes, "font": font_file.name, "side": side, "style": si,
                          "name": plan.get("layout") or side, "photo": photo_info,
                          "glyph": (lambda b: b[3] - b[1])(font.getbbox("가")) if font else 0,
                          "colors": [style.get("accent"), *(fills or [style.get("fill")])], "stroke": style.get("stroke")}
    return out


# ---------- 점검: YouTube 공식 도움말·대규모 조사에서 코드로 잴 수 있는 것 ----------
BADGE = (1280 - 430, 720 - 170, 1280, 720)  # 오른쪽 아래 영상 길이 표시 (1시간 넘는 영상, 168px 화면 기준을 키운 값)
PROGRESS = (0, 720 - 50, 1280, 720)  # 다시 볼 때 아래쪽 진행 막대


def _luma(color):
    r, g, b = (int(color[i:i + 2], 16) / 255 for i in (1, 3, 5)) if isinstance(color, str) else (c / 255 for c in color[:3])
    lin = [c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4 for c in (r, g, b)]
    return 0.2126 * lin[0] + 0.7152 * lin[1] + 0.0722 * lin[2]


def contrast(a, b):
    """WCAG 대비 (1~21). 큰 글자는 4.5 이상이면 넉넉."""
    hi, lo = sorted((_luma(a), _luma(b)), reverse=True)
    return (hi + 0.05) / (lo + 0.05)


def check(img):
    """[(좋음?, 설명)], 점수(0~100). 근거: YouTube 도움말(작은 화면에서 읽히게, 정확하게), vidIQ·1of10 조사(글자 적게, 밝게)."""
    lay, out, score = img.info.get("layout", {}), [], 100

    def add(ok, good, bad, cost):
        nonlocal score
        out.append((ok, good if ok else bad))
        score -= 0 if ok else cost
    lines, boxes = lay.get("lines", []), lay.get("boxes", [])
    if lines:
        g = lay.get("glyph", 0)
        add(g >= 90, f"글자 높이 {g}px — 작은 화면(168px)에서도 읽힘", f"글자 높이 {g}px — 작은 화면에서 안 읽힐 수 있음 (90px 이상 권장)", 25)
        words = sum(len(x.split()) for x in lines)
        add(words <= 6, f"문구 {words}단어", f"문구 {words}단어 — 많음 (잘 되는 썸네일은 대부분 5단어 이하)", 10)
        area = sum((b[2] - b[0]) * (b[3] - b[1]) for b in boxes) / (1280 * 720)
        add(area <= 0.3, f"글자가 화면의 {area:.0%}", f"글자가 화면의 {area:.0%} — 그림이 잘 안 보일 수 있음", 5)
        cmin = min((contrast(c, lay["stroke"]) for c in lay.get("colors", []) if c), default=21)
        add(cmin >= 4.5, f"글자·외곽선 대비 {cmin:.1f}:1", f"글자·외곽선 대비 {cmin:.1f}:1 — 낮음 (4.5:1 이상)", 10)

        def hits(zone):
            return any(b[0] < zone[2] and zone[0] < b[2] and b[1] < zone[3] and zone[1] < b[3] for b in boxes)
        add(not hits(BADGE), "오른쪽 아래 영상 길이 표시 자리 비어 있음",
            "글자 끝이 오른쪽 아래 영상 길이 표시에 가려질 수 있음 (작은 화면)", 10)
        add(not hits(PROGRESS), "맨 아래 진행 막대 자리 비어 있음", "글자가 맨 아래 진행 막대 자리(50px)에 걸침", 5)
    luma = sum(img.convert("L").resize((64, 36)).getdata()) / (64 * 36)
    add(luma >= 90, f"밝기 {luma:.0f}/255", f"밝기 {luma:.0f}/255 — 어두운 편 (어두운 썸네일이 덜 눌린다는 조사)", 10)
    return out, max(score, 0)


def save(img, path):
    """YouTube 한도(2MB) 안으로 JPG 저장."""
    for quality in (92, 88, 82, 75, 68):
        buf = io.BytesIO()
        img.save(buf, "JPEG", quality=quality, optimize=True)
        if buf.tell() <= core.THUMB_MAX:
            Path(path).write_bytes(buf.getvalue())
            return path
    raise ValueError("썸네일을 2MB 아래로 줄이지 못했습니다.")


def fetch_photo(folder, num, title, desc, text, key="", log=print, force=False):
    """주제마다 실제 사진 1장 (위키미디어 공용, 저작권 풀린 것만): 채널 폴더/실제사진/<순번>.jpg. 없으면 None.
    한 번 찾으면 다시 찾지 않는다 (force면 새로, 앞에서 고른 것은 빼고).
    준비 화면과 최종 만들기가 같은 영상을 동시에 찾으면 하나가 끝날 때까지 기다렸다가 그 결과를 쓴다 (Claude 비용 두 번 안 듦)."""
    with _LOCKS.setdefault(("사진", str(folder), num), threading.Lock()):
        return _fetch_photo(folder, num, title, desc, text, key, log, force)


def _fetch_photo(folder, num, title, desc, text, key, log, force):
    path = Path(folder) / core.PHOTOS / f"{num}.jpg"
    note = path.with_suffix(".json")
    old = json.loads(note.read_text("utf-8")) if note.exists() else {}
    if path.exists() and not force:
        return path
    if old.get("none") and not force:
        return None
    if not (key or os.environ.get("ANTHROPIC_API_KEY")):
        return None
    import ai
    cl = ai.client(key)
    queries = ai.queries(cl, title, desc, text)
    photo, credit = ai.find_photo(cl, queries, text, title, log, skip=old.get("skip", []) + [old.get("credit", "")], desc=desc)
    path.parent.mkdir(exist_ok=True)
    note.write_text(json.dumps({"credit": credit, "queries": queries, "none": not photo,
                                "skip": old.get("skip", []) + ([old["credit"]] if force and old.get("credit") else [])},
                               ensure_ascii=False, indent=1), "utf-8")
    if not photo:
        path.unlink(missing_ok=True)
        return None
    photo.convert("RGB").save(path, "JPEG", quality=90)
    return path


def preview(ch, base, photo, out):
    """후보 미리보기: 후보 그림 + 실제 사진 (글자 없이). 채널 마크는 그대로."""
    img = compose(ch, base, "", {"side": "left"}, photo, random.Random(0))
    img.thumbnail((640, 360))
    img.save(out, "JPEG", quality=85)
    return out


def make_for(ch, folder, num, text, title="", desc="", key="", log=print, force=False, rnd=None):
    """순번 하나의 최종 썸네일: 고른 후보 그림 + 실제 사진 + 문구 + 채널 마크 → 채널 폴더/<순번>_자동썸네일.jpg.
    API 키가 있으면 Claude가 글자 쪽·강조할 말을 정한다. 그림·문구·실제 사진이 그대로면 다시 만들지 않는다."""
    base = core.material_for(folder, num)
    if not base:
        raise FileNotFoundError(f"{num}번 썸네일 후보 그림이 없습니다. 준비 화면에서 넣어주세요.")
    text = (text or "").strip()
    if not text:
        raise ValueError(f"{num}번 썸네일 문구가 비어 있습니다.")
    tpl = load_template(ch)
    photo = fetch_photo(folder, num, title, desc, text, key, log) if tpl.get("inset") else None
    path = Path(folder) / f"{num}_자동썸네일.jpg"
    note = path.with_suffix(".json")
    sig = {"text": text, "base": base.name, "size": base.stat().st_size, "photo": photo.stat().st_size if photo else 0}
    if not force and path.exists() and note.exists() and json.loads(note.read_text("utf-8")).get("sig") == sig:
        return path
    plan, review = {}, None
    use_ai = bool(key or os.environ.get("ANTHROPIC_API_KEY"))
    made = _MADE.setdefault(str(folder), {})
    with _LOCKS.setdefault(str(folder), threading.Lock()):  # 같은 채널은 디자인을 차례로 정한다 (동시에 만든 편끼리도 안 겹치게)
        recent = used_before(folder, path, skip=made) + [u for p, u in made.items() if p != path]
        stats = ctr_stats(ch)
        if use_ai:
            import ai
            plan = ai.plan(ai.client(key), base, text, title, desc, options(ch, tpl), recent[-4:], ctr_text(stats), photo)
        else:  # Claude 없이: 최근 두 편에 쓴 글자 자리는 피하고, 클릭률이 좋았던 자리를 더 자주
            plan = {"avoid": [r.get("layout") for r in recent[-2:]], "boost": boost(stats.get("layout", {}))}
        img = compose(ch, base, text, plan, photo, rnd)
        lay = img.info["layout"]
        used = {"layout": lay["name"], "font": lay["font"], "style": lay["style"], "lines": lay["lines"],
                "photo": (lay["photo"] or {}).get("spot"), "photo_size": (lay["photo"] or {}).get("size")}
        made.pop(path, None)
        made[path] = {k: used[k] for k in ("layout", "font", "style", "photo", "photo_size") if used[k] is not None}
    checks, points = check(img)
    if use_ai:
        try:
            review = ai.score(ai.client(key), img, title, text, desc, examples(ch), checks)
        except Exception as e:
            log(f"    (Claude 점수 실패: {e})")
    save(img, path)
    note.write_text(json.dumps({"sig": sig, "plan": plan, "used": used, "check": checks, "check_score": points, "review": review},
                               ensure_ascii=False, indent=1), "utf-8")
    return path


def options(ch, tpl):
    """Claude에게 줄 이 채널의 고를 거리: 글자 자리·색 조합·글씨체·사진 자리."""
    out = {"sides": tpl["text"]["sides"], "max_lines": tpl["text"]["max_lines"],
           "spots": [(lambda n, b, a: f"{n} {b}")(*_spot(s, i)) for i, s in enumerate(_spots(tpl))],
           "fonts": sorted({p.name for p in fonts(ch, tpl)}),
           "styles": [s.get("_설명") or f"줄 색 {s.get('line_fills') or s.get('fill')}, 강조 {s.get('accent')}" for s in tpl["styles"]]}
    if tpl.get("layouts"):
        total = sum(x.get("weight", 1) for x in tpl["layouts"])
        out["layouts"] = [(x["name"], x.get("_설명", "") + f" (이 채널에서 약 {x.get('weight', 1) / total:.0%}, 글자 칸 폭 {x['box'][2]}px)"
                           + (f" (사진 자리 {x['spots']}번만)" if x.get("spots") is not None else ""))
                          for x in tpl["layouts"]]
    return out


_LOCKS, _MADE = {}, {}  # 채널 폴더마다: 디자인 정하는 차례, 이번에 프로그램이 만든 조합 {그림 경로: 조합}


def used_before(folder, path, n=4, skip=()):
    """같은 채널 폴더에서 먼저 만든 최종 썸네일들이 쓴 조합 (오래된 것부터, 최근 n개). skip: 뺄 그림 경로들"""
    notes = sorted((q for q in Path(folder).glob("*_자동썸네일.json")
                    if q != path.with_suffix(".json") and q.with_suffix(".jpg") not in skip),
                   key=lambda q: q.stat().st_mtime)
    out = []
    for q in notes[-n:]:
        try:
            u = json.loads(q.read_text("utf-8")).get("used")
        except (OSError, ValueError):
            continue
        if u:
            out.append({k: u[k] for k in ("layout", "font", "style", "photo", "photo_size") if u.get(k) is not None})
    return out


# ---------- 클릭률: 올린 썸네일의 조합 ↔ YouTube 썸네일 클릭률 ----------
def _ctr_file(ch):
    return template_dir(ch) / "클릭률.json"  # 틀 폴더에 둔다: 청소·업데이트에 지워지지 않게


def load_ctr(ch):
    f = _ctr_file(ch)
    data = json.loads(f.read_text("utf-8")) if f.exists() else {}
    return {"designs": {}, "days": {}, "after": ""} | data


def _save_ctr(ch, data):
    _ctr_file(ch).parent.mkdir(parents=True, exist_ok=True)
    _ctr_file(ch).write_text(json.dumps(data, ensure_ascii=False), "utf-8")


def remember_upload(ch, video_id, path):
    """자동 썸네일을 영상에 넣을 때: 그 영상이 쓴 조합(자리·글씨체·색)을 적어 둔다 (나중에 클릭률과 맞춰 봄)."""
    note = Path(path).with_suffix(".json")
    used = json.loads(note.read_text("utf-8")).get("used") if note.exists() else None
    if used:
        data = load_ctr(ch)
        data["designs"][video_id] = used
        _save_ctr(ch, data)


def add_reach(ch, reports):
    """YouTube 클릭률 보고서(CSV)를 날짜별로 쌓는다. 같은 날 보고서가 다시 오면(보정) 덮어쓴다."""
    data = load_ctr(ch)
    for r in sorted(reports, key=lambda r: r["createTime"]):
        for row in csv.DictReader(io.StringIO(r["csv"])):
            vid, impr = row.get("video_id"), int(float(row.get("video_thumbnail_impressions") or 0))
            if vid and impr:
                data["days"].setdefault(vid, {})[row["date"]] = [impr, float(row.get("video_thumbnail_impressions_ctr") or 0)]
        data["after"] = max(data["after"], r["createTime"])
    _save_ctr(ch, data)
    return data


def ctr_stats(ch, min_impr=300):
    """조합별 클릭률: {'layout': {'아래_가운데': (평균 %, 영상 수)}, 'font': …, 'style': …}.
    노출이 min_impr번 넘은 영상만, 영상마다 같은 무게로 (조회수 큰 한 편이 다 정하지 않게)."""
    data = load_ctr(ch)
    rates = {}
    for vid, days in data["days"].items():
        impr = sum(i for i, _ in days.values())
        if vid in data["designs"] and impr >= min_impr:
            rates[vid] = sum(i * c for i, c in days.values()) / impr
    # shortcut: 보고서의 클릭률이 비율(0.05)인지 퍼센트(5)인지 문서에 없어, 1보다 큰 값이 있으면 퍼센트로 본다
    scale = 1 if any(r > 1 for r in rates.values()) else 100
    out = {}
    for key in ("layout", "font", "style"):
        groups = {}
        for vid, rate in rates.items():
            groups.setdefault(str(data["designs"][vid].get(key)), []).append(rate * scale)
        out[key] = {v: (round(sum(rs) / len(rs), 2), len(rs)) for v, rs in groups.items()}
    return out


def boost(groups, least=3):
    """클릭률이 평균보다 좋았던 값은 더 자주 (0.5~2배). 영상이 least편 미만인 값은 그대로."""
    known = {v: r for v, (r, n) in groups.items() if n >= least}
    if len(known) < 2:
        return {}
    mean = sum(known.values()) / len(known)
    return {v: min(2.0, max(0.5, r / mean)) for v, r in known.items()} if mean else {}


def ctr_text(stats):
    """사람(과 Claude)이 읽는 한 줄: '글자 자리 아래_가운데 5.1% (6편) · …'"""
    names = {"layout": "글자 자리", "font": "글씨체", "style": "색 조합"}
    parts = [f"{names[k]} {v} {r}% ({n}편)" for k, g in stats.items() for v, (r, n) in sorted(g.items(), key=lambda x: -x[1][0])]
    return " · ".join(parts)


def examples(ch, n=3):
    """채널 예시 썸네일 (틀 폴더/예시/): Claude가 채널다운지 비교할 때."""
    folder = template_dir(ch) / "예시"
    return sorted(p for p in folder.glob("*") if p.suffix.lower() in core.IMAGE_EXT)[:n] if folder.exists() else []


def report(path):
    """최종 썸네일 옆 기록: (점검 [(좋음?, 설명)], 점검 점수, Claude 평가 또는 None)."""
    note = Path(path).with_suffix(".json")
    data = json.loads(note.read_text("utf-8")) if note.exists() else {}
    return data.get("check", []), data.get("check_score"), data.get("review")


def make(ch, row, key="", log=print, force=False, rnd=None):
    """목록의 한 줄로 최종 썸네일 (2. 자동 처리에서)."""
    num = core.row_num(row)
    if num is None:
        raise ValueError("영상 파일 이름에서 순번을 못 찾았습니다.")
    return make_for(ch, (core.VIDEO_DIR / row["파일"]).parent, num, row["썸네일문구"], row["제목"], row["설명"],
                    key, log, force, rnd)
