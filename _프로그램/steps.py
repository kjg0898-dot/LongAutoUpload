"""버튼 하나 = 단계 하나. ui는 log(글), ask(질문)->bool, alert(경고), notify(끝났을 때 알림)을 가진 객체."""
import math
import time
import webbrowser
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path

import core
import yt

QUOTA_MSG = "오늘 YouTube API 할당량을 다 썼습니다. 한국 시간 오후 5시 이후에 같은 버튼을 다시 누르면 이어서 합니다."


class Stop(Exception):
    """연속 실패, 할당량 소진, 엑셀 저장 불가로 단계를 멈춘다."""


def _now():
    return datetime.now(core.KST)


def _client(ui, ch, skipped):
    try:
        return yt.client_for(ch)
    except yt.NotLoggedIn:
        why = "로그인이 만료됐습니다" if ch["channel_id"] else "로그인 전입니다"
        msg = f"[{ch['name']}] {why} → 설정 → 이 채널 선택 → 로그인"
    except Exception as e:
        if yt.is_quota(e):
            raise Stop(QUOTA_MSG) from None
        msg = f"[{ch['name']}] 연결 실패: {yt.error_text(e)}"
    ui.log(msg)
    skipped.append(msg)
    return None


def _load(ui):
    """엑셀이 닫힐 때까지 기다린 뒤 읽는다. 열린 채로 읽으면 사용자가 닫으면서 저장한 수정을 놓친다."""
    while core.EXCEL.exists():
        try:
            open(core.EXCEL, "r+b").close()
            break
        except PermissionError:
            if not ui.ask("엑셀이 열려 있습니다. 고친 내용을 저장하고 엑셀을 닫은 뒤 '예'를 눌러주세요."):
                raise Stop("엑셀이 열려 있어서 시작하지 않았습니다.") from None
    core.backup_excel()
    return core.read_rows()


def _save(ui, rows):
    """엑셀 저장. 열려 있으면 닫을 때까지 묻는다 — 진행 상황을 잃으면 댓글이 두 번 달릴 수 있어서."""
    while True:
        try:
            return core.write_rows(rows)
        except core.ExcelLocked:
            if ui.ask("작업 중에 엑셀이 열려 있어서 진행 상황을 저장하지 못했습니다.\n엑셀을 (저장하지 말고) 닫고 '예'를 눌러주세요.\n"
                      "※ 작업 중에 엑셀에서 고친 내용은 덮어써집니다.\n\n"
                      "('아니요'를 누르면 여기서 멈추고, 진행 상황은 백업 폴더에 따로 저장합니다)"):
                continue
            path = core.BACKUP_DIR / "엑셀" / f"저장못함_{datetime.now(core.KST):%Y%m%d_%H%M%S}.xlsx"
            path.parent.mkdir(parents=True, exist_ok=True)
            excel, core.EXCEL = core.EXCEL, path
            try:
                core.write_rows(rows)
            finally:
                core.EXCEL = excel
            raise Stop(f"엑셀에 저장하지 못해 멈췄습니다. 진행 상황은 {path} 에 있습니다. "
                       f"엑셀을 닫고 이 파일을 '{excel.name}'로 바꿔 넣은 뒤 다시 눌러주세요.") from None


def _loop(ui, cfg, rows, todo, work):
    """todo 행마다 work(row)를 실행. 실패는 기록하고 넘어가되 연속 N번이면 멈춘다."""
    fails, failed = 0, []
    for i, r in enumerate(todo, 1):
        if cfg["mode"] == "영상마다 확인" and not ui.ask(f"진행할까요? ({i}/{len(todo)})\n\n[{r['채널']}] {r['파일']}\n{r['제목']}"):
            continue
        try:
            work(r)
            r["오류"], fails = "", 0
            ui.log(f"  ✓ ({i}/{len(todo)}) {r['파일']}")
        except Exception as e:
            if yt.is_quota(e):
                _save(ui, rows)
                raise Stop(QUOTA_MSG) from None
            r["오류"] = yt.error_text(e)
            failed.append(r)
            fails += 1
            ui.log(f"  ✗ ({i}/{len(todo)}) {r['파일']}: {r['오류']}")
            if fails >= cfg["max_fails"]:
                _save(ui, rows)
                raise Stop(f"{fails}번 연속 실패해서 멈췄습니다. 마지막 오류: {r['오류']}") from None
        _save(ui, rows)  # 영상 하나 끝날 때마다 저장 → 중간에 멈춰도 이어서 가능
    return failed


def _report(ui, title, failed, skipped):
    lines = [f"- {r['파일']}: {r['오류']}" for r in failed] + [f"- {m}" for m in skipped]
    if lines:
        ui.alert(f"{title}: 실패 {len(failed)}개, 건너뛴 채널 {len(skipped)}개\n" + "\n".join(lines))
    else:
        ui.notify(f"{title}: 모두 끝났습니다.")


def _move_safely(ui, r):
    try:
        core.move_done(r)
    except Exception as e:  # 파일이 다른 프로그램에서 열려 있으면 나중에 다시 옮긴다
        ui.log(f"  (파일을 완료 폴더로 옮기지 못함, 다음에 다시 시도: {e})")


def _retry_moves(ui, rows):
    for r in rows:
        if r["상태"] in core.DONE_STATES and (core.VIDEO_DIR / r["파일"]).exists():
            _move_safely(ui, r)


def status_summary(rows, cfg):
    """창 위쪽에 보여줄 채널별 진행 상황과 다음 할 일."""
    if not cfg["channels"]:
        return "설정에서 채널을 추가하세요."
    lines = []
    for ch in cfg["channels"]:
        mine = [r for r in rows if r["채널"] == ch["name"]]
        if not mine:
            continue
        c = Counter(r["상태"] if r["상태"] == core.MEMBER_TODO else "완료" if r["상태"] in core.DONE_STATES
                    else r["상태"] or "대기" for r in mine)
        errors = sum(1 for r in mine if r["오류"])
        parts = [f"{k} {c[k]}" for k in ("대기", "정보완료", "댓글완료", "완료", core.MEMBER_TODO) if c[k]]
        lines.append(f"{ch['name']}: " + " · ".join(parts) + (f" · 오류 {errors}" if errors else ""))
    states = {r["상태"] for r in rows}
    if "" in states:
        nxt = "엑셀에서 제목 확인 → 2. 정보·댓글 넣기"
    elif "정보완료" in states:
        nxt = "2. 정보·댓글 넣기 (남은 댓글)"
    elif "댓글완료" in states:
        nxt = "3. 고정 탭 열기 → 고정 → 4. 예약 걸기"
    elif core.MEMBER_TODO in states:
        nxt = "Studio에서 회원 전용 + 공개 예약 직접 설정 (엑셀 상태 '회원 설정 필요'인 영상) → 끝나면 다음 묶음"
    else:
        nxt = "0. 새 묶음 폴더에 파일 넣고 Studio에 업로드 → 1. 엑셀 만들기"
    return ("\n".join(lines) or "진행 중인 영상 없음") + f"\n▶ 다음 할 일: {nxt}"


# ---------- 1. 엑셀 만들기 ----------
def build_excel(ui):
    cfg = core.load_config()
    if not cfg["channels"]:
        return ui.alert("설정에서 채널을 먼저 추가해주세요.")
    old = _load(ui)
    _retry_moves(ui, old)
    rows, problems = core.build_rows(cfg, old)
    skipped = []
    for ch in cfg["channels"]:
        if not any(r["채널"] == ch["name"] and not r["예약일시"] for r in rows):
            continue
        client = _client(ui, ch, skipped)
        if not client:
            continue
        try:
            core.fill_schedule(rows, ch, yt.last_scheduled(yt.recent_videos(client)), _now())
        except Exception as e:
            problems.append(f"[{ch['name']}] 예약일시 계산 실패: {yt.error_text(e)}")
    _save(ui, rows)
    ui.log(f"엑셀을 만들었습니다: 영상 {len(rows)}개 → {core.EXCEL.name}")
    if skipped:
        problems += [f"{m} (예약일시는 4단계에서 채움)" for m in skipped]
    if problems:
        ui.alert("확인이 필요한 것이 있습니다:\n" + "\n".join(f"- {p}" for p in problems))
    ui.log("엑셀 열기 → 노란 칸(제목·회원우선·예약일시)만 확인·수정 → 저장하고 엑셀을 닫은 뒤 2단계를 누르세요.")


# ---------- 2. 정보·댓글 넣기 ----------
def _find_video(ui, client, r, by_id, recent, used):
    """이 행의 영상. 다른 영상을 잘못 고치지 않도록 확실할 때만 돌려준다."""
    name = Path(r["파일"]).name.lower()
    if r["영상ID"]:
        video = by_id.get(r["영상ID"]) or next(iter(yt.get_videos(client, [r["영상ID"]])), None)
        if not video:
            raise LookupError(f"영상ID {r['영상ID']}를 이 채널에서 못 찾았습니다. 엑셀의 영상ID를 확인해주세요.")
        if r["상태"] == "" and not yt.is_untouched(video):
            raise LookupError("이 영상ID는 이미 공개됐거나 예약된 영상입니다. 엑셀의 영상ID를 확인해주세요.")
        if yt.file_name(video) and yt.file_name(video).lower() != name and not ui.ask(
                f"영상ID {r['영상ID']}의 원본 파일은 '{yt.file_name(video)}'인데, 이 행의 파일은 '{Path(r['파일']).name}'입니다.\n"
                f"이 영상이 맞나요? (업로드한 뒤 파일 이름을 바꿨다면 '예')"):
            raise LookupError("영상ID의 원본 파일이 이 행의 파일과 다릅니다. 엑셀의 영상ID를 확인해주세요.")
        return video
    local = core.VIDEO_DIR / r["파일"]
    # 지난 묶음의 같은 이름 영상을 고르지 않게: 이 파일을 만든 뒤에 올라간 것만
    since = datetime.fromtimestamp(local.stat().st_mtime, timezone.utc) - timedelta(days=1) if local.exists() else None
    cands = [v for v in recent if yt.file_name(v).lower() == name and yt.is_untouched(v) and v["id"] not in used
             and not (since and v["snippet"].get("publishedAt") and yt.parse_dt_utc(v["snippet"]["publishedAt"]) < since)]
    if len(cands) == 1:
        return cands[0]
    if not cands:
        raise LookupError("Studio에서 이 파일의 초안을 못 찾았습니다. 업로드가 끝났는지 확인하거나, "
                          "영상 주소(https://youtu.be/… 등)를 엑셀 '영상ID' 칸에 붙여넣고 다시 눌러주세요.")
    raise LookupError(f"같은 파일 이름의 초안이 {len(cands)}개라 어느 것인지 모릅니다. "
                      "영상 주소(https://youtu.be/… 등)를 엑셀 '영상ID' 칸에 붙여넣어주세요.")


def _compose(ch, r, original):
    """보낼 내용. 항상 처음 백업한 초안 틀에서 합치므로 여러 번 실행해도 제목이 두 번 들어가지 않는다."""
    s = original["snippet"]
    title = core.merge(s.get("title", ""), r["제목"], ch["title_mode"], ch["title_marker"], " ")
    desc = core.merge(s.get("description", ""), r["설명"], ch["desc_mode"], ch["desc_marker"], "\n\n")
    tags = core.merge_tags(s.get("tags", []), core.parse_tags(r["태그"]), ch["tags_mode"])
    core.check_text(title, desc)
    if not r["제목"].strip():
        raise ValueError("엑셀의 제목 칸이 비어 있습니다.")
    if not r["설명"].strip():
        raise ValueError("설명이 비어 있습니다 (SEO 메모장을 못 찾음). 1단계를 다시 누르거나 엑셀 설명 칸을 채워주세요.")
    if r["예약일시"]:
        core.parse_dt(r["예약일시"], core.parse_slots(ch["slots"], ch["name"]))  # 형식 오류를 미리 알림
    if not r["썸네일"]:
        raise ValueError("썸네일을 못 찾았습니다. 썸네일을 채널 폴더에 넣고 1단계를 다시 누르거나, 없이 올리려면 썸네일 칸에 '없음'.")
    if r["썸네일"] != "없음":
        thumb = core.VIDEO_DIR / r["썸네일"]
        if not thumb.exists():
            raise FileNotFoundError(f"썸네일 파일이 없습니다: {r['썸네일']}")
        if thumb.stat().st_size > core.THUMB_MAX:
            raise ValueError(f"썸네일이 2MB를 넘습니다: {r['썸네일']}")
    snippet_over, status_over = core.overrides(ch)
    # 댓글을 달려면 일부공개여야 한다
    return {"title": title, "description": desc, "tags": tags} | snippet_over, status_over | {"privacyStatus": "unlisted"}


def fill_all(ui):
    cfg, rows = core.load_config(), _load(ui)
    todo_all = [r for r in rows if r["상태"] in ("", "정보완료")]
    if not todo_all:
        if any(r["상태"] == "댓글완료" for r in rows):
            return ui.log("정보·댓글은 모두 넣었습니다 → 3. 고정 탭 열기")
        return ui.log("2단계에서 할 영상이 없습니다. 1단계(엑셀 만들기)를 먼저 해주세요.")
    units = sum(yt.COST_FILL if r["상태"] == "" else 50 for r in todo_all)
    days = math.ceil(units / (yt.DAILY_QUOTA * 0.9))
    ui.log(f"영상 {len(todo_all)}개, 예상 할당량 {units:,} (하루 {yt.DAILY_QUOTA:,}) → "
           + ("오늘 다 됩니다." if days <= 1 else f"약 {days}일. 멈추면 다음 날 같은 버튼을 누르세요."))
    failed, skipped = [], []
    for ch in cfg["channels"]:
        todo = [r for r in todo_all if r["채널"] == ch["name"]]
        if not todo or not (client := _client(ui, ch, skipped)):
            continue
        ui.log(f"[{ch['name']}] {len(todo)}개 — 먼저 전부 점검합니다")
        recent = yt.recent_videos(client)
        by_id, videos, ready = {v["id"]: v for v in recent}, {}, []
        for r in todo:  # 사전 점검: YouTube를 고치기 전에 영상 찾기·제목 길이·썸네일을 모두 확인
            try:
                used = {x["영상ID"] for x in rows if x is not r and x["영상ID"]}
                video = _find_video(ui, client, r, by_id, recent, used)
                if r["상태"] == "":
                    _compose(ch, r, core.save_original(video))
                r["영상ID"], videos[video["id"]] = video["id"], video
                ready.append(r)
            except Exception as e:
                if yt.is_quota(e):
                    raise Stop(QUOTA_MSG) from None
                r["오류"] = yt.error_text(e)
                failed.append(r)
                ui.log(f"  ✗ 점검: {r['파일']}: {r['오류']}")
        _save(ui, rows)

        def work(r):
            video = videos[r["영상ID"]]
            if r["상태"] == "":
                yt.update_video(client, video, *_compose(ch, r, core.save_original(video)))
                if r["썸네일"] not in ("", "없음"):
                    yt.set_thumbnail(client, video["id"], core.VIDEO_DIR / r["썸네일"])
                if ch["playlist"].strip():
                    yt.add_to_playlist(client, ch["playlist"].strip(), video["id"])
                r["상태"] = "정보완료"
                _save(ui, rows)
            kids = video["status"].get("selfDeclaredMadeForKids") or ch["kids"] == "예"
            if r["고정댓글"].strip() and kids:
                ui.log(f"  (아동용 영상은 댓글을 달 수 없어 건너뜀: {r['파일']})")
            elif r["고정댓글"].strip():
                yt.post_comment(client, video["id"], r["고정댓글"].strip())
            r["상태"] = "댓글완료"

        failed += _loop(ui, cfg, rows, ready, work)
    _report(ui, "정보·댓글 넣기", failed, skipped)


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
    cfg, rows = core.load_config(), _load(ui)
    if not any(r["상태"] == "댓글완료" for r in rows):
        return ui.log("예약할 영상이 없습니다. 2단계(정보·댓글 넣기)와 3단계(고정)를 먼저 해주세요.")
    if not ui.ask("고정(수창 채널은 광고 설문까지)을 모두 마치셨나요?\n예를 누르면 예약을 겁니다."):
        return
    failed, skipped, manual = [], [], []
    for ch in cfg["channels"]:
        todo = [r for r in rows if r["채널"] == ch["name"] and r["상태"] == "댓글완료"]
        if not todo or not (client := _client(ui, ch, skipped)):
            continue
        recent = yt.recent_videos(client)
        try:
            if any(not r["예약일시"] for r in todo):
                core.fill_schedule(rows, ch, yt.last_scheduled(recent), _now())
        except ValueError as e:  # 엑셀의 예약일시 하나가 잘못돼도 다른 채널은 계속
            for r in todo:
                r["오류"] = str(e)
            failed += todo
            ui.log(f"[{ch['name']}] {e}")
            continue
        taken = {yt.parse_dt_utc(v["status"]["publishAt"]): v["id"] for v in recent if v["status"].get("publishAt")}
        slot_list = core.parse_slots(ch["slots"], ch["name"])
        slots = set(slot_list)
        times = Counter()
        for r in todo:
            try:
                times[core.parse_dt(r["예약일시"], slot_list)] += 1
            except ValueError:
                pass
        videos = {v["id"]: v for v in yt.get_videos(client, [r["영상ID"] for r in todo if r["영상ID"]])}

        def work(r):
            when = core.parse_dt(r["예약일시"], slot_list)
            r["예약일시"] = core.fmt_dt(when)  # 날짜만 적었던 칸은 실제 시간으로 채워 보여준다
            if when <= _now() + timedelta(minutes=15):
                raise ValueError(f"예약일시({r['예약일시']})가 지났거나 너무 가깝습니다. 엑셀에서 고쳐주세요.")
            if times[when] > 1:
                raise ValueError(f"같은 채널의 다른 영상과 예약일시({r['예약일시']})가 같습니다. 엑셀에서 고쳐주세요.")
            if taken.get(when, r["영상ID"]) != r["영상ID"]:
                raise ValueError(f"{r['예약일시']}에 이미 다른 영상이 예약돼 있습니다. 엑셀에서 고쳐주세요.")
            if (when.weekday(), when.hour, when.minute) not in slots and not ui.ask(
                    f"[{r['채널']}] {r['파일']}\n예약일시 {r['예약일시']}가 채널의 공개 요일·시간({ch['slots']})과 다릅니다.\n그대로 예약할까요?"):
                raise ValueError(f"예약일시({r['예약일시']})가 채널의 공개 요일·시간과 달라 건너뜀. 엑셀에서 확인해주세요.")
            if core.is_member(r["회원우선"]):
                manual.append(r)  # API에는 회원 전용 공개가 없어서 사용자가 직접 설정한다
                r["상태"] = core.MEMBER_TODO
            else:
                if r["영상ID"] not in videos:
                    raise LookupError("YouTube에서 이 영상을 못 찾았습니다 (삭제됐는지 확인)")
                yt.schedule(client, videos[r["영상ID"]], r["예약일시"])
                r["상태"] = "예약완료"
            _move_safely(ui, r)

        failed += _loop(ui, cfg, rows, todo, work)
        done = [r for r in todo if r["상태"] == "예약완료"]
        if done:  # 예약이 실제로 걸렸는지 다시 읽어 확인 (50개당 할당량 1)
            def wrong(r, check):
                st = check.get(r["영상ID"], {}).get("status", {})
                return st.get("privacyStatus") != "private" or \
                    yt.parse_dt_utc(st.get("publishAt")) != core.parse_dt(r["예약일시"], slot_list)
            bad = done
            for wait in (0, 10, 20, 30):  # YouTube는 수정한 내용을 몇 초~수십 초 늦게 보여줄 때가 있다
                time.sleep(wait)
                check = {v["id"]: v for v in yt.get_videos(client, [r["영상ID"] for r in bad])}
                bad = [r for r in bad if wrong(r, check)]
                if not bad:
                    break
            for r in bad:
                st = check.get(r["영상ID"], {}).get("status", {})
                r["오류"] = f"예약 확인 실패: YouTube에는 {st.get('privacyStatus')} / {st.get('publishAt')} 로 돼 있음. Studio에서 확인해주세요"
                failed.append(r)
            _save(ui, rows)
    _retry_moves(ui, rows)
    if manual:
        ui.alert("회원 전용으로 직접 설정할 영상:\n" + "\n".join(
            f"- [{r['채널']}] {r['제목']} → 회원 전용, {r['예약일시']} 전체 공개" for r in manual))
        if ui.ask("이 영상들의 Studio 화면을 열까요?"):
            open_tabs([f"https://studio.youtube.com/video/{r['영상ID']}/edit" for r in manual])
    _report(ui, "예약 걸기", failed, skipped)
