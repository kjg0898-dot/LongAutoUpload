"""버튼 하나 = 단계 하나. ui는 log(글), ask(질문)->bool, alert(경고), notify(끝났을 때 알림)을 가진 객체."""
import json
import math
import os
import re
import shutil
import time
import webbrowser
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
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
        try:
            return yt.client_for(ch)
        except yt.NotLoggedIn:
            if not (ch["channel_id"] and ui.ask(f"[{ch['name']}] 로그인이 만료됐습니다. 지금 다시 로그인할까요?\n"
                                                "브라우저가 열리면 이 채널의 계정과 채널을 고르세요.")):
                raise
            return yt.client_for(ch, interactive=True)
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
        nxt = "2. 자동 처리"
    elif "정보완료" in states:
        nxt = "2. 자동 처리 (남은 댓글)"
    elif "댓글완료" in states:
        nxt = "3. 댓글 고정 → 4. 예약 걸기"
    elif core.MEMBER_TODO in states:
        nxt = "Studio에서 회원 전용 + 공개 예약 직접 설정 (엑셀 상태 '회원 설정 필요'인 영상) → 끝나면 다음 묶음"
    else:
        nxt = "1. 준비 (SEO 넣기 → 썸네일 넣기, 영상은 Studio에 업로드) → 2. 자동 처리"
    return ("\n".join(lines) or "진행 중인 영상 없음") + f"\n▶ 다음 할 일: {nxt}"


def card_status(rows, has_videos=False):
    """화면 왼쪽 단계 카드: ({카드: 설명}, 다음 할 카드). 카드 = 1 영상 준비, 2 자동 처리, 3 댓글 고정, 4 예약 걸기."""
    c = Counter(r["상태"] for r in rows)
    done = sum(c[s] for s in core.DONE_STATES)
    errors = sum(1 for r in rows if r["오류"])
    member, left = c[core.MEMBER_TODO], c[""] + c["정보완료"]
    text = {
        "1": f"이번 묶음 영상 {len(rows)}개" if rows else "SEO 넣기 → 작업표 보고 썸네일 만들기 → 썸네일 넣기\n영상은 Studio에 업로드",
        "2": (f"남은 {left}개" if left else "완료" if rows else "목록 → 제목 확인 → 제목·설명·썸네일·댓글")
             + (f" · 오류 {errors}" if errors else ""),
        "3": f"고정할 영상 {c['댓글완료']}개" if c["댓글완료"] else "탭을 열어 댓글 고정 (직접)",
        "4": (f"예약할 영상 {c['댓글완료']}개" if c["댓글완료"] else "예약 + 완료 폴더로 이동")
             + (f" · 완료 {done}" if done else "") + (f" · 회원 직접설정 {member}" if member else ""),
    }
    if left:
        nxt = "2"
    elif c["댓글완료"]:
        nxt = "3"
    elif member:
        nxt = "4"
    elif rows:
        nxt = "1"
    else:
        nxt = "2" if has_videos else "1"
    return text, nxt


# ---------- 1. 엑셀 만들기 ----------
def build_excel(ui):
    cfg = core.load_config()
    if not cfg["channels"]:
        return ui.alert("설정에서 채널을 먼저 추가해주세요.")
    old = _load(ui)
    _retry_moves(ui, old)
    # Studio 초안(원본 파일 이름)을 읽어 둔다: 폴더에 영상 파일이 없어도 되고, 예약일시 계산에도 쓴다
    recent, skipped, problems = {}, [], []
    for ch in cfg["channels"]:
        if core.channel_dirs(ch) and (client := _client(ui, ch, skipped)):
            try:
                recent[ch["name"]] = yt.recent_videos(client)
            except Exception as e:
                if yt.is_quota(e):
                    raise Stop(QUOTA_MSG) from None
                problems.append(f"[{ch['name']}] Studio 영상 목록을 못 읽음: {yt.error_text(e)}")
    drafts = {}
    for name, videos in recent.items():
        mine = drafts[name] = {}
        for v in sorted(videos, key=lambda v: v["snippet"].get("publishedAt", "")):  # 같은 이름이면 최근 것
            if yt.is_untouched(v) and yt.file_name(v):
                mine[yt.file_name(v)] = v["id"]
    uploaded = {v["id"]: yt.file_name(v) for videos in recent.values() for v in videos}
    rows, more = core.build_rows(cfg, old, drafts, uploaded)
    problems += more
    for ch in cfg["channels"]:
        if ch["name"] in recent and any(r["채널"] == ch["name"] and not r["예약일시"] for r in rows):
            try:
                core.fill_schedule(rows, ch, yt.last_scheduled(recent[ch["name"]]), _now())
            except Exception as e:
                problems.append(f"[{ch['name']}] 예약일시 계산 실패: {yt.error_text(e)}")
    _save(ui, rows)
    ui.log(f"엑셀을 만들었습니다: 영상 {len(rows)}개 → {core.EXCEL.name}")
    if skipped:
        problems += [f"{m} (예약일시는 4단계에서 채움)" for m in skipped]
    if problems:
        ui.alert("확인이 필요한 것이 있습니다:\n" + "\n".join(f"- {p}" for p in problems))
    ui.log("제목·예약일시를 바꾸려면 오른쪽 목록에서 영상을 더블클릭하세요.")


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
    template = s.get("title", "")
    if template.strip() == Path(r["파일"]).stem.strip():
        template = ""  # Studio 제목 틀이 없는 채널: 초안 제목이 파일 이름이라 통째로 바꾼다
    title = core.merge(template, r["제목"], ch["title_mode"], ch["title_marker"], " ")
    desc = core.merge(s.get("description", ""), r["설명"], ch["desc_mode"], ch["desc_marker"], "\n\n")
    tags = core.merge_tags(s.get("tags", []), core.parse_tags(r["태그"]), ch["tags_mode"])
    core.check_text(title, desc)
    if not r["제목"].strip():
        raise ValueError("엑셀의 제목 칸이 비어 있습니다.")
    if not r["설명"].strip():
        raise ValueError("설명이 비어 있습니다 (SEO 메모장을 못 찾음). '2. 자동 처리'를 다시 누르세요.")
    if r["예약일시"]:
        core.parse_dt(r["예약일시"], core.parse_slots(ch["slots"], ch["name"]))  # 형식 오류를 미리 알림
    if not r["썸네일"] and ch["thumb_auto"]:
        raise ValueError(f"썸네일을 만들지 못했습니다. 채널 폴더/{core.MATERIAL}/ 의 같은 번호 사진과 썸네일 문구를 확인하고 "
                         "'2. 자동 처리'를 다시 누르세요.")
    if not r["썸네일"]:
        raise ValueError("썸네일을 못 찾았습니다. 썸네일을 채널 폴더에 넣고 '2. 자동 처리'를 다시 누르거나, 목록에서 오른쪽 클릭 → '썸네일 없이 올리기'.")
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
            return ui.log("정보·댓글은 모두 넣었습니다 → 3. 댓글 고정")
        return ui.log("넣을 영상이 없습니다. 폴더에 SEO 메모장·썸네일을 넣고 영상을 Studio에 올린 뒤 '2. 자동 처리'를 눌러주세요.")
    extra = {ch["name"]: 50 * max(len(core.playlist_ids(ch["playlist"])) - 1, 0) for ch in cfg["channels"]}  # 재생목록 하나는 COST_FILL에 포함
    units = sum(yt.COST_FILL + extra.get(r["채널"], 0) if r["상태"] == "" else 50 for r in todo_all)
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
                for playlist in core.playlist_ids(ch["playlist"]):
                    yt.add_to_playlist(client, playlist, video["id"])
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


# ---------- 2. 자동 처리 (목록 → 제목 확인 → 정보·댓글, 그다음 고정 창) ----------
def auto_all(ui):
    build_excel(ui)
    rows = core.read_rows()
    new = [r for r in rows if r["상태"] == ""]
    if new:  # 썸네일을 보며 제목(과 썸네일 문구)을 고르는 창. 처음 값은 SEO 추천
        picked = ui.review_titles(new)
        if picked is None:
            return ui.log("제목 확인을 취소했습니다. '2. 자동 처리'를 다시 누르면 이어서 합니다.")
        changed = [(r, k, v) for r in new for k, v in picked.get(r["파일"], {}).items() if r[k] != v]
        for r, k, v in changed:
            r[k] = v
            if core.row_num(r) is not None:  # 준비 화면과 같게 (다시 만들 때 고친 값이 되돌아가지 않게)
                remember((core.VIDEO_DIR / r["파일"]).parent, core.row_num(r), **{k: v})
        if changed:
            _save(ui, rows)
        auto = {c["name"] for c in core.load_config()["channels"] if c["thumb_auto"]}
        targets = [r for r in new if r["채널"] in auto]
        if targets:
            made = make_thumbnails(ui, rows, targets)  # 준비 화면에서 이미 만든 것은 그대로라 다시 안 보여줌
            if made and not ui.review_thumbs(made):
                return ui.log("썸네일 확인을 취소했습니다. 문구를 고친 뒤 '2. 자동 처리'를 다시 누르세요.")
    fill_all(ui)
    return bool(pin_targets(core.read_rows(), core.load_config()))


def make_thumbnails(ui, rows, targets, force=False):
    """썸네일 자동 만들기 채널의 영상들. Claude 답을 기다리는 시간이 길어서 4개씩 함께 만든다."""
    import thumbs
    cfg = core.load_config()
    chans, key = {c["name"]: c for c in cfg["channels"]}, cfg.get("claude_key", "")
    ui.log(f"썸네일 {len(targets)}개를 만듭니다" + ("" if key or os.environ.get("ANTHROPIC_API_KEY")
                                                 else " (Claude API 키가 없어 실제 사진 없이, 강조는 마지막 줄)"))

    def one(r):
        try:
            r["썸네일"] = core.rel(thumbs.make(chans[r["채널"]], r, key, ui.log, force))
            r["오류"] = ""
            ui.log(f"  ✓ 썸네일: {Path(r['파일']).name}")
        except Exception as e:
            r["오류"] = f"썸네일 만들기 실패: {e}"
            ui.log(f"  ✗ 썸네일: {Path(r['파일']).name}: {e}")
    import ai
    before = len(ai.USAGE)
    stamp = {r["파일"]: _mtime(r) for r in targets}
    with ThreadPoolExecutor(4) as pool:
        list(pool.map(one, targets))
    if len(ai.USAGE) > before:
        used = ai.USAGE[before:]
        ui.log(f"Claude 사용: 요청 {len(used)}번, 입력 {sum(i for i, _ in used):,} · 출력 {sum(o for _, o in used):,} 토큰 "
               f"→ 약 ${ai.cost(used):.2f}")
    _save(ui, rows)
    return [r for r in targets if not r["오류"] and _mtime(r) != stamp[r["파일"]]]  # 새로 만든 것


def _mtime(r):
    p = core.VIDEO_DIR / r["썸네일"] if r["썸네일"] not in ("", "없음") else None
    return p.stat().st_mtime_ns if p and p.exists() else None


# ---------- 1. 준비 화면 (SEO를 넣은 뒤: 후보 그림 고르기·제목·문구, 최종 썸네일) ----------
def remember(folder, num, **fields):
    """준비 화면에서 고른 것 저장 (채널 폴더/제작.json)."""
    data = core.load_prep(folder)
    data.setdefault(str(num), {}).update(fields)
    core.save_prep(folder, data)


def prep_items(ch):
    """이 채널의 마지막 묶음에서 SEO 항목마다: 순번·SEO 내용 + 고른 것 + 후보 그림 + 실제 사진 + 최종 썸네일."""
    dirs = core.channel_dirs(ch)
    if not dirs:
        return None, []
    folder = dirs[-1]
    entries = [e for e in core.load_entries(folder.rglob("*.txt")) if e["num"] is not None]
    prep, out = core.load_prep(folder), []
    for e in sorted(entries, key=lambda e: e["num"]):
        p, texts = prep.get(str(e["num"]), {}), e.get("thumb_texts", [])
        final = folder / f"{e['num']}_자동썸네일.jpg"
        photo = folder / core.PHOTOS / f"{e['num']}.jpg"
        out.append({"num": e["num"], "orig": e["orig"], "titles": e["titles"], "thumb_texts": texts,
                    "제목": p.get("제목") or e["title"],
                    "썸네일문구": p.get("썸네일문구") or (texts[e.get("thumb_pick", 0)] if texts else ""),
                    "설명": p.get("설명") or e["desc"], "고정댓글": p.get("고정댓글") or e["comment"],
                    "선택": core.material_for(folder, e["num"]), "후보": core.candidates(folder, e["num"]),
                    "실제사진": photo if photo.exists() else None, "최종": final if final.exists() else None})
    return folder, out


def drop_number(path):
    """끌어다 놓은 그림의 순번: 번호 폴더(3/, 3번/) 안이면 그 번호, 아니면 파일 이름 맨 앞의 번호(3.png, 3번_a.png).
    이름 중간의 숫자는 보지 않는다 (Gemini_Generated_Image_7k2… 같은 이름이 엉뚱한 영상으로 가지 않게)."""
    p = Path(path)
    m = re.fullmatch(r"\s*(\d{1,3})\s*번?\s*", p.parent.name) or re.match(r"\s*(\d{1,3})(?:번|[\s_\-.)]|$)", p.stem)
    return int(m[1]) if m else None


def _name_key(name):
    """채널을 알아보려고 이름을 고른다: 띄어쓰기·기호와 'seo'·'썸네일' 같은 말을 뺀다."""
    key = re.sub(r"[\s_\-.()\[\]]+", "", str(name)).lower()
    return re.sub(r"(seo|썸네일|thumbnails?|thumb|메모장|파일)+$", "", key)


def channel_of(name, channels):
    """이름(파일·폴더)에서 채널: 채널 이름·영상 폴더 이름·YouTube 채널 이름과 같으면, 아니면 한쪽이 다른 쪽을 품으면."""
    key = _name_key(name)
    if not key:
        return None
    for exact in (True, False):
        for ch in channels:
            for cand in (ch["name"], ch["folder"], ch.get("channel_title", "")):
                c = _name_key(cand)
                if c and (c == key if exact else len(c) >= 2 and (c in key or key in c)):
                    return ch
    return None


def route(paths, channels):
    """폴더째 끌어다 놓은 것 나누기: {'seo': {채널: [메모장]}, 'pics': {채널: {순번: [그림]}}, 'unknown_seo': [...], 'unknown_pics': [...]}.
    메모장은 파일 이름(채널seo.txt) 또는 폴더 이름으로, 그림은 manifest.json → 이름(채널_번호_a.png) → 번호 폴더 순으로 채널·순번을 찾는다."""
    files = []
    for p in map(Path, paths):
        files += sorted(f for f in p.rglob("*") if f.is_file()) if p.is_dir() else [p]
    manifest = {}
    for m in (f for f in files if f.name.lower() == "manifest.json"):
        try:
            data = json.loads(core.read_text(m))
        except ValueError:
            continue
        for seq, e in (data.get("entries") or {}).items():
            for name in e.get("files", []):
                manifest[name] = (data.get("channel", ""), int(e.get("seq") or seq))
    out = {"seo": {}, "pics": {}, "unknown_seo": [], "unknown_pics": []}
    by_name = {ch["name"]: ch for ch in channels}
    for f in files:
        if f.suffix.lower() == ".txt":
            if core.SKIP_TXT.search(f.name):
                continue
            ch = channel_of(f.stem, channels) or channel_of(f.parent.name, channels)
            (out["seo"].setdefault(ch["name"], []) if ch else out["unknown_seo"]).append(f)
        elif f.suffix.lower() in core.IMAGE_EXT:
            ch, num = None, None
            if f.name in manifest:
                ch, num = channel_of(manifest[f.name][0], channels), manifest[f.name][1]
            if not ch and (m := re.match(r"^(.*?)[\s_\-]+(\d{1,3})(?:[\s_\-.].*)?$", f.stem)):
                ch, num = channel_of(m[1], channels), int(m[2])
            if not ch:
                ch = next((c for c in (channel_of(d.name, channels) for d in list(f.parents)[:3]) if c), None)
                num = num if num is not None else drop_number(f)
            if ch and num is not None:
                out["pics"].setdefault(ch["name"], {}).setdefault(num, []).append(f)
            else:
                out["unknown_pics"].append(f)
    out["channels"] = by_name
    return out


def guess_channel_name(path):
    """설정에 없는 채널의 SEO 메모장 이름에서 채널 이름 짐작 (예: 한국사인물관seo.txt → 한국사인물관)."""
    return re.sub(r"[\s_\-]*(seo|SEO|썸네일)$", "", Path(path).stem).strip()


def add_candidates(folder, num, paths):
    """후보 그림을 채널 폴더/재료/<순번>/ 에 넣는다 (이름이 같으면 뒤에 번호)."""
    dest = Path(folder) / core.MATERIAL / str(num)
    dest.mkdir(parents=True, exist_ok=True)
    out = []
    for p in map(Path, paths):
        target, n = dest / p.name, 2
        while target.exists():
            target, n = dest / f"{p.stem}_{n}{p.suffix}", n + 1
        shutil.copy2(p, target)
        out.append(target)
    return out


# ---------- 4. 예약 걸기 ----------


def thumb_test(ui):
    """썸네일 시험: 실제 Claude·위키미디어로 하나 만들어 보고 결과를 파일로 남긴다 (YouTube에는 아무것도 안 함).
    (그림 경로, 결과 파일 경로)"""
    import ai
    import thumbs
    from PIL import Image, ImageDraw
    cfg, lines = core.load_config(), []

    def note(msg):
        lines.append(msg)
        ui.log(msg)
    ch = next((c for c in cfg["channels"] if c["thumb_auto"]), None) or (cfg["channels"] or [core.new_channel("시험")])[0]
    try:
        rows = core.read_rows()
    except Exception:
        rows = []
    row = next((r for r in rows if r["채널"] == ch["name"] and r["설명"]), None)
    title = row["제목"] if row else "걸프전 병사들이 모래바람 속에서 삼킨 '전투식량' | 미국VS이라크"
    desc = row["설명"] if row else "미군 병사들이 비웃던 걸프전 전투식량 MRE가 이라크군 참호에서는 전혀 다른 물건이었습니다."
    text = (row or {}).get("썸네일문구") or "미군은 버린 봉지 / 적군은 백기 들었다"
    base = (row and core.material(row)) or next((p for p in core.VIDEO_DIR.rglob("*") if core.MATERIAL in p.parts
                                                  and p.suffix.lower() in core.IMAGE_EXT), None)
    if not base:  # 재료 사진이 아직 없으면 시험용 그림
        base = Image.new("RGB", (1600, 900), "#b08a5a")
        ImageDraw.Draw(base).ellipse((950, 120, 1350, 900), fill="#5a4426")
    key = cfg.get("claude_key", "")
    tpl = thumbs.load_template(ch)
    note(f"썸네일 시험 {datetime.now():%Y-%m-%d %H:%M} — 채널 '{ch['name']}', 문구 '{text}'")
    note(f"  배경: {base if isinstance(base, Path) else '시험용 그림 (재료 사진 없음)'}")
    note(f"  틀: {'틀.json 있음' if (thumbs.template_dir(ch) / '틀.json').exists() else '기본 틀'}, "
         f"로고: {'있음' if (thumbs.template_dir(ch) / tpl['logo'].get('file', '')).is_file() else '없음'}")
    try:
        note(f"  폰트: {thumbs.font_path(ch, tpl)}")
    except Exception as e:
        note(f"✗ 폰트: {e}")
    note(f"  API 키: {'설정에 있음 (' + key[:10] + '…)' if key else ('환경변수 ANTHROPIC_API_KEY' if os.environ.get('ANTHROPIC_API_KEY') else '없음')}")
    before, start, plan, photo, credit = len(ai.USAGE), time.time(), {}, None, ""
    if key or os.environ.get("ANTHROPIC_API_KEY"):
        try:
            cl = ai.client(key)
            plan = ai.plan(cl, base, text, title, desc, tpl["text"]["sides"], (tpl.get("inset") or {}).get("boxes", ()))
            note(f"✓ Claude 배치: 글자 {plan.get('side')}, 강조 {plan.get('accents')}, 검색어 {plan.get('queries')}")
            if tpl.get("inset"):
                photo, credit = ai.find_photo(cl, plan.get("queries", []), text, title, note)
                note(f"✓ 실제 사진: {credit}" if photo else "  실제 사진: 알맞은 후보 없음 (사진 없이 만듦)")
        except Exception as e:
            note(f"✗ Claude·위키미디어: {e}")
    else:
        note("  (API 키가 없어 Claude·실제 사진 없이)")
    used = ai.USAGE[before:]
    if used:
        note(f"  Claude 요청 {len(used)}번, 입력 {sum(i for i, _ in used):,} · 출력 {sum(o for _, o in used):,} 토큰 → 약 ${ai.cost(used):.3f}")
    image = core.PROG / "썸네일_시험.jpg"
    try:
        thumbs.save(thumbs.compose(ch, base, text, plan, photo), image)
        note(f"✓ 썸네일을 만들었습니다 ({time.time() - start:.0f}초): {image}")
    except Exception as e:
        image = None
        note(f"✗ 썸네일 만들기: {e}")
    report = core.PROG / "썸네일_시험_결과.txt"
    report.write_text("\n".join(lines) + "\n", "utf-8")
    note("결과 파일과 썸네일 그림을 Claude에게 보내주세요.")
    return image, report


# ---------- 4. 예약 걸기 ----------
def schedule_all(ui):
    cfg, rows = core.load_config(), _load(ui)
    if not any(r["상태"] == "댓글완료" for r in rows):
        return ui.log("예약할 영상이 없습니다. '2. 자동 처리'와 '3. 댓글 고정'을 먼저 해주세요.")
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
