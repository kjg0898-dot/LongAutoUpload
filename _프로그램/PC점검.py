"""PC 점검: 사용자 PC에서만 확인할 수 있는 것을 한 번에 확인하고 PC점검_결과.txt 로 남긴다.

1 환경(파이썬·부품·폴더·인터넷·업데이트·열쇠)  2 채널 로그인  3 엑셀이 열려 있을 때 감지
4 알림·폴더·브라우저 열기  5 리허설: 실제 프로그램 1→2→4단계를 테스트용 초안 하나로 실제 YouTube에 그대로
진짜 엑셀·영상 폴더·설정은 건드리지 않고 _프로그램/리허설 폴더에서 따로 한다 (끝나면 지움).
"""
import os
import platform
import shutil
import sys
import time
import traceback
from datetime import datetime, timedelta
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
RESULT = HERE / "PC점검_결과.txt"
SANDBOX = HERE / "리허설"
lines, score = [], {"ok": 0, "bad": 0}


def log(msg=""):
    print(msg)
    lines.append(str(msg))


def ok(name, good, detail=""):
    score["ok" if good else "bad"] += 1
    log(f"{'✓' if good else '✗'} {name}" + (f" — {detail}" if detail else ""))
    return good


def yes(question):
    return input(f"{question} (y/n): ").strip().lower() == "y"


def check(name, fn):
    """fn()이 (맞음, 설명)을 돌려주거나 오류를 내면 ✗."""
    try:
        good, detail = fn()
        return ok(name, good, detail)
    except Exception as e:
        return ok(name, False, f"{type(e).__name__}: {e}")


# ---------- 1. 환경 ----------
def env():
    log("\n[1] 환경")
    ok("윈도우", sys.platform == "win32", platform.platform())
    ok("파이썬 3.9 이상", sys.version_info >= (3, 9), sys.version.split()[0])
    ok("창 없이 실행(pythonw)", Path(sys.executable).with_name("pythonw.exe").exists() or sys.platform != "win32",
       str(Path(sys.executable).with_name("pythonw.exe")))
    check("tkinter(창)", lambda: (__import__("tkinter") is not None, "있음"))
    import gui
    gui.ensure_packages()
    for mod in ("openpyxl", "googleapiclient", "google_auth_oauthlib"):
        check(f"부품 {mod}", lambda m=mod: (True, getattr(__import__(m), "__version__", "있음")))
    if sys.platform == "win32":
        import ctypes
        check("고해상도 화면 설정", lambda: (ctypes.windll.shcore.SetProcessDpiAwareness(1) in (0, -2147024891), "가능"))
    root = HERE.parent
    ok("프로그램 폴더", True, str(root))
    ok("폴더 이름 LongAutoUpload", root.name == "LongAutoUpload", root.name)
    ok("OneDrive 밖", "onedrive" not in str(root).lower(),
       "OneDrive 안이면 동기화 중 파일이 잠겨 옮기기가 실패할 수 있음" if "onedrive" in str(root).lower() else "")

    def write_test():
        p = HERE / "쓰기시험_한글.tmp"
        p.write_text("시험", encoding="utf-8")
        moved = p.with_name("쓰기시험_옮김.tmp")
        os.replace(p, moved)
        moved.unlink()
        return True, "만들기·옮기기·지우기 됨"
    check("폴더에 쓰기(한글 이름)", write_test)

    def recycle_test():  # 청소 기능이 쓰는 휴지통 보내기
        import core
        folder = HERE / "휴지통시험_폴더"
        folder.mkdir(exist_ok=True)
        (folder / "안.txt").write_text("시험", encoding="utf-8")
        file = HERE / "휴지통시험_파일.txt"
        file.write_text("시험", encoding="utf-8")
        left = core.trash([folder, file])
        return not left, "휴지통으로 보냄 (휴지통에 '휴지통시험' 2개가 생겼으면 비워도 됩니다)" if not left else f"남음: {left}"
    check("청소: 휴지통으로 보내기", recycle_test)
    import urllib.request

    def reach(url):
        with urllib.request.urlopen(url, timeout=20) as r:
            return r.status == 200, f"응답 {r.status}"
    check("인터넷: GitHub(업데이트)", lambda: reach("https://codeload.github.com/kjg0898-dot/LongAutoUpload/zip/HEAD"))
    check("인터넷: YouTube API", lambda: reach("https://youtube.googleapis.com/$discovery/rest?version=v3"))
    import update
    check("최신 버전", lambda: (lambda n: (n == 0, "최신" if n == 0 else f"바뀐 파일 {n}개 — 실행.pyw를 켜면 자동 업데이트"))(
        update.apply(update.fetch(), dry=True)))
    import yt
    ok("열쇠 파일", yt.secret_file().exists(), yt.secret_file().name)


# ---------- 2. 로그인 ----------
def logins():
    log("\n[2] 채널 로그인 (할당량 채널당 약 1)")
    import core
    import yt
    cfg = core.load_config()
    if not cfg["channels"]:
        log("  설정에 채널이 아직 없습니다 (리허설은 테스트 로그인으로 진행).")
    for ch in cfg["channels"]:
        check(f"[{ch['name']}]", lambda ch=ch: (yt.client_for(ch) is not None, f"YouTube 채널 '{ch['channel_title']}'"))


# ---------- 3. 엑셀 열림 감지 ----------
def excel_lock():
    log("\n[3] 엑셀이 열려 있을 때 감지 (실제 엑셀로)")
    if not yes("시험용 엑셀 파일을 열어서 확인할까요?"):
        return log("  건너뜀")
    import core
    import steps
    SANDBOX.mkdir(exist_ok=True)
    core.EXCEL = SANDBOX / "잠금시험.xlsx"
    core.write_rows([dict.fromkeys(core.COLUMNS, "") | {"채널": "시험", "파일": "시험.mp4", "제목": "엑셀 잠금 시험"}])
    os.startfile(core.EXCEL)
    input("엑셀에서 '잠금시험' 파일이 열리면 엔터를 누르세요 (닫지 말고)… ")
    time.sleep(2)

    def opened_detected():
        try:
            open(core.EXCEL, "r+b").close()
            return False, "열려 있는데 감지 못함"
        except PermissionError:
            return True, "감지됨"
    check("열려 있음 감지(읽기 전)", opened_detected)

    def save_blocked():
        try:
            core.write_rows([dict.fromkeys(core.COLUMNS, "") | {"채널": "시험", "파일": "시험.mp4"}])
            return False, "열려 있는데 저장돼 버림"
        except core.ExcelLocked:
            # 엑셀이 스스로 만드는 잠금 파일(~$…)은 빼고, 프로그램의 임시 파일(…저장중.xlsx)만 본다
            return not any(SANDBOX.glob("*.저장중.xlsx")), "저장 막힘, 임시 파일 안 남음"
    check("열려 있을 때 저장 막힘", save_blocked)
    input("이제 엑셀을 '저장하지 않고' 닫은 뒤 엔터를 누르세요… ")
    time.sleep(2)
    check("닫은 뒤 저장", lambda: (core.write_rows([dict.fromkeys(core.COLUMNS, "") | {"채널": "시험", "파일": "시험.mp4"}])
                                  is None, "됨"))
    check("닫은 뒤 읽기", lambda: (bool(steps.core.read_rows()), "됨"))


# ---------- 4. 알림·열기 ----------
def opening():
    log("\n[4] 알림·폴더·브라우저")
    if not yes("알림을 띄우고 폴더·브라우저를 열어 볼까요?"):
        return log("  건너뜀")
    import gui
    import steps
    gui.toast("롱폼 자동 업로드", "PC 점검: 알림 시험입니다")
    time.sleep(3)
    ok("Windows 알림", yes("화면 오른쪽 아래에 '알림 시험' 알림이 떴나요?"))
    gui.open_path(HERE)
    ok("폴더 열기", yes("_프로그램 폴더가 열렸나요?"))
    gui.open_path(HERE / "사용법.md")
    ok("안내 열기(메모장)", yes("사용법이 메모장으로 열렸나요?"))
    steps.open_tabs(["https://studio.youtube.com"])
    ok("브라우저 탭", yes("브라우저에서 YouTube Studio가 열렸나요?"))
    ok("브라우저 계정", yes("열린 Studio가 숨겨진동양사(작업할 채널) 계정인가요? (고정할 때 필요)"))


# ---------- 5. 리허설 ----------
class ConsoleUI:
    def log(self, m):
        log(f"    {m}")

    def alert(self, m):
        log(f"    [알림] {m}")

    def notify(self, m):
        log(f"    [완료] {m}")

    def ask(self, q):
        log(f"    [질문 → 예] {q.splitlines()[0]}")
        return True


def rehearsal():
    log("\n[5] 리허설: 실제 프로그램 1→2→4단계를 테스트용 초안으로 (할당량 약 400)")
    if not yes("버려도 되는 테스트용 초안이 Studio에 있나요? 리허설을 할까요?"):
        return log("  건너뜀")
    import core
    import steps
    import yt
    real = core.load_config()
    if real["channels"] and yes(f"설정의 '{real['channels'][0]['name']}' 채널로 할까요? (n이면 테스트 로그인 사용)"):
        ch = dict(real["channels"][0])
    else:
        client = yt.build_client(yt.login("테스트"))
        cid, title = yt.whoami(client)
        ch = {**core.DEFAULT_CHANNEL, "key": "테스트", "channel_id": cid, "channel_title": title}
    ch |= {"name": "리허설", "folder": "리허설채널", "playlist": "", "slots": ch.get("slots") or "월 12:15"}
    client = yt.client_for(ch)
    vid = core.video_id(input("테스트용 초안의 주소: "))
    found = yt.get_videos(client, [vid])
    if not found:
        return ok("테스트 영상 찾기", False, "이 채널에서 못 찾음")
    video = found[0]
    if not yt.is_untouched(video) and not yes("이 영상은 이미 공개·예약된 영상입니다. 그래도 할까요?"):
        return log("  취소")
    name = yt.file_name(video)
    ok("테스트 영상", bool(name), f"{vid} | {video['status']['privacyStatus']} | 파일={name}")

    # 진짜 엑셀·영상 폴더·설정 대신 리허설 폴더를 쓰게 바꾼다
    shutil.rmtree(SANDBOX, ignore_errors=True)
    core.VIDEO_DIR, core.DONE_DIR = SANDBOX / "영상", SANDBOX / "완료"
    core.EXCEL, core.BACKUP_DIR = SANDBOX / "업로드목록.xlsx", SANDBOX / "백업"
    cfg = {**core.DEFAULT_CONFIG, "channels": [ch], "mode": "한 번에 전체"}
    core.load_config = lambda: cfg
    folder = core.new_batch(cfg) / ch["folder"]
    (folder / name).write_bytes(b"")  # 짝 맞추기는 파일 이름만 보므로 빈 파일로 충분
    stem = Path(name).stem
    (folder / "seo.txt").write_text(
        "=" * 60 + f"\n[{datetime.now():%Y-%m-%d %H:%M}] 행 2 | 순번 1 | 제목: {stem}\n" + "=" * 60 +
        "\n---\n제목 (3안)\n1. 리허설 제목 후보\n2. 리허설 둘째 후보\n추천 1순위: 1 — 시험\n---\n"
        "설명글 & 해시태그\n리허설 설명입니다.\n\n#리허설\n---\n고정 댓글\nLongAutoUpload 리허설 댓글입니다.\n", "utf-8")
    import 전체기능_테스트 as full
    full.test_png(folder / f"{stem}.png")
    ui = ConsoleUI()

    log("  ▶ 1. 엑셀 만들기")
    steps.build_excel(ui)
    rows = core.read_rows()
    ok("1단계: 엑셀 행", len(rows) == 1 and rows[0]["제목"] == "리허설 제목 후보" and rows[0]["썸네일"].endswith(".png"),
       f"제목={rows[0]['제목'] if rows else '-'} / 썸네일={rows[0]['썸네일'] if rows else '-'}")
    slots = core.parse_slots(ch["slots"], ch["name"])
    rows[0]["예약일시"] = core.fmt_dt(core.next_slots(datetime.now(core.KST) + timedelta(days=30), slots, 1)[0])
    core.write_rows(rows)  # 실수로 공개되지 않게 30일 뒤로

    log("  ▶ 2. 정보·댓글 넣기")
    steps.fill_all(ui)
    row = core.read_rows()[0]
    ok("2단계: 상태", row["상태"] == "댓글완료", row["상태"] + (f" / 오류: {row['오류']}" if row["오류"] else ""))
    for wait in (0, 5, 10, 20):
        time.sleep(wait)
        v = yt.get_videos(client, [vid])[0]
        if "리허설 제목 후보" in v["snippet"]["title"]:
            break
    ok("2단계: 제목", "리허설 제목 후보" in v["snippet"]["title"], v["snippet"]["title"])
    ok("2단계: 설명", v["snippet"].get("description", "").startswith("리허설 설명"),
       v["snippet"].get("description", "")[:60].replace("\n", " / "))
    ok("2단계: 일부공개", v["status"]["privacyStatus"] == "unlisted", v["status"]["privacyStatus"])
    ok("2단계: 원본 백업", (core.BACKUP_DIR / "영상" / f"{vid}.json").exists())
    mine = []
    for wait in (3, 5, 10, 15, 20):  # 댓글은 단 뒤 목록에 보이기까지 몇 초~수십 초 걸린다
        time.sleep(wait)
        threads = yt.execute(client.commentThreads().list(part="snippet", videoId=vid, maxResults=20))["items"]
        mine = [c for c in threads if "리허설 댓글" in c["snippet"]["topLevelComment"]["snippet"].get("textOriginal", "")]
        if mine:
            break
    ok("2단계: 댓글", bool(mine), "목록에서 확인" if mine else "아직 안 보임 (Studio 댓글에서 확인)")
    ok("3단계: 고정 탭 주소", bool(steps.pin_targets(core.read_rows(), cfg)), "고정은 직접 하는 단계라 주소만 확인")

    log("  ▶ 4. 예약 걸기")
    steps.schedule_all(ui)
    row = core.read_rows()[0]
    ok("4단계: 상태", row["상태"] == "예약완료", row["상태"] + (f" / 오류: {row['오류']}" if row["오류"] else ""))
    v = yt.get_videos(client, [vid])[0]
    ok("4단계: 예약", v["status"].get("privacyStatus") == "private" and bool(v["status"].get("publishAt")),
       f"{v['status'].get('privacyStatus')} / {v['status'].get('publishAt')} (엑셀: {row['예약일시']})")
    ok("4단계: 완료 폴더로 이동", not (core.VIDEO_DIR / row["파일"]).exists()
       and any(p.name == name for p in core.DONE_DIR.rglob("*")))

    if mine:
        try:
            yt.execute(client.comments().delete(id=mine[0]["snippet"]["topLevelComment"]["id"]))
        except Exception:
            pass
    if yes("리허설이 끝났습니다. 테스트 영상을 YouTube에서 지울까요?"):
        check("테스트 영상 지우기", lambda: (yt.execute(client.videos().delete(id=vid)) is None or True, "지움"))
    else:
        log("  ⚠ 테스트 영상이 30일 뒤로 예약돼 있습니다. 쓰지 않을 거면 Studio에서 지워주세요.")


def main():
    log(f"PC 점검 {datetime.now():%Y-%m-%d %H:%M}")
    for part in (env, logins, excel_lock, opening, rehearsal):
        try:
            part()
        except Exception:
            ok(f"{part.__name__} 진행", False, traceback.format_exc()[-800:])
    shutil.rmtree(SANDBOX, ignore_errors=True)
    log(f"\n결과: ✓ {score['ok']}개, ✗ {score['bad']}개")


if __name__ == "__main__":
    try:
        main()
    except Exception:
        log(traceback.format_exc())
    RESULT.write_text("\n".join(lines), encoding="utf-8")
    print(f"\n결과를 저장했습니다: {RESULT}\n이 파일 내용을 Claude에게 붙여넣어 주세요.")
    input("엔터를 누르면 닫힙니다.")
