"""버튼 하나 = 단계 하나. ui는 log(글), ask(질문)->bool, alert(알림)을 가진 객체."""
import webbrowser
from datetime import datetime, timedelta
from pathlib import Path

import core
import yt


class Stop(Exception):
    """연속 실패나 할당량 소진으로 단계를 멈춘다."""


def _now():
    return datetime.now(core.KST)


def _client(ui, ch):
    try:
        return yt.client_for(ch)
    except yt.NotLoggedIn:
        ui.log(f"[{ch['name']}] 로그인 전입니다. 설정에서 '로그인'을 눌러주세요.")
    except Exception as e:
        ui.log(f"[{ch['name']}] 연결 실패: {yt.error_text(e)}")
    return None


def _loop(ui, cfg, rows, todo, work):
    """todo 행마다 work(row)를 실행. 실패는 기록하고 넘어가되 연속 N번이면 멈춘다."""
    fails, failed = 0, []
    for r in todo:
        if cfg["mode"] == "영상마다 확인" and not ui.ask(f"진행할까요?\n\n[{r['채널']}] {r['파일']}\n{r['제목']}"):
            continue
        try:
            work(r)
            r["오류"], fails = "", 0
        except Exception as e:
            if yt.is_quota(e):
                core.write_rows(rows)
                raise Stop("오늘 YouTube API 할당량을 다 썼습니다. 한국 시간 오후 5시쯤 이후에 같은 버튼을 다시 누르면 이어서 합니다.")
            r["오류"] = yt.error_text(e)
            failed.append(r)
            fails += 1
            ui.log(f"  ✗ {r['파일']}: {r['오류']}")
            if fails >= cfg["max_fails"]:
                core.write_rows(rows)
                raise Stop(f"{fails}번 연속 실패해서 멈췄습니다. 마지막 오류: {r['오류']}")
        core.write_rows(rows)  # 영상 하나 끝날 때마다 저장 → 중간에 멈춰도 이어서 가능
    return failed


def _report(ui, title, failed):
    if failed:
        ui.alert(f"{title}: {len(failed)}개 실패\n" + "\n".join(f"- {r['파일']}: {r['오류']}" for r in failed))
    else:
        ui.log(f"{title}: 모두 끝났습니다.")


# ---------- 1. 엑셀 만들기 ----------
def build_excel(ui):
    cfg = core.load_config()
    if not cfg["channels"]:
        return ui.alert("설정에서 채널을 먼저 추가해주세요.")
    rows, problems = core.build_rows(cfg, core.read_rows())
    for ch in cfg["channels"]:
        if not any(r["채널"] == ch["name"] and not r["예약일시"] for r in rows):
            continue
        client = _client(ui, ch)
        if not client:
            ui.log(f"[{ch['name']}] 예약일시는 4단계에서 채웁니다.")
            continue
        try:
            core.fill_schedule(rows, ch, yt.last_scheduled(yt.recent_videos(client)), _now())
        except Exception as e:
            problems.append(f"[{ch['name']}] 예약일시 계산 실패: {yt.error_text(e)}")
    core.write_rows(rows)
    ui.log(f"엑셀을 만들었습니다: 영상 {len(rows)}개 → {core.EXCEL.name}")
    if problems:
        ui.alert("확인이 필요한 파일이 있습니다:\n" + "\n".join(problems))
    ui.log("노란 칸(제목·회원우선·예약일시)만 고치고 저장한 뒤, 엑셀을 닫고 2단계를 누르세요.")


# ---------- 2. 정보·댓글 넣기 ----------
def _fill_info(client, ch, r, video):
    s = video["snippet"]
    title = core.merge(s["title"], r["제목"], ch["title_mode"], ch["title_marker"], " ")
    desc = core.merge(s.get("description", ""), r["설명"], ch["desc_mode"], ch["desc_marker"], "\n\n")
    tags = core.merge_tags(s.get("tags", []), core.parse_tags(r["태그"]), ch["tags_mode"])
    core.check_text(title, desc)
    snippet_over, status_over = core.overrides(ch)
    # 댓글을 달려면 일부공개여야 한다
    yt.update_video(client, video, {"title": title, "description": desc, "tags": tags} | snippet_over,
                    status_over | {"privacyStatus": "unlisted"})
    if r["썸네일"]:
        yt.set_thumbnail(client, video["id"], core.VIDEO_DIR / r["썸네일"])
    if ch["playlist"].strip():
        yt.add_to_playlist(client, ch["playlist"].strip(), video["id"])


def fill_all(ui):
    cfg, rows = core.load_config(), core.read_rows()
    core.write_rows(rows)  # 엑셀이 열려 있으면 여기서 먼저 알려준다
    failed = []
    for ch in cfg["channels"]:
        todo = [r for r in rows if r["채널"] == ch["name"] and r["상태"] in ("", "정보완료")]
        if not todo or not (client := _client(ui, ch)):
            continue
        ui.log(f"[{ch['name']}] {len(todo)}개 처리 시작")
        recent = yt.recent_videos(client)
        by_id, by_name = {v["id"]: v for v in recent}, yt.by_filename(recent)

        def work(r):
            video = by_id.get(r["영상ID"]) if r["영상ID"] else by_name.get(Path(r["파일"]).name.lower())
            if not video:
                raise LookupError("Studio에서 이 파일의 초안을 못 찾았습니다 (업로드가 끝났는지 확인)")
            r["영상ID"] = video["id"]
            if r["상태"] == "":
                _fill_info(client, ch, r, video)
                r["상태"] = "정보완료"
            if r["고정댓글"].strip():
                yt.post_comment(client, video["id"], r["고정댓글"].strip())
            r["상태"] = "댓글완료"
            ui.log(f"  ✓ {r['파일']}")

        failed += _loop(ui, cfg, rows, todo, work)
    _report(ui, "정보·댓글 넣기", failed)


# ---------- 3. 고정 탭 열기 ----------
def pin_targets(rows, cfg):
    """{채널 이름: [열 주소]} — 수창 채널은 광고 설문 화면도 같이."""
    out = {}
    for ch in cfg["channels"]:
        urls = []
        for r in rows:
            if r["채널"] == ch["name"] and r["상태"] == "댓글완료" and r["영상ID"]:
                if r["고정댓글"].strip():
                    urls.append(f"https://www.youtube.com/watch?v={r['영상ID']}")
                if ch["ypp"]:
                    urls.append(f"https://studio.youtube.com/video/{r['영상ID']}/monetization")
        if urls:
            out[ch["name"]] = urls
    return out


def open_tabs(urls):
    for u in urls:
        webbrowser.open_new_tab(u)


# ---------- 4. 예약 걸기 ----------
def schedule_all(ui):
    if not ui.ask("고정(수창 채널은 광고 설문까지)을 모두 마치셨나요?\n예를 누르면 예약을 겁니다."):
        return
    cfg, rows = core.load_config(), core.read_rows()
    core.write_rows(rows)
    failed, manual = [], []
    for ch in cfg["channels"]:
        todo = [r for r in rows if r["채널"] == ch["name"] and r["상태"] == "댓글완료"]
        if not todo or not (client := _client(ui, ch)):
            continue
        if any(not r["예약일시"] for r in todo):
            core.fill_schedule(rows, ch, yt.last_scheduled(yt.recent_videos(client)), _now())
        videos = {v["id"]: v for v in yt.get_videos(client, [r["영상ID"] for r in todo])}

        def work(r):
            when = core.parse_dt(r["예약일시"])
            if when <= _now() + timedelta(minutes=15):
                raise ValueError(f"예약일시({r['예약일시']})가 지났거나 너무 가깝습니다. 엑셀에서 고쳐주세요.")
            if r["회원우선"].strip():
                # API에는 회원 전용 공개가 없어서 사용자가 직접 설정한다
                manual.append(r)
                r["상태"] = "완료(회원 직접설정)"
            else:
                if r["영상ID"] not in videos:
                    raise LookupError("YouTube에서 이 영상을 못 찾았습니다 (삭제됐는지 확인)")
                yt.schedule(client, videos[r["영상ID"]], r["예약일시"])
                r["상태"] = "예약완료"
            core.move_done(r)
            ui.log(f"  ✓ {r['파일']} → {r['예약일시']}")

        failed += _loop(ui, cfg, rows, todo, work)
    if manual:
        ui.alert("회원 전용으로 직접 설정할 영상:\n" + "\n".join(
            f"- [{r['채널']}] {r['제목']} → 회원 전용, {r['예약일시']} 전체 공개" for r in manual))
        if ui.ask("이 영상들의 Studio 화면을 열까요?"):
            open_tabs([f"https://studio.youtube.com/video/{r['영상ID']}/edit" for r in manual])
    _report(ui, "예약 걸기", failed)
