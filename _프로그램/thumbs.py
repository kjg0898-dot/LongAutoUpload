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
    """글씨체: 내가 틀 폴더에 넣은 폰트 → 틀이 정한 폰트(프로그램과 함께 오는 무료 폰트 등) → 맑은 고딕 굵게."""
    own = sorted(p for p in template_dir(ch).glob("*") if p.suffix.lower() in (".ttf", ".otf", ".ttc"))
    given = [template_dir(ch) / tpl["font"]] if tpl.get("font") else []
    for p in own + given + (WINDOWS_FONTS if sys.platform == "win32" else OTHER_FONTS):
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
    t = tpl["text"]
    lines = [s.strip() for s in (text or "").replace("\\n", "/").split("/") if s.strip()]  # 비면 글자 없이 (후보 미리보기)
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
    if tpl.get("shade"):  # 글자 쪽(또는 아래쪽)으로 갈수록 어두워지는 그늘 (글자가 잘 읽히게)
        bottom = tpl.get("shade_dir") == "bottom" or side == "center"
        shade = Image.new("L", SIZE)
        for i in range(SIZE[1] if bottom else SIZE[0]):
            k = i / SIZE[1] if bottom else (1 - i / SIZE[0] if side == "left" else i / SIZE[0])
            band = (0, i, SIZE[0], i + 1) if bottom else (i, 0, i + 1, SIZE[1])
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
    if photo and inset:  # 실제 사진: 글자 반대쪽 위, 흰 테두리 + 그림자 + 살짝 기울임
        spots = inset.get("boxes") or [inset["box"]]
        spot = spots[plan["spot"]] if isinstance(plan.get("spot"), int) and 0 <= plan["spot"] < len(spots) else rnd.choice(spots)
        x, y, w, h = _mirror(spot, side) if side != "center" else spot  # 사진 칸은 '글자 왼쪽' 기준 (오른쪽이면 뒤집음)
        if mark:  # 채널 마크를 가리지 않게 마크 아래로
            lx, ly = logo["pos"]
            if x < lx + mark.width and lx < x + w and y < ly + mark.height and ly < y + h:
                y = ly + mark.height + 24
        pic = _cover(_image(photo), w, h)
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
    box = _mirror(box, side) if side != "center" else box
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
    for n, line in enumerate(lines):
        width = font.getbbox(line, stroke_width=stroke)[2]
        x = {"left": box[0], "center": box[0] + (box[2] - width) / 2}.get(side, box[0] + box[2] - width)
        base_fill = fills[min(n, len(fills) - 1)] if fills else style["fill"]
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


def fetch_photo(folder, num, title, desc, text, key="", log=print, force=False):
    """주제마다 실제 사진 1장 (위키미디어 공용, 저작권 풀린 것만): 채널 폴더/실제사진/<순번>.jpg. 없으면 None.
    한 번 찾으면 다시 찾지 않는다 (force면 새로, 앞에서 고른 것은 빼고)."""
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
    photo, credit = ai.find_photo(cl, queries, text, title, log, skip=old.get("skip", []) + [old.get("credit", "")])
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
    plan = {}
    if key or os.environ.get("ANTHROPIC_API_KEY"):
        import ai
        plan = ai.plan(ai.client(key), base, text, title, desc, tpl["text"]["sides"], (tpl.get("inset") or {}).get("boxes", ()))
    save(compose(ch, base, text, plan, photo, rnd), path)
    note.write_text(json.dumps({"sig": sig, "plan": plan}, ensure_ascii=False, indent=1), "utf-8")
    return path


def make(ch, row, key="", log=print, force=False, rnd=None):
    """목록의 한 줄로 최종 썸네일 (2. 자동 처리에서)."""
    num = core.row_num(row)
    if num is None:
        raise ValueError("영상 파일 이름에서 순번을 못 찾았습니다.")
    return make_for(ch, (core.VIDEO_DIR / row["파일"]).parent, num, row["썸네일문구"], row["제목"], row["설명"],
                    key, log, force, rnd)
