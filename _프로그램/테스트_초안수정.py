"""초안을 API로 찾고 고칠 수 있는지 확인하는 테스트.

제목 끝에 ' 테스트'를 붙였다가 원래 제목으로 되돌린다. 영상은 삭제되지 않는다.
결과는 같은 폴더의 테스트결과.txt에 저장된다.
"""
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
RESULT = HERE / "테스트결과.txt"
lines = []


def log(msg=""):
    print(msg)
    lines.append(str(msg))


def main():
    import gui
    gui.ensure_packages()
    import yt

    client = yt.build_client(yt.login("테스트"))
    cid, title = yt.whoami(client)
    log(f"채널: {title} ({cid})")

    recent = yt.recent_videos(client, limit=50)
    log(f"\n[최근 업로드 {len(recent)}개]")
    for v in recent:
        st = v["status"]
        log(f"{v['id']} | {st.get('privacyStatus')} / {st.get('uploadStatus')} / 예약={st.get('publishAt', '-')} | "
            f"파일명={v.get('fileDetails', {}).get('fileName', '(없음)')} | {v['snippet']['title']}")

    log("\n※ 예약·공개된 영상 말고, Studio에 올리기만 한 '초안'으로 해주세요.")
    log("   Studio에서 초안을 열면 주소가 studio.youtube.com/video/<영상ID>/edit 입니다.")
    vid = input("초안의 영상ID를 붙여넣고 엔터: ").strip()
    log(f"영상ID: {vid}")
    log(f"확인1) 초안이 위 목록에 있음: {'예' if vid in {v['id'] for v in recent} else '아니요'}")
    found = yt.get_videos(client, [vid])
    if not found:
        log("그 영상을 찾지 못했습니다. 로그인한 채널의 영상이 맞는지 확인해 주세요.")
        return
    video = found[0]
    original = video["snippet"]["title"]
    log(f"확인2) 파일명: {video.get('fileDetails', {}).get('fileName', '(없음)')}")
    log(f"원래 제목: {original}")
    log(f"상태(전): {video['status']}")

    try:
        yt.update_video(client, video, {"title": original[:90] + " 테스트"})
        changed = yt.get_videos(client, [vid])[0]
        log(f"확인3) 바뀐 제목: {changed['snippet']['title']}")
        log(f"상태(후): {changed['status']}")
    except Exception as e:
        log(f"확인3) 제목 수정 실패: {yt.error_text(e)}")
        return
    finally:
        time.sleep(5)  # 바로 다시 고치면 YouTube가 409(conflict)를 낸다
        try:
            yt.update_video(client, yt.get_videos(client, [vid])[0], {"title": original})
            log(f"되돌린 제목: {yt.get_videos(client, [vid])[0]['snippet']['title']}")
        except Exception as e:
            log(f"되돌리기 실패! Studio에서 제목을 직접 '{original}'로 바꿔주세요. ({yt.error_text(e)})")
    log("\n결과: 초안 수정 가능")


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        log(f"오류: {e!r}")
    RESULT.write_text("\n".join(lines), encoding="utf-8")
    print(f"\n결과를 저장했습니다: {RESULT}\n이 파일 내용을 Claude에게 붙여넣어 주세요.")
    input("엔터를 누르면 닫힙니다.")
