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
        self.logs, self.alerts = [], []

    def log(self, m):
        self.logs.append(m)

    def alert(self, m):
        self.alerts.append(m)

    def ask(self, q):
        return True


def setup(d):
    core.VIDEO_DIR, core.DONE_DIR = Path(d) / "영상", Path(d) / "완료"
    core.EXCEL, core.CONFIG = Path(d) / "목록.xlsx", Path(d) / "설정.json"
    ch = {**core.new_channel("명언"), "folder": "명언", "members": True, "channel_id": "UC1",
          "slots": "월 18:00, 수 18:00, 금 18:00", "playlist": "PL1"}
    core.save_config({**core.DEFAULT_CONFIG, "channels": [ch]})
    for n in (1, 2, 3):
        (core.VIDEO_DIR / "명언" / f"{n}.mp4").write_bytes(b"v")
        (core.VIDEO_DIR / "명언" / f"{n}.jpg").write_bytes(b"i")
        (core.VIDEO_DIR / "명언" / f"{n}.txt").write_text(
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
        assert [r["상태"] for r in rows] == ["예약완료", "예약완료", "완료(회원 직접설정)"], rows
        assert fake.videos_db["vid0"]["status"]["privacyStatus"] == "private"
        assert fake.videos_db["vid0"]["status"]["publishAt"].endswith("Z")
        assert fake.videos_db["vid2"]["status"]["privacyStatus"] == "unlisted"  # 회원 영상은 사용자가 직접
        assert any("회원 전용" in a for a in ui.alerts)
        assert not list((core.VIDEO_DIR / "명언").iterdir())
        assert len(list((core.DONE_DIR / "명언").iterdir())) == 9


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


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print("ok", name)
