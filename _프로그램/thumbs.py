"""썸네일 자동 만들기: 채널 틀 + 재료 사진 + 사용자가 쓴 문구 (+ 위키미디어 공용 실제 사진).
글자는 AI가 그리지 않고 폰트로 찍는다 (한글이 깨지지 않게). 채널 마크는 항상 같은 파일을 같은 자리에 그대로 붙인다.
매번 조금씩 다르게: 글씨 색 조합·글자 쪽(왼/오른)·크기·사진 기울기가 틀이 정한 범위 안에서 바뀐다."""
import io
import json
import os
import random
import sys
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter, ImageFont

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


def font_path(ch, tpl):
    if tpl.get("font"):
        p = template_dir(ch) / tpl["font"]
        if not p.exists():
            raise FileNotFoundError(f"[{ch['name']}] 틀의 폰트 파일이 없습니다: {p}")
        return p
    own = sorted(p for p in template_dir(ch).glob("*") if p.suffix.lower() in (".ttf", ".otf", ".ttc"))
    for p in own + (WINDOWS_FONTS if sys.platform == "win32" else OTHER_FONTS):
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
    """썸네일 그림. plan: {'side': 'left'|'right', 'accents': [...]} (없으면 무작위·마지막 줄 강조)."""
    rnd = rnd or random.Random()
    tpl, plan = load_template(ch), plan or {}
    t = tpl["text"]
    lines = [s.strip() for s in text.replace("\\n", "/").split("/") if s.strip()]
    if not lines:
        raise ValueError("썸네일 문구가 비어 있습니다.")
    if len(lines) > t["max_lines"]:
        raise ValueError(f"썸네일 문구가 {len(lines)}줄입니다 (이 채널은 {t['max_lines']}줄까지).")
    font_file = font_path(ch, tpl)
    lost = missing_glyphs(ImageFont.truetype(str(font_file), 40), "".join(lines))
    if lost:
        raise ValueError(f"폰트({font_file.name})에 없는 글자가 있어 깨집니다: {' '.join(lost)} — 문구에서 빼거나 다른 폰트를 쓰세요.")
    side = plan.get("side") if plan.get("side") in t["sides"] else rnd.choice(t["sides"])
    style = rnd.choice(tpl["styles"])
    jit = tpl.get("jitter", 0)

    canvas = _cover(_image(base), *SIZE).convert("RGBA")
    if tpl.get("shade"):  # 글자 쪽으로 갈수록 어두워지는 그늘 (글자가 잘 읽히게)
        shade = Image.new("L", SIZE)
        for x in range(SIZE[0]):
            k = 1 - x / SIZE[0] if side == "left" else x / SIZE[0]
            shade.paste(int(255 * tpl["shade"] * max(0.0, k * 1.6 - 0.3)), (x, 0, x + 1, SIZE[1]))
        canvas = Image.composite(Image.new("RGBA", SIZE, (0, 0, 0, 255)), canvas, shade)

    logo = tpl.get("logo") or {}
    logo_file = template_dir(ch) / logo.get("file", "")
    mark = None
    if logo.get("file") and logo_file.is_file():
        mark = Image.open(logo_file).convert("RGBA")
        if logo.get("width"):
            mark = mark.resize((logo["width"], round(mark.height * logo["width"] / mark.width)), Image.LANCZOS)

    inset = tpl.get("inset")
    if photo and inset:  # 실제 사진: 글자 반대쪽 위, 흰 테두리 + 그림자 + 살짝 기울임
        x, y, w, h = _mirror(inset["box"], side)  # 틀의 사진 칸은 '글자 왼쪽' 기준이라 이미 반대편에 있다
        if mark:  # 채널 마크를 가리지 않게 마크 아래로
            lx, ly = logo["pos"]
            if x < lx + mark.width and lx < x + w and y < ly + mark.height and ly < y + h:
                y = ly + mark.height + 24
        pic = _cover(_image(photo), w, h)
        b = inset.get("border", 0)
        framed = Image.new("RGBA", (w + 2 * b, h + 2 * b), inset.get("border_color", "#FFFFFF"))
        framed.paste(pic, (b, b))
        framed = framed.rotate(rnd.uniform(-1, 1) * inset.get("tilt", 0), expand=True, resample=Image.BICUBIC)
        shadow = Image.new("RGBA", framed.size, (0, 0, 0, 0))
        shadow.paste((0, 0, 0, 150), mask=framed.split()[3])
        canvas.alpha_composite(shadow.filter(ImageFilter.GaussianBlur(10)), (x - b + 10, y - b + 12))
        canvas.alpha_composite(framed, (x - b, y - b))

    box = _mirror([round(v * (1 + rnd.uniform(-jit, jit))) if i < 2 else v for i, v in enumerate(t["box"])], side)
    stroke = t["stroke"]
    font = _fit(lines, font_file, box, t, stroke)
    draw = ImageDraw.Draw(canvas)
    step = font.size * (1 + t["line_gap"])
    total = step * len(lines)
    y = box[1] + {"top": 0, "bottom": box[3] - total}.get(t["valign"], (box[3] - total) / 2)
    accents = plan.get("accents") or [lines[-1]]
    for line in lines:
        width = font.getbbox(line, stroke_width=stroke)[2]
        x = box[0] if side == "left" else box[0] + box[2] - width
        for seg, accent in _segments(line, accents):
            draw.text((x + 6, y + 8), seg, font=font, fill=(0, 0, 0, 140), stroke_width=stroke, stroke_fill=(0, 0, 0, 140))
            draw.text((x, y), seg, font=font, fill=style["accent"] if accent else style["fill"],
                      stroke_width=stroke, stroke_fill=style["stroke"])
            x += font.getlength(seg)
        y += step

    if mark:  # 채널 마크: 늘 같은 파일·같은 자리·같은 크기, 맨 위에
        canvas.alpha_composite(mark, tuple(logo["pos"]))
    return canvas.convert("RGB")


def save(img, path):
    """YouTube 한도(2MB) 안으로 JPG 저장."""
    for quality in (92, 88, 82, 75, 68):
        buf = io.BytesIO()
        img.save(buf, "JPEG", quality=quality, optimize=True)
        if buf.tell() <= core.THUMB_MAX:
            Path(path).write_bytes(buf.getvalue())
            return path
    raise ValueError("썸네일을 2MB 아래로 줄이지 못했습니다.")


def make(ch, row, key="", log=print, force=False, rnd=None):
    """영상 하나의 썸네일을 만들어 채널 폴더에 <번호>_자동썸네일.jpg 로 저장하고 경로를 돌려준다.
    API 키(또는 ANTHROPIC_API_KEY)가 있으면 Claude가 글자 쪽·강조할 말을 정하고 실제 사진을 골라 넣는다.
    문구와 재료 사진이 그대로면 다시 만들지 않는다 (force면 새로)."""
    base = core.material(row)
    if not base:
        raise FileNotFoundError(f"재료 사진이 없습니다: 채널 폴더/{core.MATERIAL}/ 에 같은 번호 사진을 넣어주세요.")
    text = row["썸네일문구"].strip()
    if not text:
        raise ValueError("썸네일 문구가 비어 있습니다.")
    num = min(core._nums(Path(row["파일"])))
    path = (core.VIDEO_DIR / row["파일"]).parent / f"{num}_자동썸네일.jpg"
    note = path.with_suffix(".json")
    sig = {"text": text, "base": base.name, "size": base.stat().st_size}
    if not force and path.exists() and note.exists() and json.loads(note.read_text("utf-8")).get("sig") == sig:
        return path
    tpl = load_template(ch)
    plan, photo, credit = {}, None, ""
    if key or os.environ.get("ANTHROPIC_API_KEY"):
        import ai
        cl = ai.client(key)
        plan = ai.plan(cl, base, text, row["제목"], row["설명"], tpl["text"]["sides"])
        if tpl.get("inset"):
            photo, credit = ai.find_photo(cl, plan.get("queries", []), text, row["제목"], log)
    save(compose(ch, base, text, plan, photo, rnd), path)
    note.write_text(json.dumps({"sig": sig, "plan": plan, "photo": credit}, ensure_ascii=False, indent=1), "utf-8")
    return path
