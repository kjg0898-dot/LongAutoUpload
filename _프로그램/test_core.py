"""YouTube 연결 없이 확인할 수 있는 부분 검사. 실행: python test_core.py"""
import tempfile
from datetime import datetime
from pathlib import Path

import core
from core import KST


def test_merge():
    m = core.merge
    assert m("【명언】 | 채널", "인생 명언", "기준 글자 앞", "|") == "【명언】 인생 명언 | 채널"
    assert m("【명언】 | 채널", "인생 명언", "기준 글자 뒤", "【명언】") == "【명언】 인생 명언 | 채널"
    assert m("A {제목} B", "X", "표시 문자 바꾸기", "{제목}") == "A X B"
    assert m("구독 부탁", "본문", "맨 앞", sep="\n\n") == "본문\n\n구독 부탁"
    assert m("구독 부탁", "본문", "맨 뒤", sep="\n\n") == "구독 부탁\n\n본문"
    once = m("【명언】 | 채널", "인생", "기준 글자 앞", "|")
    assert m(once, "인생", "기준 글자 앞", "|") == once  # 두 번 실행해도 한 번만
    assert m("", "제목만", "기준 글자 앞", "|") == "제목만"
    assert m("틀", "", "맨 앞") == "틀"
    try:
        m("틀에 기준 없음", "X", "기준 글자 앞", "|")
        raise AssertionError
    except ValueError:
        pass


def test_tags():
    assert core.parse_tags("#명언, 좋은글\n#인생 #명언 모음") == ["명언", "좋은글", "인생", "명언 모음"]
    assert core.merge_tags(["명언", "기본"], ["명언", "새것"], "뒤에 추가") == ["명언", "기본", "새것"]
    assert core.merge_tags(["기본"], ["새것"], "앞에 추가") == ["새것", "기본"]
    assert core.tags_len(["a b", "c"]) == 3 + 2 + 1 + 1
    many = core.merge_tags([], [f"태그{i:03}" for i in range(200)], "뒤에 추가")
    assert core.tags_len(many) <= 500 < core.tags_len(many + ["x" * 6])


def test_memo():
    text = "제목: 인생 명언 10가지\n\n[설명]\n첫 줄\n둘째 줄\n\n■ 태그 : #명언, #인생\n고정 댓글:\n여러분의 명언은?\n"
    assert core.parse_memo(text) == {"title": "인생 명언 10가지", "desc": "첫 줄\n둘째 줄",
                                     "tags": "#명언, #인생", "comment": "여러분의 명언은?"}
    assert core.parse_memo("머리말 없는 글") == {}
    assert core.parse_memo("설명하자면 이렇다") == {}  # 낱말이 머리말로 오인되지 않게


def test_slots():
    slots = core.parse_slots("월 18:00, 수요일 18:00, 금 20:30")
    assert slots == [(0, 18, 0), (2, 18, 0), (4, 20, 30)]
    after = datetime(2026, 10, 19, 18, 0, tzinfo=KST)  # 월요일 18:00 정각 (이 자리는 제외)
    got = [core.fmt_dt(t) for t in core.next_slots(after, slots, 4)]
    assert got == ["2026-10-21 18:00", "2026-10-23 20:30", "2026-10-26 18:00", "2026-10-28 18:00"], got
    for bad in ("", "월18:00", "월 25:00", "x 18:00"):
        try:
            core.parse_slots(bad)
            raise AssertionError(bad)
        except ValueError:
            pass


def test_fill_schedule():
    ch = {"name": "A", "slots": "화 09:00"}
    rows = [{"채널": "A", "예약일시": "2026-11-03 09:00"}, {"채널": "A", "예약일시": ""},
            {"채널": "B", "예약일시": ""}]
    now = datetime(2026, 10, 16, 12, 0, tzinfo=KST)
    core.fill_schedule(rows, ch, datetime(2026, 10, 27, 9, 0, tzinfo=KST), now)
    assert rows[1]["예약일시"] == "2026-11-10 09:00"  # 엑셀에 이미 잡힌 마지막 자리 다음
    assert rows[2]["예약일시"] == ""


def test_scan_and_build():
    with tempfile.TemporaryDirectory() as d:
        core.VIDEO_DIR = Path(d) / "영상"
        a = core.VIDEO_DIR / "명언채널"
        (a / "02_사랑").mkdir(parents=True)
        for name in ("10_끝.mp4", "10_끝.txt", "10_끝.jpg", "1_시작.mp4", "1_시작 메모.txt", "1_시작_썸네일.png"):
            (a / name).write_bytes(b"x")
        (a / "1_시작 메모.txt").write_text("제목: 시작\n설명: 첫 영상\n태그: 명언", "utf-8")
        (a / "10_끝.txt").write_bytes("제목: 끝".encode("cp949"))
        for name in ("영상.mp4", "info.txt", "cover.jpg"):  # 영상마다 폴더가 따로 있는 경우
            (a / "02_사랑" / name).write_bytes(b"x")
        pairs = core.scan_channel(a)
        assert [(v.name, m and m.name, t and t.name) for v, m, t in pairs] == [
            ("1_시작.mp4", "1_시작 메모.txt", "1_시작_썸네일.png"),
            ("영상.mp4", "info.txt", "cover.jpg"),
            ("10_끝.mp4", "10_끝.txt", "10_끝.jpg")], pairs

        cfg = {"channels": [{**core.DEFAULT_CHANNEL, "name": "명언", "folder": "명언채널", "members": True}]}
        rows, problems = core.build_rows(cfg, [])
        assert [r["회원우선"] for r in rows] == ["", "", "O"]
        assert rows[0]["제목"] == "시작" and rows[0]["태그"] == "명언" and rows[2]["제목"] == "끝"
        assert any("info.txt" in p for p in problems)  # 빈 메모장은 확인 필요로 알린다
        rows[0]["제목"] = "고친 제목"
        again, _ = core.build_rows(cfg, rows)
        assert again[0]["제목"] == "고친 제목"  # 다시 만들어도 사용자가 고친 칸은 그대로

        core.EXCEL = Path(d) / "목록.xlsx"
        core.write_rows(rows)
        assert core.read_rows() == rows

        core.DONE_DIR = Path(d) / "완료"
        core.move_done(rows[2])
        assert (core.DONE_DIR / rows[2]["파일"]).exists() and not (core.VIDEO_DIR / rows[2]["파일"]).exists()


def test_overrides():
    ch = {**core.DEFAULT_CHANNEL}
    assert core.overrides(ch) == ({}, {})  # 기본값은 아무것도 건드리지 않음
    ch.update(category="교육", synthetic="아니요", license="크리에이티브 커먼즈", language="한국어")
    assert core.overrides(ch) == ({"categoryId": "27", "defaultLanguage": "ko", "defaultAudioLanguage": "ko"},
                                  {"containsSyntheticMedia": False, "license": "creativeCommon"})


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print("ok", name)
