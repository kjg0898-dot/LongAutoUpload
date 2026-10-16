"""YouTube Data API 호출. 공식 API만 쓴다."""
from datetime import datetime, timezone

from core import PROG, parse_dt

SECRET = PROG / "client_secret.json"
TOKENS = PROG / "토큰"
SCOPES = ["https://www.googleapis.com/auth/youtube.force-ssl"]
# videos.update에 보내면 안 되는(읽기 전용) 필드
READONLY_SNIPPET = {"thumbnails", "channelId", "channelTitle", "publishedAt", "liveBroadcastContent", "localized"}
WRITABLE_STATUS = {"privacyStatus", "publishAt", "license", "embeddable", "publicStatsViewable",
                   "selfDeclaredMadeForKids", "containsSyntheticMedia"}


class NotLoggedIn(Exception):
    pass


def login(key, interactive=True):
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials
    from google_auth_oauthlib.flow import InstalledAppFlow

    token = TOKENS / f"{key}.json"
    creds = Credentials.from_authorized_user_file(token, SCOPES) if token.exists() else None
    if creds and creds.expired and creds.refresh_token:
        try:
            creds.refresh(Request())
        except Exception:
            creds = None  # 만료된 로그인은 다시 받는다
    if not creds or not creds.valid:
        if not interactive:
            raise NotLoggedIn
        if not SECRET.exists():
            raise FileNotFoundError(f"client_secret.json이 없습니다. 구글연동_안내.md대로 받아서 넣어주세요: {PROG}")
        creds = InstalledAppFlow.from_client_secrets_file(SECRET, SCOPES).run_local_server(port=0)
    TOKENS.mkdir(exist_ok=True)
    token.write_text(creds.to_json(), encoding="utf-8")
    return creds


def build_client(creds):
    from googleapiclient.discovery import build
    return build("youtube", "v3", credentials=creds, cache_discovery=False)


def whoami(yt):
    ch = yt.channels().list(part="snippet", mine=True).execute()["items"][0]
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
        out += yt.videos().list(part="snippet,status,fileDetails", id=",".join(ids[i:i + 50])).execute()["items"]
    return out


def recent_videos(yt, limit=200):
    # shortcut: 최근 업로드 200개만 본다. 묶음이 그보다 크면 limit을 올린다
    uploads = yt.channels().list(part="contentDetails", mine=True).execute()["items"][0][
        "contentDetails"]["relatedPlaylists"]["uploads"]
    ids, page = [], None
    while len(ids) < limit:
        res = yt.playlistItems().list(part="contentDetails", playlistId=uploads, maxResults=50,
                                      pageToken=page).execute()
        ids += [i["contentDetails"]["videoId"] for i in res["items"]]
        page = res.get("nextPageToken")
        if not page:
            break
    return get_videos(yt, ids)


def by_filename(videos):
    return {v.get("fileDetails", {}).get("fileName", "").lower(): v for v in videos}


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
    return yt.videos().update(part=",".join(parts), body=body).execute()


def set_thumbnail(yt, video_id, path):
    from googleapiclient.http import MediaFileUpload
    yt.thumbnails().set(videoId=video_id, media_body=MediaFileUpload(str(path))).execute()


def add_to_playlist(yt, playlist_id, video_id):
    found = yt.playlistItems().list(part="id", playlistId=playlist_id, videoId=video_id).execute()["items"]
    if not found:
        yt.playlistItems().insert(part="snippet", body={"snippet": {
            "playlistId": playlist_id, "resourceId": {"kind": "youtube#video", "videoId": video_id}}}).execute()


def post_comment(yt, video_id, text):
    yt.commentThreads().insert(part="snippet", body={"snippet": {
        "videoId": video_id, "topLevelComment": {"snippet": {"textOriginal": text}}}}).execute()


def schedule(yt, video, when):
    utc = parse_dt(when).astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    update_video(yt, video, status={"privacyStatus": "private", "publishAt": utc})


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
