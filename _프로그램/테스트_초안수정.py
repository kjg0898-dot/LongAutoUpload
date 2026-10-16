"""초안을 API로 고칠 수 있는지 확인하는 테스트.

제목 끝에 ' 테스트'를 붙였다가 원래 제목으로 되돌린다. 영상은 삭제되지 않는다.
결과는 같은 폴더의 테스트결과.txt에 저장된다.
"""
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
SECRET = HERE / "client_secret.json"
TOKEN_DIR = HERE / "토큰"
RESULT = HERE / "테스트결과.txt"
SCOPES = ["https://www.googleapis.com/auth/youtube.force-ssl"]
# videos.update에 보내면 안 되는 읽기 전용 필드
READONLY_SNIPPET = {"thumbnails", "channelId", "channelTitle", "publishedAt", "liveBroadcastContent", "localized"}

lines = []


def log(msg=""):
    print(msg)
    lines.append(str(msg))


def ensure_packages():
    try:
        import googleapiclient  # noqa: F401
        import google_auth_oauthlib  # noqa: F401
    except ImportError:
        print("필요한 부품을 설치합니다 (처음 한 번만)...")
        subprocess.check_call([sys.executable, "-m", "pip", "install", "-r", str(HERE / "requirements.txt")])


def login(name):
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials
    from google_auth_oauthlib.flow import InstalledAppFlow

    TOKEN_DIR.mkdir(exist_ok=True)
    token = TOKEN_DIR / f"{name}.json"
    creds = Credentials.from_authorized_user_file(token, SCOPES) if token.exists() else None
    if creds and creds.expired and creds.refresh_token:
        try:
            creds.refresh(Request())
        except Exception:
            creds = None  # 만료된 로그인은 다시 받는다
    if not creds or not creds.valid:
        print("브라우저가 열리면 테스트할 채널의 구글 계정으로 로그인하고, 채널을 선택하세요.")
        creds = InstalledAppFlow.from_client_secrets_file(SECRET, SCOPES).run_local_server(port=0)
    token.write_text(creds.to_json(), encoding="utf-8")
    return creds


def set_title(yt, video, title):
    snippet = {k: v for k, v in video["snippet"].items() if k not in READONLY_SNIPPET}
    snippet["title"] = title
    yt.videos().update(part="snippet", body={"id": video["id"], "snippet": snippet}).execute()


def get_video(yt, vid):
    items = yt.videos().list(part="snippet,status,fileDetails", id=vid).execute()["items"]
    return items[0] if items else None


def main():
    if not SECRET.exists():
        log(f"client_secret.json이 없습니다. 안내서대로 받아서 이 폴더에 넣어주세요:\n  {HERE}")
        return
    ensure_packages()
    from googleapiclient.discovery import build

    yt = build("youtube", "v3", credentials=login("테스트"))

    ch = yt.channels().list(part="snippet,contentDetails", mine=True).execute()["items"][0]
    log(f"채널: {ch['snippet']['title']} ({ch['id']})")

    # 확인 1: 초안이 업로드 목록에 보이는지, 원본 파일 이름을 읽을 수 있는지
    uploads = ch["contentDetails"]["relatedPlaylists"]["uploads"]
    items = yt.playlistItems().list(part="contentDetails", playlistId=uploads, maxResults=10).execute()["items"]
    ids = [i["contentDetails"]["videoId"] for i in items]
    log("\n[최근 업로드 10개]")
    if ids:
        for n, v in enumerate(yt.videos().list(part="snippet,status,fileDetails", id=",".join(ids)).execute()["items"], 1):
            st = v["status"]
            log(f"{n:2}. {v['id']} | {st.get('privacyStatus')} / {st.get('uploadStatus')} | "
                f"파일명={v.get('fileDetails', {}).get('fileName', '(없음)')} | {v['snippet']['title']}")

    # 확인 2: 초안 제목을 바꿨다가 되돌릴 수 있는지
    log("\nStudio에서 초안을 열면 주소가 studio.youtube.com/video/<영상ID>/edit 입니다.")
    vid = input("테스트할 초안의 영상ID를 붙여넣고 엔터: ").strip()
    log(f"영상ID: {vid}")
    video = get_video(yt, vid)
    if not video:
        log("그 영상을 찾지 못했습니다. 로그인한 채널의 영상이 맞는지 확인해 주세요.")
        return
    original = video["snippet"]["title"]
    log(f"원래 제목: {original}")
    log(f"상태(전): {video['status']}")

    try:
        set_title(yt, video, original[:90] + " 테스트")
        changed = get_video(yt, vid)
        log(f"바뀐 제목: {changed['snippet']['title']}")
        log(f"상태(후): {changed['status']}")
    except Exception as e:
        log(f"제목 수정 실패: {e}")
        return
    finally:
        try:
            set_title(yt, get_video(yt, vid), original)
            log(f"되돌린 제목: {get_video(yt, vid)['snippet']['title']}")
        except Exception as e:
            log(f"되돌리기 실패! Studio에서 제목을 직접 '{original}'로 바꿔주세요. ({e})")
    log("\n결과: 초안 수정 가능")


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        log(f"오류: {e!r}")
    RESULT.write_text("\n".join(lines), encoding="utf-8")
    print(f"\n결과를 저장했습니다: {RESULT}\n이 파일 내용을 Claude에게 붙여넣어 주세요.")
    input("엔터를 누르면 닫힙니다.")
