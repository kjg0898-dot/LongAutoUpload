"""가짜 YouTube로 1→2→3→4단계 전체 흐름 검사. 실행: python test_steps.py"""
import tempfile
from datetime import datetime, timedelta
from pathlib import Path

import core
import steps
import yt


class Call:
    def __init__(self, fn):
        self.fn = fn

    def execute(self):
        return self.fn()


class FakeYouTube:
    """필요한 API만 흉내 낸다. 초안 = fileDetails.fileName이 있는 비공개 영상."""

    def __init__(self, files):
        self.videos_db = {f"vid{i}": {
            "id": f"vid{i}", "fileDetails": {"fileName": f},
            "snippet": {"title": "[숨겨진 동양사]   l 역사 인물 l 고전 | 수면오디오북", "description": "구독 부탁", "tags": ["기본"],
                        "categoryId": "22", "channelId": "UC1", "thumbnails": {}},
            "status": {"privacyStatus": "private", "uploadStatus": "processed", "madeForKids": False,
                       "selfDeclaredMadeForKids": False}} for i, f in enumerate(files)}
        self.comments, self.thumbs, self.playlist, self.updates = [], [], [], []
        self.fail_comment_for = set()

    def channels(self):
        return self

    def playlistItems(self):
        return self

    def thumbnails(self):
        return self

    def commentThreads(self):
        return self

    def videos(self):
        return self

    def list(self, part, mine=None, id=None, playlistId=None, maxResults=None, pageToken=None, videoId=None):
        if mine:
            return Call(lambda: {"items": [{"id": "UC1", "snippet": {"title": "내채널"},
                                            "contentDetails": {"relatedPlaylists": {"uploads": "UU1"}}}]})
        if playlistId == "UU1":
            return Call(lambda: {"items": [{"contentDetails": {"videoId": v}} for v in self.videos_db]})
        if playlistId:
            return Call(lambda: {"items": [p for p in self.playlist if p == (playlistId, videoId)]})
        return Call(lambda: {"items": [self.videos_db[v] for v in id.split(",") if v in self.videos_db]})

    def update(self, part, body):
        assert "thumbnails" not in body.get("snippet", {}) and "madeForKids" not in body.get("status", {})

        def do():
            v = self.videos_db[body["id"]]
            for p in part.split(","):
                v[p] = {**v[p], **body[p]}
            self.updates.append(body)
            return v
        return Call(do)

    def set(self, videoId, media_body):
        return Call(lambda: self.thumbs.append(videoId))

    def insert(self, part, body):
        s = body["snippet"]
        if "playlistId" in s:
            return Call(lambda: self.playlist.append((s["playlistId"], s["resourceId"]["videoId"])))

        def comment():
            if s["videoId"] in self.fail_comment_for:
                raise RuntimeError("commentsDisabled")
            self.comments.append((s["videoId"], s["topLevelComment"]["snippet"]["textOriginal"]))
        return Call(comment)


class UI:
    def __init__(self):
        self.logs, self.alerts, self.asked, self.answer = [], [], [], True

    def log(self, m):
        self.logs.append(m)

    def alert(self, m):
        self.alerts.append(m)

    def notify(self, m):
        self.logs.append(m)

    def ask(self, q):
        self.asked.append(q)
        return self.answer(q) if callable(self.answer) else self.answer


def setup(d):
    core.VIDEO_DIR, core.DONE_DIR = Path(d) / "영상", Path(d) / "완료"
    core.EXCEL, core.CONFIG, core.BACKUP_DIR = Path(d) / "목록.xlsx", Path(d) / "설정.json", Path(d) / "백업"
    ch = {**core.new_channel("명언"), "folder": "명언", "members": True, "channel_id": "UC1",
          "slots": "월 18:00, 수 18:00, 금 18:00", "playlist": "PL1"}
    core.save_config({**core.DEFAULT_CONFIG, "channels": [ch]})
    (core.VIDEO_DIR / "b" / "명언").mkdir(parents=True)
    for n in (1, 2, 3):
        (core.VIDEO_DIR / "b" / "명언" / f"{n}.mp4").write_bytes(b"v")
        (core.VIDEO_DIR / "b" / "명언" / f"{n}.jpg").write_bytes(b"i")
        (core.VIDEO_DIR / "b" / "명언" / f"{n}.txt").write_text(
            f"제목: 명언 {n}\n설명: 본문 {n}\n태그: #명언, 새태그\n고정댓글: 댓글 {n}", "utf-8")
    fake = FakeYouTube(["1.mp4", "2.mp4", "3.mp4"])
    yt.client_for = lambda ch, interactive=False: fake
    return fake


def test_full_flow():
    with tempfile.TemporaryDirectory() as d:
        fake, ui = setup(d), UI()
        steps.build_excel(ui)
        rows = core.read_rows()
        assert [r["회원우선"] for r in rows] == ["", "", "O"] and all(r["예약일시"] for r in rows), rows
        rows[0]["제목"] = "고친 명언 1"
        core.write_rows(rows)

        fake.fail_comment_for = {"vid1"}
        steps.fill_all(ui)
        rows = core.read_rows()
        assert [r["상태"] for r in rows] == ["댓글완료", "정보완료", "댓글완료"], rows
        assert "commentsDisabled" in rows[1]["오류"]
        v0 = fake.videos_db["vid0"]
        assert v0["snippet"]["title"] == "[숨겨진 동양사] 고친 명언 1 l 역사 인물 l 고전 | 수면오디오북"
        assert v0["snippet"]["description"] == "본문 1\n\n구독 부탁"
        assert v0["snippet"]["tags"] == ["기본", "명언", "새태그"]
        assert v0["status"]["privacyStatus"] == "unlisted"
        assert fake.thumbs == ["vid0", "vid1", "vid2"] and len(fake.playlist) == 3

        fake.fail_comment_for = set()
        steps.fill_all(ui)  # 실패한 것만 이어서: 제목이 두 번 들어가지 않아야 한다
        assert fake.videos_db["vid1"]["snippet"]["title"] == "[숨겨진 동양사] 명언 2 l 역사 인물 l 고전 | 수면오디오북"
        assert sorted(c[0] for c in fake.comments) == ["vid0", "vid1", "vid2"]

        targets = steps.pin_targets(core.read_rows(), core.load_config())
        assert len(targets["명언"]) == 3

        steps.schedule_all(ui)
        rows = core.read_rows()
        assert [r["상태"] for r in rows] == ["예약완료", "예약완료", "회원 설정 필요"], rows
        assert fake.videos_db["vid0"]["status"]["privacyStatus"] == "private"
        assert fake.videos_db["vid0"]["status"]["publishAt"].endswith("Z")
        assert fake.videos_db["vid2"]["status"]["privacyStatus"] == "unlisted"  # 회원 영상은 사용자가 직접
        assert any("회원 전용" in a for a in ui.alerts)
        assert not list(core.VIDEO_DIR.iterdir())  # 끝난 묶음 폴더는 비워지고 지워진다
        assert len(list((core.DONE_DIR / "b" / "명언").iterdir())) == 9


def test_draft_by_pasted_id():
    """초안이 업로드 목록에 안 보여도 엑셀 영상ID 칸에 넣으면 찾는다."""
    with tempfile.TemporaryDirectory() as d:
        fake, ui = setup(d), UI()
        steps.build_excel(ui)
        hidden = fake.list
        fake.list = lambda part, playlistId=None, **k: Call(lambda: {"items": []}) if playlistId == "UU1" \
            else hidden(part, playlistId=playlistId, **k)
        try:
            steps.fill_all(ui)  # 3개 모두 못 찾음 → 연속 실패로 멈춤
        except steps.Stop:
            pass
        assert all("영상ID" in r["오류"] for r in core.read_rows())
        rows = core.read_rows()
        rows[0]["영상ID"] = "vid0"
        core.write_rows(rows)
        steps.fill_all(ui)
        assert core.read_rows()[0]["상태"] == "댓글완료"


def test_stops_after_consecutive_fails():
    with tempfile.TemporaryDirectory() as d:
        fake, ui = setup(d), UI()
        steps.build_excel(ui)
        cfg = core.load_config()
        cfg["max_fails"] = 2
        core.save_config(cfg)
        fake.fail_comment_for = {"vid0", "vid1", "vid2"}
        try:
            steps.fill_all(ui)
            raise AssertionError("멈춰야 함")
        except steps.Stop as e:
            assert "2번 연속" in str(e)
        assert [r["상태"] for r in core.read_rows()] == ["정보완료", "정보완료", ""]


def test_past_schedule_is_rejected():
    with tempfile.TemporaryDirectory() as d:
        ui = UI()
        setup(d)
        steps.build_excel(ui)
        steps.fill_all(ui)
        rows = core.read_rows()
        rows[0]["예약일시"] = core.fmt_dt(datetime.now(core.KST) - timedelta(days=1))
        core.write_rows(rows)
        steps.schedule_all(ui)
        rows = core.read_rows()
        assert rows[0]["상태"] == "댓글완료" and "지났거나" in rows[0]["오류"]
        assert rows[1]["상태"] == "예약완료"


def test_safety_guards():
    """지난 묶음의 같은 이름 영상, 공개 영상ID, 같은 이름 초안 2개 → 고치지 않고 오류."""
    with tempfile.TemporaryDirectory() as d:
        fake, ui = setup(d), UI()
        old = {**fake.videos_db["vid0"], "id": "old0", "snippet": {**fake.videos_db["vid0"]["snippet"],
               "publishedAt": "2020-01-01T00:00:00Z"}, "status": {**fake.videos_db["vid0"]["status"], "privacyStatus": "unlisted"}}
        fake.videos_db = {"old0": old, **fake.videos_db}  # 지난 묶음의 회원 영상 (같은 파일 이름 1.mp4, 일부공개)
        fake.videos_db["vid1"]["fileDetails"] = {"fileName": "3.mp4"}  # 3.mp4 초안이 2개
        steps.build_excel(ui)
        steps.fill_all(ui)
        rows = core.read_rows()
        assert rows[0]["영상ID"] == "vid0" and fake.videos_db["old0"]["snippet"]["title"].startswith("[숨겨진")
        assert "2개" in rows[2]["오류"] and rows[2]["상태"] == "", rows[2]
        assert "못 찾았" in rows[1]["오류"]
        rows[1]["영상ID"] = "pub"
        fake.videos_db["pub"] = {**fake.videos_db["vid2"], "id": "pub", "fileDetails": {"fileName": "2.mp4"},
                                 "status": {"privacyStatus": "public"}}
        core.write_rows(rows)
        steps.fill_all(ui)
        assert "이미 공개" in core.read_rows()[1]["오류"] and fake.videos_db["pub"]["status"] == {"privacyStatus": "public"}


def test_rerun_after_title_edit_and_excel_lock():
    """썸네일 실패 뒤 제목을 고쳐 다시 실행해도 제목이 한 번만, 엑셀이 잠기면 물어보고 다시 저장."""
    with tempfile.TemporaryDirectory() as d:
        fake, ui = setup(d), UI()
        steps.build_excel(ui)
        real_set = fake.set
        fake.set = lambda videoId, media_body: Call(lambda: (_ for _ in ()).throw(RuntimeError("썸네일 실패")))
        try:
            steps.fill_all(ui)
        except steps.Stop:  # 3번 연속 실패
            pass
        rows = core.read_rows()
        assert [r["상태"] for r in rows] == ["", "", ""] and fake.videos_db["vid0"]["snippet"]["title"].count("명언 1") == 1
        rows[0]["제목"] = "다시 고친 제목"
        core.write_rows(rows)
        fake.set = real_set
        writes, real_write = [], core.write_rows

        def flaky(rows_, *a):
            writes.append(1)
            if len(writes) == 3:  # 진행 중 한 번 엑셀이 열려 있었다고 치기
                raise core.ExcelLocked("열려 있음")
            return real_write(rows_, *a)
        core.write_rows = flaky
        try:
            steps.fill_all(ui)
        finally:
            core.write_rows = real_write
        assert fake.videos_db["vid0"]["snippet"]["title"] == "[숨겨진 동양사] 다시 고친 제목 l 역사 인물 l 고전 | 수면오디오북"
        assert [r["상태"] for r in core.read_rows()] == ["댓글완료"] * 3
        assert sorted(c[0] for c in fake.comments) == ["vid0", "vid1", "vid2"]  # 댓글 중복 없음
        assert (core.BACKUP_DIR / "영상" / "vid0.json").exists()


def test_schedule_checks():
    """예약 시간 겹침, 이미 예약된 시간, 예약 확인 실패, 파일 옮기기 실패는 각각 알리고 계속."""
    with tempfile.TemporaryDirectory() as d:
        ui = UI()
        setup(d)
        steps.build_excel(ui)
        steps.fill_all(ui)
        rows = core.read_rows()
        rows[1]["예약일시"] = rows[0]["예약일시"]
        core.write_rows(rows)
        real_move = core.move_done
        core.move_done = lambda r: (_ for _ in ()).throw(PermissionError("파일 사용 중"))
        try:
            steps.schedule_all(ui)
        finally:
            core.move_done = real_move
        rows = core.read_rows()
        assert "같습니다" in rows[0]["오류"] and "같습니다" in rows[1]["오류"]
        assert rows[2]["상태"] == "회원 설정 필요" and (core.VIDEO_DIR / rows[2]["파일"]).exists()
        steps.build_excel(ui)  # 다음에 누를 때 못 옮긴 파일을 다시 옮긴다
        assert not (core.VIDEO_DIR / rows[2]["파일"]).exists()


def test_status_summary_and_kids():
    with tempfile.TemporaryDirectory() as d:
        fake, ui = setup(d), UI()
        cfg = core.load_config()
        cfg["channels"][0]["kids"] = "예"
        core.save_config(cfg)
        steps.build_excel(ui)
        assert "대기 3" in steps.status_summary(core.read_rows(), cfg) and "2. 정보" in steps.status_summary(core.read_rows(), cfg)
        steps.fill_all(ui)
        assert fake.comments == [] and any("아동용" in m for m in ui.logs)
        assert "3. 고정" in steps.status_summary(core.read_rows(), cfg)


def test_excel_open_and_off_slot_time():
    """엑셀이 열려 있으면 닫을 때까지 기다렸다가 (닫으며 저장한 수정을) 읽고, 공개 시간과 다른 예약일시는 물어본다."""
    with tempfile.TemporaryDirectory() as d:
        fake, ui = setup(d), UI()
        steps.build_excel(ui)
        steps.fill_all(ui)
        rows = core.read_rows()
        rows[0]["예약일시"] = "2030-01-02 00:00"  # 날짜만 적어 자정이 된 경우 (수요일 00:00)
        core.write_rows(rows)
        locked = [True]

        def fake_open(path, mode="r", *a, **k):
            if locked and mode == "r+b":
                locked.pop()
                rows[1]["예약일시"] = "2030-01-04 18:00"  # 사용자가 엑셀을 닫으며 저장한 수정
                core.write_rows(rows)
                raise PermissionError
            return open(path, mode, *a, **k)
        steps.open = fake_open
        ui.answer = lambda q: "다릅니다" not in q  # 공개 시간과 다른 것은 '아니요'
        try:
            steps.schedule_all(ui)
        finally:
            del steps.open
        rows = core.read_rows()
        assert any("엑셀이 열려" in q for q in ui.asked)
        assert rows[0]["상태"] == "댓글완료" and "공개 요일" in rows[0]["오류"]
        assert rows[1]["상태"] == "예약완료" and rows[1]["예약일시"] == "2030-01-04 18:00"  # 닫으며 저장한 수정이 반영됨
        assert fake.videos_db["vid1"]["status"]["publishAt"] == "2030-01-04T09:00:00Z"


def test_persona_fixes():
    """썸네일 없으면 2단계 점검에서 멈춤('없음'이면 통과), 할 일 없을 때 안내, 날짜만 적은 예약일시는 공개 시간으로."""
    with tempfile.TemporaryDirectory() as d:
        fake, ui = setup(d), UI()
        (core.VIDEO_DIR / "b" / "명언" / "2.jpg").unlink()
        ui.answer = True
        steps.schedule_all(ui)
        assert any("예약할 영상이 없습니다" in m for m in ui.logs) and not ui.asked
        steps.build_excel(ui)
        assert any("2.mp4: 썸네일을 못 찾음" in a for a in ui.alerts)
        steps.fill_all(ui)
        rows = core.read_rows()
        assert rows[1]["상태"] == "" and "썸네일" in rows[1]["오류"] and "vid1" not in fake.thumbs
        rows[1]["썸네일"] = "없음"
        rows[0]["예약일시"] = "2030-01-02"  # 수요일, 날짜만
        core.write_rows(rows)
        steps.build_excel(ui)  # 다시 눌러도 '없음'은 유지
        assert core.read_rows()[1]["썸네일"] == "없음"
        steps.fill_all(ui)
        assert [r["상태"] for r in core.read_rows()] == ["댓글완료"] * 3 and "vid1" not in fake.thumbs
        steps.fill_all(ui)
        assert any("모두 넣었습니다" in m for m in ui.logs)
        steps.schedule_all(ui)
        rows = core.read_rows()
        assert rows[0]["예약일시"] == "2030-01-02 18:00" and rows[0]["상태"] == "예약완료"


def test_schedule_verify_waits_for_delayed_read():
    """YouTube가 예약을 늦게 보여줘도 실패로 치지 않고, 끝까지 다르면 실패로 알린다."""
    import copy
    with tempfile.TemporaryDirectory() as d:
        fake, ui = setup(d), UI()
        steps.build_excel(ui)
        steps.fill_all(ui)
        stale = {k: copy.deepcopy(v) for k, v in fake.videos_db.items()}  # 예약 전 모습
        reads, real_list = [0], fake.list

        def delayed(part, id=None, **k):
            if id and fake.updates and fake.updates[-1].get("status", {}).get("publishAt"):
                reads[0] += 1
                if reads[0] <= 2:  # 예약 직후 두 번은 옛 모습
                    return Call(lambda: {"items": [stale[v] for v in id.split(",")]})
            return real_list(part, id=id, **k)
        fake.list = delayed
        real_sleep, steps.time.sleep = steps.time.sleep, lambda s: None
        try:
            steps.schedule_all(ui)
        finally:
            steps.time.sleep = real_sleep
        rows = core.read_rows()
        assert [r["상태"] for r in rows[:2]] == ["예약완료", "예약완료"] and not rows[0]["오류"], rows


def test_card_status():
    row = lambda state, err="": dict.fromkeys(core.COLUMNS, "") | {"상태": state, "오류": err}  # noqa: E731
    assert steps.card_status([])[1] == "0" and steps.card_status([], has_videos=True)[1] == "1"
    text, nxt = steps.card_status([row(""), row("", "썸네일 없음"), row("정보완료")])
    assert nxt == "제목" and text["1"] == "영상 3개 · 오류 1" and text["2"] == "남은 3개"
    assert steps.card_status([row("정보완료")])[1] == "2"
    assert steps.card_status([row("댓글완료"), row("예약완료")])[1] == "3"
    text, nxt = steps.card_status([row("예약완료"), row(core.MEMBER_TODO)])
    assert nxt == "4" and "회원 직접설정 1" in text["4"]
    assert steps.card_status([row("예약완료")])[1] == "0"


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print("ok", name)
