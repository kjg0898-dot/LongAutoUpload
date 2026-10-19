"""전체 기능 실제 테스트: 프로그램이 자동으로 하는 일을 테스트용 초안 하나에 실제로 해 보고 결과를 확인한다.

버려도 되는 테스트용 초안으로 하세요 (예: 아무 영상이나 올려둔 초안). 끝나면 그 영상을 지울지 묻습니다.
결과는 같은 폴더의 전체기능_결과.txt 에 저장됩니다. 할당량은 약 700 정도 씁니다.
"""
import struct
import sys
import tempfile
import time
import zlib
from datetime import datetime, timedelta
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
RESULT = HERE / "전체기능_결과.txt"
lines = []


def log(msg=""):
    print(msg)
    lines.append(str(msg))


def ok(name, good, detail=""):
    log(f"{'✓' if good else '✗'} {name}" + (f" — {detail}" if detail else ""))
    return good


def test_png(path):
    """1280x720 단색 PNG (썸네일 시험용, 추가 부품 없이 만든다)."""
    raw = b"".join(b"\x00" + bytes((30, 90, 160)) * 1280 for _ in range(720))
    chunk = lambda t, d: struct.pack(">I", len(d)) + t + d + struct.pack(">I", zlib.crc32(t + d))  # noqa: E731
    path.write_bytes(b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", 1280, 720, 8, 2, 0, 0, 0))
                     + chunk(b"IDAT", zlib.compress(raw, 9)) + chunk(b"IEND", b""))
    return path


def main():
    import gui
    gui.ensure_packages()
    import core
    import yt

    client = yt.build_client(yt.login("테스트"))
    cid, title = yt.whoami(client)
    log(f"채널: {title} ({cid})")

    def get():
        return yt.get_videos(client, [vid])[0]

    def wait_for(check, tries=(0, 5, 10, 15, 30, 30)):
        """YouTube가 수정을 늦게 보여줄 수 있어 몇 번 다시 읽는다. (맞았는지, 마지막으로 읽은 영상, 걸린 초)"""
        spent = 0
        for t in tries:
            time.sleep(t)
            spent += t
            v = get()
            if check(v):
                return True, v, spent
        return False, v, spent

    log("\n버려도 되는 테스트용 초안의 주소를 붙여넣으세요 (실제로 올릴 영상 말고).")
    vid = core.video_id(input("영상 주소나 ID: "))
    if not yt.get_videos(client, [vid]):
        return log("그 영상을 이 채널에서 찾지 못했습니다. 주소를 확인해주세요.")
    video = get()
    log(f"영상: {vid} | {video['status'].get('privacyStatus')} | 파일={yt.file_name(video)} | {video['snippet']['title']}")
    if input("이 영상으로 테스트합니다. 제목·설명·썸네일 등이 바뀝니다. 계속할까요? (y/n): ").strip().lower() != "y":
        return log("취소했습니다.")

    s = video["snippet"]
    log("\n[A] 초안에 들어 있는 Studio 기본 설정")
    ok("제목 틀", bool(s.get("title")), s.get("title", ""))
    ok("설명 틀", True, (s.get("description", "") or "(비어 있음)")[:150].replace("\n", " / "))
    ok("태그 틀", True, ", ".join(s.get("tags", [])) or "(없음)")
    ok("카테고리", True, s.get("categoryId", "(없음)"))

    log("\n[B] 2단계와 같은 방식: 제목(] 뒤)·설명(맨 앞)·태그 합치기 + 일부공개 (한 번에)")
    ch = {**core.DEFAULT_CHANNEL}
    new_title = core.merge(s.get("title", ""), "전체기능 테스트 제목", ch["title_mode"], ch["title_marker"], " ")
    new_desc = core.merge(s.get("description", ""), "전체기능 테스트 설명입니다.\n#테스트", ch["desc_mode"], "", "\n\n")
    new_tags = core.merge_tags(s.get("tags", []), ["전체기능테스트"], ch["tags_mode"])
    core.check_text(new_title, new_desc)
    try:
        yt.update_video(client, video, {"title": new_title, "description": new_desc, "tags": new_tags},
                        {"privacyStatus": "unlisted"})
        good, v, t = wait_for(lambda v: v["snippet"]["title"] == new_title and v["status"]["privacyStatus"] == "unlisted"
                              and "전체기능테스트" in v["snippet"].get("tags", []))
        ok("제목", v["snippet"]["title"] == new_title, v["snippet"]["title"])
        ok("설명", v["snippet"].get("description", "").startswith("전체기능 테스트 설명"),
           v["snippet"].get("description", "")[:80].replace("\n", " / "))
        ok("태그", "전체기능테스트" in v["snippet"].get("tags", []), ", ".join(v["snippet"].get("tags", [])))
        ok("일부공개", v["status"]["privacyStatus"] == "unlisted", f"반영까지 약 {t}초")
    except Exception as e:
        ok("제목·설명·태그·일부공개", False, yt.error_text(e))

    log("\n[C] 썸네일")
    try:
        with tempfile.TemporaryDirectory() as d:
            yt.set_thumbnail(client, vid, test_png(Path(d) / "test.png"))
        ok("썸네일 올리기", True, "Studio에서 파란 썸네일로 바뀌었는지 확인해보세요")
    except Exception as e:
        ok("썸네일 올리기", False, yt.error_text(e) + " (휴대폰 인증이 안 된 채널이면 썸네일을 못 바꿉니다)")

    category = "22" if s.get("categoryId") == "27" else "27"  # 원래와 다른 값으로 바꿔야 반영됐는지 알 수 있다
    log(f"\n[D] 설정에서 바꿀 수 있는 항목 (카테고리 {category}, AI 합성 표시 예, 퍼가기 안 함)")
    try:
        time.sleep(5)
        yt.update_video(client, get(), {"categoryId": category},
                        {"containsSyntheticMedia": True, "embeddable": False})
        good, v, t = wait_for(lambda v: v["snippet"].get("categoryId") == category
                              and v["status"].get("containsSyntheticMedia") is True and v["status"].get("embeddable") is False)
        ok("카테고리", v["snippet"].get("categoryId") == category, f"{v['snippet'].get('categoryId')} / 반영까지 약 {t}초")
        ok("AI 합성 표시", v["status"].get("containsSyntheticMedia") is True, str(v["status"].get("containsSyntheticMedia")))
        ok("퍼가기", v["status"].get("embeddable") is False, str(v["status"].get("embeddable")))
    except Exception as e:
        ok("설정 항목", False, yt.error_text(e))

    log("\n[E] 재생목록 (테스트용 비공개 재생목록을 만들었다가 지움)")
    playlist = None
    try:
        playlist = yt.execute(client.playlists().insert(part="snippet,status", body={
            "snippet": {"title": "LongAutoUpload 테스트 (지워도 됨)"}, "status": {"privacyStatus": "private"}}))["id"]
        for t in (5, 10, 15, 30):  # 방금 만든 재생목록은 YouTube가 알아보는 데 시간이 걸린다
            time.sleep(t)
            try:
                yt.add_to_playlist(client, playlist, vid)
                break
            except Exception as e:
                if "playlistNotFound" not in yt.error_text(e) or t == 30:
                    raise
        items = yt.execute(client.playlistItems().list(part="id", playlistId=playlist, videoId=vid))["items"]
        ok("재생목록에 넣기", bool(items))
    except Exception as e:
        ok("재생목록에 넣기", False, yt.error_text(e))
    finally:
        if playlist:
            try:
                yt.execute(client.playlists().delete(id=playlist))
                ok("테스트 재생목록 지우기", True)
            except Exception as e:
                ok("테스트 재생목록 지우기", False, f"{yt.error_text(e)} — YouTube에서 직접 지워주세요")

    log("\n[F] 댓글 (일부공개 영상에 달았다가 지움)")
    try:
        yt.post_comment(client, vid, "LongAutoUpload 전체기능 테스트 댓글입니다.")
        found = None
        for t in (3, 5, 10, 15):
            time.sleep(t)
            threads = yt.execute(client.commentThreads().list(part="snippet", videoId=vid, maxResults=20))["items"]
            found = next((c for c in threads if "전체기능 테스트 댓글" in
                          c["snippet"]["topLevelComment"]["snippet"].get("textOriginal", "")), None)
            if found:
                break
        ok("댓글 달기", bool(found), "목록에서 확인됨" if found else "목록에 아직 안 보임 (Studio 댓글에서 확인)")
        if found:
            yt.execute(client.comments().delete(id=found["snippet"]["topLevelComment"]["id"]))
            ok("테스트 댓글 지우기", True)
    except Exception as e:
        ok("댓글 달기", False, yt.error_text(e))

    log("\n[G] 4단계와 같은 방식: 예약 (30일 뒤)")
    try:
        time.sleep(5)
        when = core.fmt_dt(datetime.now(core.KST) + timedelta(days=30))
        yt.schedule(client, get(), when)
        want = core.parse_dt(when)
        good, v, t = wait_for(lambda v: yt.parse_dt_utc(v["status"].get("publishAt")) == want)
        ok("예약", good and v["status"]["privacyStatus"] == "private",
           f"{v['status'].get('privacyStatus')} / {v['status'].get('publishAt')} / 반영까지 약 {t}초")
    except Exception as e:
        ok("예약", False, yt.error_text(e))

    log("\n[H] 정리")
    v = get()
    log(f"마지막 상태: {v['status'].get('privacyStatus')} / 예약={v['status'].get('publishAt', '-')} | {v['snippet']['title']}")
    if input("테스트가 끝났습니다. 이 테스트용 영상을 YouTube에서 지울까요? (y/n): ").strip().lower() == "y":
        try:
            yt.execute(client.videos().delete(id=vid))
            ok("테스트 영상 지우기", True)
        except Exception as e:
            ok("테스트 영상 지우기", False, f"{yt.error_text(e)} — Studio에서 직접 지워주세요")
    else:
        log("⚠ 영상을 남겼습니다. 30일 뒤로 예약돼 있으니, 쓰지 않을 거면 Studio에서 지우거나 예약을 해제해주세요.")


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        log(f"오류: {e!r}")
    RESULT.write_text("\n".join(lines), encoding="utf-8")
    print(f"\n결과를 저장했습니다: {RESULT}\n이 파일 내용을 Claude에게 붙여넣어 주세요.")
    input("엔터를 누르면 닫힙니다.")
