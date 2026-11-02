"""Claude API + 위키미디어 공용: 썸네일 글자 쪽·강조할 말 정하기, 저작권이 풀린 실제 사진 찾아 고르기."""
import base64
import io
import json
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

from PIL import Image

import core

CLAUDE = core.RULES["claude"]  # 모델·가격은 규칙.json (매주 자동 점검)
MODEL = CLAUDE["model"]
UA = "LongAutoUpload/1.0 (https://github.com/kjg0898-dot/LongAutoUpload)"  # 위키미디어는 이름 없는 요청을 막는다
COMMONS = "https://commons.wikimedia.org/w/api.php"
PRICE = (CLAUDE["price_in"], CLAUDE["price_out"])  # 달러 / 100만 토큰 (입력, 출력)
USAGE = []  # 이번에 켠 뒤 쓴 토큰 [(입력, 출력)] — 실제 비용 확인용


def cost(usages):
    return sum(i * PRICE[0] + o * PRICE[1] for i, o in usages) / 1e6


def client(key=""):
    import anthropic
    return anthropic.Anthropic(api_key=key) if key else anthropic.Anthropic()  # 키가 없으면 ANTHROPIC_API_KEY 환경변수


def image_block(img, width=768):
    img = (img if isinstance(img, Image.Image) else Image.open(img)).convert("RGB")
    img.thumbnail((width, width))
    buf = io.BytesIO()
    img.save(buf, "JPEG", quality=85)
    return {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg",
                                        "data": base64.standard_b64encode(buf.getvalue()).decode()}}


def ask(cl, content, schema):
    """구조화된 JSON 답 하나. 거절되면 서버가 다른 모델로 다시 돌린다 (fallbacks)."""
    import anthropic
    try:
        res = cl.beta.messages.create(
            model=MODEL, max_tokens=16000,
            betas=[CLAUDE["fallback_beta"]], fallbacks="default",
            output_config={"effort": CLAUDE["effort"], "format": {"type": "json_schema", "schema": schema}},
            messages=[{"role": "user", "content": content}])
    except anthropic.AuthenticationError:
        raise RuntimeError("Claude API 키가 맞지 않습니다. 설정 → 고급 설정에서 키를 확인해주세요.") from None
    except anthropic.PermissionDeniedError:
        raise RuntimeError("이 Claude API 키로는 쓸 수 없습니다 (권한).") from None
    except anthropic.RateLimitError:
        raise RuntimeError("Claude API 요청이 몰렸습니다. 잠시 뒤 다시 해주세요.") from None
    except anthropic.APIStatusError as e:
        raise RuntimeError(f"Claude API 오류 ({e.status_code}): {e.message}") from None
    except anthropic.APIConnectionError:
        raise RuntimeError("Claude API에 연결하지 못했습니다 (인터넷 확인).") from None
    u = getattr(res, "usage", None)
    if u:
        USAGE.append((u.input_tokens + (u.cache_read_input_tokens or 0) + (u.cache_creation_input_tokens or 0),
                      u.output_tokens))
    if res.stop_reason == "refusal":
        raise RuntimeError("Claude가 이 요청을 거절했습니다.")
    text = next((b.text for b in res.content if b.type == "text"), "")
    try:
        return json.loads(text)
    except ValueError:
        raise RuntimeError(f"Claude 답을 읽지 못했습니다 (끝난 이유: {res.stop_reason}).") from None


def _schema(props, required=None):
    return {"type": "object", "properties": props, "required": required or list(props), "additionalProperties": False}


def plan(cl, base, text, title, desc, opts, recent=(), ctr="", photo=None):
    """이번 편의 디자인: 글자 자리·줄 나눔·강조할 말·색 조합·글씨체·크기·실제 사진 자리·검색어.
    opts: thumbs.options()가 만든 고를 거리, recent: 같은 채널에서 최근 쓴 조합 (되풀이하지 않게)."""
    layouts, fonts, styles, spots = opts.get("layouts", []), opts.get("fonts", []), opts.get("styles", []), opts.get("spots", [])
    ask_text = [
        "유튜브 썸네일을 만듭니다. 첨부한 사진이 배경이고, 그 위에 아래 문구를 크게 넣습니다. "
        "썸네일은 영상 클릭을 정하는 가장 중요한 요소입니다. 이 사진과 문구에 가장 잘 맞게, 그리고 이 채널의 다른 편과 너무 똑같지 않게 정해주세요.",
        f"문구 (/ 는 줄바꿈): {text}\n영상 제목: {title}\n영상 설명: {desc[:600]}\n",
        "lines: 문구를 몇 줄로 어디서 나눌지 (1~%d줄). 글자는 문구 그대로, 띄어쓰기만 바꿔 나눔. 의미가 끊기지 않게, "
        "줄 길이가 비슷하게 (한 줄이 다른 줄의 절반보다 짧으면 안 됨)." % opts.get("max_lines", 2),
        "accents: 다른 색으로 강조할 핵심 말 1~2개 (숫자·인물·반전 같은 궁금증을 만드는 말). 문구에 있는 글자 그대로."]
    props = {"lines": {"type": "array", "items": {"type": "string"}}, "accents": {"type": "array", "items": {"type": "string"}},
             "queries": {"type": "array", "items": {"type": "string"}}}
    if layouts:
        ask_text.append("layout: 문구를 놓을 자리. 사진 속 사람 얼굴이나 핵심 대상을 가리지 않는 곳. 채널 통일감을 위해 비율을 대략 지키고, "
                        "자리가 좁으면 글자가 작아지니 문구가 길면(한 줄 8자 넘음) 넓은 자리를 고를 것. "
                        + "; ".join(f"{n} = {d}" for n, d in layouts))
        props["layout"] = {"type": "string", "enum": [n for n, _ in layouts]}
    else:
        ask_text.append(f"side: 문구를 둘 쪽 ({' 또는 '.join(opts['sides'])}). 사진 속 사람 얼굴이나 핵심 대상을 가리지 않는 쪽.")
        props["side"] = {"type": "string", "enum": list(opts["sides"])}
    if len(styles) > 1:
        ask_text.append("style: 글자 색 조합 번호. " + "; ".join(f"{i}번 {s}" for i, s in enumerate(styles)) + ". 배경 색과 잘 대비되는 것.")
        props["style"] = {"type": "integer"}
    if len(fonts) > 1:
        ask_text.append("font: 글씨체 (" + ", ".join(fonts) + "). 주제 분위기에 맞게.")
        props["font"] = {"type": "string", "enum": list(fonts)}
    ask_text.append("size: 글자 크기 배율 0.85~1.1 (문구가 짧고 강할수록 크게, 배경이 복잡하면 조금 작게).")
    props["size"] = {"type": "number"}
    if len(spots) > 1 and photo is not None:
        ask_text.append("spot: 두 번째로 첨부한 실제 사진을 놓을 자리 번호. 고른 layout에서 쓸 수 있는 자리 중에서, 배경 속 얼굴·핵심 대상을 가리지 않고 "
                        "배경에서 비어 있거나 덜 중요한 곳 (→ 뒤의 칸이 이 사진이 실제로 차지할 곳이니 그 안에 배경 인물 얼굴이 들어가면 안 됨). 사진은 자기 비율대로 그 자리 안에 들어가고, 글자와 조금(사진의 4분의 1까지) 겹쳐도 됨 "
                        "(글자가 외곽선과 함께 위에 그려짐). 자리(1280x720 기준 x,y,너비,높이): "
                        + ", ".join(f"{i}번 {s}" for i, s in enumerate(spots)))
        ask_text.append("photo_size: 실제 사진 크기 0.9~1.0 (자리 대비). 사진이 주제를 잘 보여주면 크게, 배경 주인공이 크면 조금 작게. "
                        "단 휴대폰에서 썸네일이 작게 보여도 사진이 무엇인지 알아봐야 하니, 세밀한 사진(문서·지도·작은 인물이 많은 장면)은 큰 자리·큰 크기로.")
        props["spot"], props["photo_size"] = {"type": "integer"}, {"type": "number"}
    ask_text.append("queries: 위키미디어 공용(Wikimedia Commons)에서 이 영상 주제의 실제 사진·기록·지도·유물을 찾을 영어 검색어 3개. "
                    "구체적인 인물·사건·장소·물건 이름으로, 각각 2~4단어로 짧게.")
    if recent:
        ask_text.append("이 채널에서 최근 만든 썸네일들의 조합 (같은 조합을 되풀이하지 말고 자리·색·글씨체 중 둘 이상은 다르게. "
                        "단 이 사진에 안 맞으면 억지로 바꾸지 말 것): "
                        + " / ".join(json.dumps(r, ensure_ascii=False) for r in recent))
    if ctr:
        ask_text.append("이 채널 지난 영상들의 실제 썸네일 클릭률 (영상마다 평균, 괄호는 영상 수): " + ctr
                        + ". 클릭률이 높았던 자리·색·글씨체를 더 자주 고르되, 영상 수가 적으면 참고만 하고 이 사진에 안 맞으면 쓰지 말 것.")
    images = [image_block(base)] + ([{"type": "text", "text": "넣을 실제 사진:"}, image_block(photo, 512)] if photo is not None else [])
    out = ask(cl, images + [{"type": "text", "text": "\n".join(ask_text)}], _schema(props))
    out["accents"] = [a for a in out.get("accents", []) if a.strip() and a.strip() in text]  # 문구에 없는 말은 버림
    return out


def queries(cl, title, desc, text):
    """주제에 맞는 실제 사진·기록·지도·유물을 위키미디어 공용에서 찾을 영어 검색어 3개."""
    out = ask(cl, [{"type": "text", "text": (
        f"유튜브 영상의 썸네일에 넣을 실제 사진을 위키미디어 공용(Wikimedia Commons)에서 찾습니다.\n"
        f"영상 제목: {title}\n썸네일 문구: {text}\n영상 설명: {desc[:600]}\n\n"
        "이 주제의 실제 사진·기록·지도·유물·그림을 찾을 영어 검색어 3개를 주세요. 구체적인 인물·사건·장소·물건 이름으로, "
        "각각 2~4단어로 짧게 (길면 검색 결과가 없음). 예: 'Gulf War soldiers', 'MRE ration', 'Battle of Fontenoy'.")}],
        _schema({"queries": {"type": "array", "items": {"type": "string"}}}))
    return out.get("queries", [])[:3]


RUBRIC = (("legibility", "작은 화면(168x94)에서도 글자가 읽히는가", 20),
          ("focus", "눈에 띄는 주인공 하나, 단순한 구성", 15),
          ("accuracy", "썸네일이 영상 제목·내용과 맞고 과장·거짓이 없는가", 15),
          ("channel", "예시로 준 채널 썸네일들과 같은 채널처럼 보이는가", 15),
          ("text", "글자 수가 적당한가 (짧을수록 좋음)", 10),
          ("contrast", "글자 대비와 전체 밝기", 10),
          ("overlay", "오른쪽 아래 영상 길이·맨 아래 진행 막대에 중요한 게 가리지 않는가", 10),
          ("distinct", "제목을 되풀이하지 않고 새 궁금증을 더하는가", 5))


def score(cl, img, title, text, desc, examples=(), checks=(), spots=()):
    """완성 썸네일 종합 점수. 먼저 정책(잔인·시신·선정·극단주의 상징·욕설·거짓 약속) 통과 여부, 그다음 항목별 점수와 고칠 점."""
    content = [{"type": "text", "text": "평가할 썸네일:"}, image_block(img, 1024)]
    for i, p in enumerate(examples):
        content += [{"type": "text", "text": f"채널 예시 썸네일 {i + 1}:"}, image_block(p, 512)]
    content.append({"type": "text", "text": (
        f"유튜브 역사·전쟁사 채널의 썸네일을 평가합니다.\n영상 제목: {title}\n썸네일 문구: {text}\n영상 설명: {desc[:500]}\n"
        "프로그램이 잰 값: " + "; ".join(m for _, m in checks) + "\n\n"
        "1) policy_ok: YouTube 썸네일 정책상 문제가 없는가 (충격·혐오를 노린 폭력, 피·상처·시신, 극단주의 상징이 중심, 선정성·노출, "
        "저속한 글자, 영상에 없는 것을 보여주는 오해 소지, 다른 채널·인물 사칭이면 false). "
        "policy_reason: false면 이유.\n"
        "2) 항목별 점수 (0~배점): " + ", ".join(f"{k} = {d} (0~{w})" for k, d, w in RUBRIC) + "\n"
        "3) tips: 고치면 좋은 점 1~3개, 짧은 한국어로. 이미 좋으면 빈 목록.\n"
        "4) title_score: 영상 제목만 따로 0~100점 (YouTube 공식 팁 기준: 영상 내용과 정확히 맞음, 짧고 가장 중요한 말을 앞에(앞 40자), "
        "채널 이름·회차 번호는 끝에, 대문자·이모지는 절제, 검색형(무엇을 볼지 분명) 또는 궁금증형, 구체적인 숫자·인물·사건, "
        "과장·낚시 없음, 썸네일 문구와 겹치지 않고 서로 보완). title_tips: 제목을 고치면 좋은 점 0~2개, 짧은 한국어로.\n"
        "5) inset_ok: 썸네일 위에 따로 얹은 네모난 실제 사진이 배경 그림 속 인물의 얼굴이나 핵심 대상을 가리지 않으면 true, "
        "가리면 false (얹은 사진이 없으면 true)."
        + ("\n6) better_spot: inset_ok가 false일 때, 같은 사진을 옮기면 배경 인물 얼굴·핵심 대상과 글자를 가리지 않을 자리 번호 "
           "(1280x720 기준 x,y,너비,높이): " + ", ".join(spots) + ". 그런 자리가 없으면 -1. inset_ok가 true면 -1." if spots else ""))})
    props = {"policy_ok": {"type": "boolean"}, "policy_reason": {"type": "string"},
             "tips": {"type": "array", "items": {"type": "string"}},
             "title_score": {"type": "integer"}, "title_tips": {"type": "array", "items": {"type": "string"}},
             "inset_ok": {"type": "boolean"}}
    if spots:
        props["better_spot"] = {"type": "integer"}
    props |= {k: {"type": "integer"} for k, _, _ in RUBRIC}
    out = ask(cl, content, _schema(props))
    out["total"] = sum(max(0, min(w, int(out.get(k, 0)))) for k, _, w in RUBRIC) if out.get("policy_ok", True) else 0
    return out


_GATE, _LAST = threading.Lock(), [0.0]


def _open(req, timeout):
    """위키미디어 요청: 한 번에 하나씩, 1초 간격 (여럿을 한꺼번에 보내면 429로 막힘). 429면 잠깐 쉬었다가 다시."""
    for attempt in range(3):
        with _GATE:
            time.sleep(max(0.0, _LAST[0] + 1.0 - time.time()))
            _LAST[0] = time.time()
        try:
            with urllib.request.urlopen(req, timeout=timeout) as res:
                return res.read()
        except urllib.error.HTTPError as e:
            if e.code != 429 or attempt == 2:
                raise
            time.sleep(min(int(e.headers.get("Retry-After") or 5), 20))


def search_commons(query, limit=6):
    """위키미디어 공용에서 저작권이 풀린(퍼블릭 도메인·CC0) 사진만. [{'title', 'url'}]"""
    # 썸네일은 위키미디어 표준 크기(960)로 받는다: 다른 크기는 서버가 막을 수 있음 (https://w.wiki/GHai)
    params = {"action": "query", "format": "json", "generator": "search", "gsrnamespace": 6, "gsrlimit": limit,
              "gsrsearch": f"{query} filetype:bitmap", "prop": "imageinfo", "iiprop": "url|extmetadata|size",
              "iiurlwidth": 960, "iiextmetadatafilter": "LicenseShortName|Copyrighted"}
    req = urllib.request.Request(f"{COMMONS}?{urllib.parse.urlencode(params)}", headers={"User-Agent": UA})
    pages = json.loads(_open(req, 30)).get("query", {}).get("pages", {})
    out = []
    for p in sorted(pages.values(), key=lambda p: p.get("index", 0)):
        info = (p.get("imageinfo") or [{}])[0]
        meta = {k: v.get("value", "") for k, v in info.get("extmetadata", {}).items()}
        free = meta.get("Copyrighted") == "False" or meta.get("LicenseShortName", "").lower().startswith(("public domain", "pd", "cc0"))
        if free and info.get("thumburl") and info.get("width", 0) >= 400:
            out.append({"title": p["title"].removeprefix("File:"), "url": info["thumburl"]})
    return out


def download(url):
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    return Image.open(io.BytesIO(_open(req, 60))).convert("RGB")


def find_photo(cl, queries, text, title, log=print, per_query=4, limit=6, skip=(), desc=""):
    """검색어로 후보를 모아 Claude가 하나 고른다. 맞는 게 없으면 None. (사진, 이름). skip: 빼고 고를 사진 이름"""
    cands, seen = [], set(skip)
    for q in queries[:3]:
        try:
            found = search_commons(q, per_query)
        except OSError as e:
            log(f"    (위키미디어 검색 실패: {q} — {e})")
            continue
        log(f"    위키미디어 '{q}': 저작권 풀린 사진 {len(found)}개")
        for c in found:
            if c["title"] not in seen and len(cands) < limit:
                try:
                    cands.append((c["title"], download(c["url"])))
                    seen.add(c["title"])
                except OSError:
                    pass
    if not cands:
        return None, ""
    content = []
    for i, (name, img) in enumerate(cands):
        content += [{"type": "text", "text": f"후보 {i}: {name}"}, image_block(img, 512)]
    content.append({"type": "text", "text": (
        f"유튜브 역사 영상 썸네일 한쪽에 작게 넣을 실제 사진을 고릅니다.\n영상 제목: {title}\n썸네일 문구: {text}\n"
        f"영상 내용: {desc[:500]}\n"
        "먼저 이 영상의 시대·나라·사건을 파악하고, 그와 다른 시대·지역의 그림·사진이면 아무리 보기 좋아도 고르지 말 것.\n"
        "기준: 주제와 직접 관련 있음, 작게(가로 500px) 줄여도 무엇인지 한눈에 알아봄 (인물 얼굴·유명한 물건·장면), "
        "잔인하거나 끔찍한 장면·시신·노출 없음, 흐릿하거나 빛바랜 사진·글자만 빽빽한 문서·작은 것이 잔뜩 모인 사진은 피함. "
        "사진 없이도 썸네일은 만들어지니, 확실히 좋은 게 없으면 choice는 -1.")})
    pick = ask(cl, content, _schema({"choice": {"type": "integer"}, "reason": {"type": "string"}}))
    i = pick.get("choice", -1)
    if not 0 <= i < len(cands):
        return None, ""
    name, img = cands[i]
    return img, name
