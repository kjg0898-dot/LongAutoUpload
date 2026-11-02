"""YouTube Data API 호출. 공식 API만 쓴다."""
import json
import os
import random
import time
from datetime import datetime, timezone

from core import PROG, YT, parse_dt

SECRET = PROG / "client_secret.json"


def secret_file():
    """열쇠 파일. 윈도우가 확장자를 숨겨 이름이 client_secret.json.json 이나 client_secret(1).json 이 돼도 찾는다."""
    found = sorted(PROG.glob("client_secret*.json"))
    return SECRET if SECRET.exists() else found[0] if found else SECRET
TOKENS = PROG / "토큰"
SCOPES = ["https://www.googleapis.com/auth/youtube.force-ssl",
          "https://www.googleapis.com/auth/yt-analytics.readonly"]  # 썸네일 클릭률 보고서 (YouTube Reporting API)
# videos.update에 보내면 안 되는(읽기 전용) 필드
READONLY_SNIPPET = {"thumbnails", "channelId", "channelTitle", "publishedAt", "liveBroadcastContent", "localized"}
WRITABLE_STATUS = {"privacyStatus", "publishAt", "license", "embeddable", "publicStatsViewable",
                   "selfDeclaredMadeForKids", "containsSyntheticMedia"}
# 할당량 (단위/회, 규칙.json): 목록 조회 1, 수정·썸네일·댓글·재생목록 추가 50. 하루 10,000 (모든 채널 합산)
COST_FILL = YT["cost_update"] + YT["cost_thumbnail"] + YT["cost_comment"] + YT["cost_playlist"]
COST_SCHEDULE, DAILY_QUOTA = YT["cost_update"], YT["daily_quota"]


class NotLoggedIn(Exception):
    pass


def login(key, interactive=True, fresh=False):
    """채널 하나의 로그인. fresh면 저장된 로그인을 지우고 브라우저에서 새로 받는다(다른 채널로 잘못 로그인했을 때)."""
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials
    from google_auth_oauthlib.flow import InstalledAppFlow

    token = TOKENS / f"{key}.json"
    if fresh:
        token.unlink(missing_ok=True)
    # 저장된 로그인은 받을 때 허용된 권한 그대로 (예전 로그인에 새 권한을 붙여 갱신하면 구글이 거절함)
    creds = Credentials.from_authorized_user_file(token) if token.exists() else None
    if creds and creds.expired and creds.refresh_token:
        try:
            creds.refresh(Request())
        except Exception:
            creds = None  # 만료된 로그인은 다시 받는다
    if not creds or not creds.valid:
        if not interactive:
            raise NotLoggedIn
        if not secret_file().exists():
            raise FileNotFoundError(f"열쇠 파일(client_secret_….json)이 없습니다. 구글연동_안내.md 4번대로 받아서 이 폴더에 넣어주세요: {PROG}")
        try:
            # 매번 계정·채널 선택 화면을 띄워 원하는 채널을 고르게 한다. 5분 안에 안 끝나면 포기
            os.environ.setdefault("OAUTHLIB_RELAX_TOKEN_SCOPE", "1")  # 통계 권한 체크를 빼고 허용해도 로그인은 되게
            flow = InstalledAppFlow.from_client_secrets_file(secret_file(), SCOPES)
            creds = flow.run_local_server(port=0, prompt="select_account consent", timeout_seconds=300)
            granted = flow.oauth2session.token.get("scope")
            if creds and granted:  # 실제로 허용된 권한만 적어 둔다
                info = json.loads(creds.to_json()) | {"scopes": granted.split() if isinstance(granted, str) else list(granted)}
                creds = Credentials.from_authorized_user_info(info)
        except Exception as e:
            raise RuntimeError(f"로그인을 마치지 못했습니다(5분이 지났거나 창을 닫음). 다시 눌러주세요. ({e})") from None
        if not creds or not creds.valid:
            raise RuntimeError("로그인을 마치지 못했습니다(5분이 지났거나 창을 닫음). 다시 눌러주세요.")
    TOKENS.mkdir(exist_ok=True)
    token.write_text(creds.to_json(), encoding="utf-8")
    return creds


def build_client(creds):
    from googleapiclient.discovery import build
    return build("youtube", "v3", credentials=creds, cache_discovery=False)


def avatar_url(yt):
    """내 채널 프로필 사진 주소 (가장 큰 것, 보통 800x800)."""
    th = execute(yt.channels().list(part="snippet", mine=True))["items"][0]["snippet"].get("thumbnails", {})
    return next((th[k]["url"] for k in ("high", "medium", "default") if k in th), None)


def whoami(yt):
    ch = execute(yt.channels().list(part="snippet", mine=True))["items"][0]
    return ch["id"], ch["snippet"]["title"]


def client_for(ch, interactive=False):
    """설정의 채널로 로그인된 클라이언트. 다른 채널로 로그인돼 있으면 멈춘다."""
    if not ch["channel_id"]:
        raise NotLoggedIn
    yt = build_client(login(ch["key"], interactive))
    cid, title = whoami(yt)
    if cid != ch["channel_id"]:
        raise RuntimeError(f"'{ch['name']}' 설정과 다른 채널({title})로 로그인돼 있습니다. 설정에서 다시 로그인하세요.")
    return yt


def get_videos(yt, ids):
    out = []
    for i in range(0, len(ids), 50):
        out += execute(yt.videos().list(part="snippet,status,fileDetails", id=",".join(ids[i:i + 50])))["items"]
    return out


def recent_videos(yt, limit=200):
    # shortcut: 최근 업로드 200개만 본다. 묶음이 그보다 크면 limit을 올린다
    uploads = execute(yt.channels().list(part="contentDetails", mine=True))["items"][0][
        "contentDetails"]["relatedPlaylists"]["uploads"]
    ids, page = [], None
    while len(ids) < limit:
        res = execute(yt.playlistItems().list(part="contentDetails", playlistId=uploads, maxResults=50,
                                              pageToken=page))
        ids += [i["contentDetails"]["videoId"] for i in res["items"]]
        page = res.get("nextPageToken")
        if not page:
            break
    return get_videos(yt, ids)


def publish_times(yt, limit=50):
    """최근 영상이 공개됐거나 공개될 시각 (예약은 publishAt, 공개된 것은 publishedAt)."""
    out = []
    for v in recent_videos(yt, limit):
        s = v["status"]
        t = s.get("publishAt") or (v["snippet"].get("publishedAt") if s.get("privacyStatus") == "public" else None)
        if t:
            out.append(parse_dt_utc(t))
    return out


def file_name(video):
    return video.get("fileDetails", {}).get("fileName", "")


def is_untouched(video):
    """아직 공개·예약 전 (초안이거나 2단계에서 일부공개로만 바꾼 것)."""
    return video["status"].get("privacyStatus") != "public" and not video["status"].get("publishAt")


def last_scheduled(videos):
    now = datetime.now(timezone.utc)
    times = [parse_dt_utc(v["status"].get("publishAt")) for v in videos]
    return max((t for t in times if t and t > now), default=None)


def parse_dt_utc(s):
    return datetime.fromisoformat(s.replace("Z", "+00:00")) if s else None


def update_video(yt, video, snippet=None, status=None):
    body, parts = {"id": video["id"]}, []
    if snippet:
        body["snippet"] = {k: v for k, v in video["snippet"].items() if k not in READONLY_SNIPPET} | snippet
        parts.append("snippet")
    if status:
        body["status"] = {k: v for k, v in video["status"].items() if k in WRITABLE_STATUS} | status
        if body["status"].get("privacyStatus") != "private":
            body["status"].pop("publishAt", None)  # 예약 시간은 비공개일 때만 둘 수 있다
        parts.append("status")
    return execute(yt.videos().update(part=",".join(parts), body=body))


def _status(e):
    return getattr(getattr(e, "resp", None), "status", None)


def execute(request, retry=(409, 429, 500, 503), waits=(3, 10, 30)):
    """같은 영상을 연달아 고칠 때의 409, 잠깐의 서버 오류·속도 제한은 기다렸다 다시 보낸다. 할당량 소진은 바로 멈춘다."""
    for wait in (*waits, None):
        try:
            return request.execute()
        except Exception as e:
            code = _status(e)
            rate_limited = code == 403 and "ateLimitExceeded" in error_text(e)
            if wait is None or is_quota(e) or not (code in retry or rate_limited):
                raise
            time.sleep(wait + random.random() * 2)


REACH = "channel_reach_basic_a1"  # 영상별·날짜별 썸네일 노출 수와 클릭률


def reach_reports(creds, after=""):
    """썸네일 클릭률 보고서 (하루에 CSV 하나). 처음이면 보고서 작업을 만든다:
    지난 30일치는 며칠 안에, 그 뒤로는 매일 이틀 늦게 나온다. after: 이 시각 뒤에 만들어진 것만 [{id, createTime, csv}]"""
    from google.auth.transport.requests import AuthorizedSession
    from googleapiclient.discovery import build
    rep = build("youtubereporting", "v1", credentials=creds, cache_discovery=False)
    job = next((j for j in execute(rep.jobs().list()).get("jobs", []) if j["reportTypeId"] == REACH), None) \
        or execute(rep.jobs().create(body={"reportTypeId": REACH, "name": "LongAutoUpload 썸네일 클릭률"}))
    found, page = [], None
    while True:
        res = execute(rep.jobs().reports().list(jobId=job["id"], pageToken=page, **({"createdAfter": after} if after else {})))
        found += res.get("reports", [])
        if not (page := res.get("nextPageToken")):
            break
    session = AuthorizedSession(creds)
    for r in found:
        res = session.get(r["downloadUrl"], timeout=60)
        res.raise_for_status()
        r["csv"] = res.content.decode("utf-8-sig")
    return found


# ---------- 댓글 답글 ----------
def fresh_public(yt, hours, limit=30):
    """공개된 지 hours시간 안 된 내 영상들."""
    from datetime import timedelta
    now = datetime.now(timezone.utc)
    out = []
    for v in recent_videos(yt, limit):
        pub = parse_dt_utc(v["snippet"].get("publishedAt"))
        if v["status"].get("privacyStatus") == "public" and pub and now - pub <= timedelta(hours=hours):
            out.append(v)
    return out


def _author(s):
    return (s.get("authorChannelId") or {}).get("value")


def new_comments(yt, video_id, channel_id):
    """영상 댓글 중 내 것이 아니고 아직 내가 답하지 않은 것 (오래된 것부터). 한 번에 100개 (할당량 1)."""
    res = execute(yt.commentThreads().list(part="snippet,replies", videoId=video_id, maxResults=100,
                                           order="time", textFormat="plainText"))
    out = []
    for th in res.get("items", []):
        top = th["snippet"]["topLevelComment"]
        s = top["snippet"]
        replies = (th.get("replies") or {}).get("comments", [])
        if _author(s) == channel_id or any(_author(r["snippet"]) == channel_id for r in replies):
            continue
        out.append({"id": top["id"], "text": s.get("textOriginal") or s.get("textDisplay", ""),
                    "author": s.get("authorDisplayName", ""), "likes": s.get("likeCount", 0), "at": s.get("publishedAt", "")})
    return out[::-1]


def my_replies(yt, channel_id, limit=30):
    """내 채널이 예전에 단 답글들 (말투를 배우는 예시). [{'comment', 'reply'}]"""
    res = execute(yt.commentThreads().list(part="snippet,replies", allThreadsRelatedToChannelId=channel_id,
                                           maxResults=100, textFormat="plainText"))
    out = []
    for th in res.get("items", []):
        top = th["snippet"]["topLevelComment"]["snippet"]
        for r in (th.get("replies") or {}).get("comments", []):
            if _author(r["snippet"]) == channel_id:
                out.append({"comment": top.get("textOriginal") or top.get("textDisplay", ""),
                            "reply": r["snippet"].get("textOriginal") or r["snippet"].get("textDisplay", "")})
    return out[:limit]


def reply(yt, parent_id, text):
    """댓글에 답글 (할당량 50). 서버 오류로 다시 보내면 두 번 달릴 수 있어 재시도하지 않는다."""
    return execute(yt.comments().insert(part="snippet", body={"snippet": {"parentId": parent_id, "textOriginal": text}}),
                   retry=())


def set_thumbnail(yt, video_id, path):
    from googleapiclient.http import MediaFileUpload
    execute(yt.thumbnails().set(videoId=video_id, media_body=MediaFileUpload(str(path))))


def add_to_playlist(yt, playlist_id, video_id):
    found = execute(yt.playlistItems().list(part="id", playlistId=playlist_id, videoId=video_id))["items"]
    if not found:
        execute(yt.playlistItems().insert(part="snippet", body={"snippet": {  # 서버 오류로 다시 넣으면 두 번 들어갈 수 있음
            "playlistId": playlist_id, "resourceId": {"kind": "youtube#video", "videoId": video_id}}}), retry=(409,))


def post_comment(yt, video_id, text):
    # 댓글은 실제로 달렸는데 응답만 실패했을 수 있어 서버 오류로는 다시 보내지 않는다 (중복 댓글 방지)
    execute(yt.commentThreads().insert(part="snippet", body={"snippet": {
        "videoId": video_id, "topLevelComment": {"snippet": {"textOriginal": text}}}}), retry=(409,))


def to_utc(when):
    return parse_dt(when).astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def schedule(yt, video, when):
    try:
        update_video(yt, video, status={"privacyStatus": "private", "publishAt": to_utc(when)})
    except Exception as e:
        if "invalidPublishAt" in error_text(e):
            raise RuntimeError("YouTube가 예약을 거부했습니다(이미 공개된 적 있는 영상은 API로 예약 불가). "
                               "Studio에서 직접 예약해주세요.") from None
        raise


def error_text(e):
    from googleapiclient.errors import HttpError
    if isinstance(e, HttpError):
        try:
            return f"{e.reason} ({e.error_details[0].get('reason', '')})" if e.error_details else str(e.reason)
        except Exception:
            return str(e)
    return str(e)


def is_quota(e):
    return "quotaExceeded" in str(e) or "quotaExceeded" in error_text(e)
