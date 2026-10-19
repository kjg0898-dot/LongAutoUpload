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
    schedule_test(yt, client, vid)


def schedule_test(yt, client, vid):
    """확인4: 일부공개로 바꾼 영상을 API로 예약할 수 있는지 (4단계가 이 방식). 끝나면 원래 상태로 되돌린다."""
    print("\n[추가 테스트] 이 초안을 잠깐 '일부공개'로 바꿨다가 30일 뒤로 예약해 보고, 원래 상태로 되돌립니다.")
    if input("할까요? (y/n): ").strip().lower() != "y":
        log("확인4) 건너뜀")
        return
    from datetime import datetime, timedelta
    import core
    original = {k: v for k, v in yt.get_videos(client, [vid])[0]["status"].items() if k in yt.WRITABLE_STATUS}
    log(f"원래 상태: {original}")
    try:
        yt.update_video(client, yt.get_videos(client, [vid])[0], status={"privacyStatus": "unlisted"})
        time.sleep(5)
        when = core.fmt_dt(datetime.now(core.KST) + timedelta(days=30))
        yt.schedule(client, yt.get_videos(client, [vid])[0], when)
        st = yt.get_videos(client, [vid])[0]["status"]
        ok = st.get("privacyStatus") == "private" and st.get("publishAt")
        log(f"확인4) 일부공개 → 예약: {'성공' if ok else '실패'} ({st.get('privacyStatus')} / {st.get('publishAt')})")
    except Exception as e:
        log(f"확인4) 일부공개 → 예약 실패: {yt.error_text(e)}")
    finally:
        time.sleep(5)
        try:  # status를 통째로 원래 값으로 보내면 예약 시간도 원래대로(없으면 지워짐) 돌아간다
            yt.execute(client.videos().update(part="status", body={"id": vid, "status": original}))
            now = yt.get_videos(client, [vid])[0]["status"]
            log(f"되돌린 상태: {now}")
            if now.get("publishAt") and not original.get("publishAt"):
                log("⚠ 예약 시간이 남아 있습니다! Studio에서 이 영상의 '공개 상태'를 열어 예약을 해제해주세요.")
        except Exception as e:
            log(f"상태 되돌리기 실패! Studio에서 이 영상의 공개 상태를 확인해주세요. ({yt.error_text(e)})")


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        log(f"오류: {e!r}")
    RESULT.write_text("\n".join(lines), encoding="utf-8")
    print(f"\n결과를 저장했습니다: {RESULT}\n이 파일 내용을 Claude에게 붙여넣어 주세요.")
    input("엔터를 누르면 닫힙니다.")
