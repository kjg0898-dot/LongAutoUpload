"""Claude API + 위키미디어 공용: 썸네일 글자 쪽·강조할 말 정하기, 저작권이 풀린 실제 사진 찾아 고르기."""
import base64
import io
import json
import time
import urllib.error
import urllib.parse
import urllib.request

from PIL import Image

MODEL = "claude-opus-5-5"
UA = "LongAutoUpload/1.0 (https://github.com/kjg0898-dot/LongAutoUpload)"  # 위키미디어는 이름 없는 요청을 막는다
COMMONS = "https://commons.wikimedia.org/w/api.php"
PRICE = (4, 20)  # claude-opus-5-5 달러 / 100만 토큰 (입력, 출력)
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
            betas=["server-side-fallback-2026-07-01"], fallbacks="default",
            output_config={"effort": "medium", "format": {"type": "json_schema", "schema": schema}},
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


def plan(cl, base, text, title, desc, sides):
    """글자를 둘 쪽, 다른 색으로 강조할 말, 실제 사진 검색어."""
    content = [image_block(base), {"type": "text", "text": (
        "유튜브 썸네일을 만듭니다. 첨부한 사진이 배경이고, 그 위에 아래 문구를 크게 넣습니다.\n"
        f"문구 (/ 는 줄바꿈): {text}\n영상 제목: {title}\n영상 설명: {desc[:600]}\n\n"
        f"1) side: 문구를 둘 쪽 ({' 또는 '.join(sides)}). 사진 속 사람 얼굴이나 핵심 대상을 가리지 않는 쪽.\n"
        "2) accents: 문구에서 다른 색으로 강조할 핵심 말 1~2개. 문구에 있는 글자 그대로 (띄어쓰기 포함).\n"
        "3) queries: 위키미디어 공용(Wikimedia Commons)에서 이 영상 주제의 실제 사진·기록·지도·유물을 찾을 영어 검색어 3개. "
        "구체적인 인물·사건·장소·물건 이름으로.")}]
    out = ask(cl, content, _schema({"side": {"type": "string", "enum": list(sides)},
                                    "accents": {"type": "array", "items": {"type": "string"}},
                                    "queries": {"type": "array", "items": {"type": "string"}}}))
    out["accents"] = [a for a in out.get("accents", []) if a.strip() and a.strip() in text]  # 문구에 없는 말은 버림
    return out


def queries(cl, title, desc, text):
    """주제에 맞는 실제 사진·기록·지도·유물을 위키미디어 공용에서 찾을 영어 검색어 3개."""
    out = ask(cl, [{"type": "text", "text": (
        f"유튜브 영상의 썸네일에 넣을 실제 사진을 위키미디어 공용(Wikimedia Commons)에서 찾습니다.\n"
        f"영상 제목: {title}\n썸네일 문구: {text}\n영상 설명: {desc[:600]}\n\n"
        "이 주제의 실제 사진·기록·지도·유물·그림을 찾을 영어 검색어 3개를 주세요. 구체적인 인물·사건·장소·물건 이름으로.")}],
        _schema({"queries": {"type": "array", "items": {"type": "string"}}}))
    return out.get("queries", [])[:3]


def search_commons(query, limit=6):
    """위키미디어 공용에서 저작권이 풀린(퍼블릭 도메인·CC0) 사진만. [{'title', 'url'}]"""
    # 썸네일은 위키미디어 표준 크기(960)로 받는다: 다른 크기는 서버가 막을 수 있음 (https://w.wiki/GHai)
    params = {"action": "query", "format": "json", "generator": "search", "gsrnamespace": 6, "gsrlimit": limit,
              "gsrsearch": f"{query} filetype:bitmap", "prop": "imageinfo", "iiprop": "url|extmetadata|size",
              "iiurlwidth": 960, "iiextmetadatafilter": "LicenseShortName|Copyrighted"}
    req = urllib.request.Request(f"{COMMONS}?{urllib.parse.urlencode(params)}", headers={"User-Agent": UA})
    for attempt in range(3):  # 요청이 몰렸다(429)고 하면 잠깐 쉬었다가 다시
        try:
            with urllib.request.urlopen(req, timeout=30) as res:
                pages = json.load(res).get("query", {}).get("pages", {})
            break
        except urllib.error.HTTPError as e:
            if e.code != 429 or attempt == 2:
                raise
            time.sleep(min(int(e.headers.get("Retry-After") or 5), 20))
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
    for attempt in range(3):  # 요청이 몰렸다(429)고 하면 잠깐 쉬었다가 다시
        try:
            with urllib.request.urlopen(req, timeout=60) as res:
                return Image.open(io.BytesIO(res.read())).convert("RGB")
        except urllib.error.HTTPError as e:
            if e.code != 429 or attempt == 2:
                raise
            time.sleep(min(int(e.headers.get("Retry-After") or 5), 20))


def find_photo(cl, queries, text, title, log=print, per_query=4, limit=6, skip=()):
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
        "기준: 주제와 직접 관련 있음, 작게 줄여도 알아보기 쉬움, 잔인하거나 끔찍한 장면·시신·노출 없음, "
        "글자만 빽빽한 문서는 피함. 알맞은 게 없으면 choice는 -1.")})
    pick = ask(cl, content, _schema({"choice": {"type": "integer"}, "reason": {"type": "string"}}))
    i = pick.get("choice", -1)
    if not 0 <= i < len(cands):
        return None, ""
    name, img = cands[i]
    return img, name
