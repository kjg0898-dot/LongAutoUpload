"""창 화면. 오래 걸리는 일은 별도 스레드에서 돌리고, 화면 갱신은 메인 스레드에서만 한다."""
import json
import os
import queue
import random
import re
import shutil
import subprocess
import sys
import threading
import tkinter as tk
from datetime import datetime, timedelta
from pathlib import Path
from tkinter import font as tkfont
from tkinter import filedialog, messagebox, scrolledtext, ttk

import core

PACKAGES = ["openpyxl", "google-api-python-client", "google-auth-oauthlib", "pillow", "anthropic"]
NO_WINDOW = 0x08000000  # 설치·알림 때 검은 창이 뜨지 않게
FONT = "맑은 고딕"


def ensure_packages():
    try:
        import google_auth_oauthlib  # noqa: F401
        import googleapiclient  # noqa: F401
        import openpyxl  # noqa: F401
        import PIL  # noqa: F401
        import anthropic  # noqa: F401
    except ImportError:
        subprocess.check_call([sys.executable, "-m", "pip", "install", "--quiet", *PACKAGES],
                              creationflags=NO_WINDOW if sys.platform == "win32" else 0)
    try:
        import tkinterdnd2  # noqa: F401  파일 끌어다 놓기 (없으면 버튼으로만)
    except ImportError:
        subprocess.call([sys.executable, "-m", "pip", "install", "--quiet", "tkinterdnd2"],
                        creationflags=NO_WINDOW if sys.platform == "win32" else 0)


def toast(title, msg):
    if sys.platform != "win32":
        return
    q = lambda s: s.replace("'", "''")[:250]  # noqa: E731  PowerShell 작은따옴표 이스케이프
    ps = ("Add-Type -AssemblyName System.Windows.Forms,System.Drawing;"
          "$n=New-Object System.Windows.Forms.NotifyIcon;$n.Icon=[System.Drawing.SystemIcons]::Information;"
          f"$n.Visible=$true;$n.ShowBalloonTip(10000,'{q(title)}','{q(msg)}','Info');Start-Sleep 11;$n.Dispose()")
    subprocess.Popen(["powershell", "-NoProfile", "-Command", ps], creationflags=NO_WINDOW)


def open_path(path):
    path = str(path)
    if sys.platform == "win32":
        if path.endswith(".md"):  # .md는 연결된 프로그램이 없는 경우가 많아 메모장으로
            subprocess.Popen(["notepad", path])
        else:
            os.startfile(path)


def short(msg, limit=12):
    """메시지 창이 화면보다 길어지지 않게."""
    lines = msg.split("\n")
    if len(lines) <= limit:
        return msg
    return "\n".join(lines[:limit] + [f"… 외 {len(lines) - limit}줄 — 엑셀 '오류' 칸과 창 아래 기록을 확인하세요."])


COLORS = {"next": "#FFF4CC", "card": "#FFFFFF", "edge": "#D9D9D9", "muted": "#666666", "accent": "#C0392B",
          "bad": "#F8D7DA", "done": "#E2F0D9", "member": "#FCE4D6", "work": "#EAF1FB"}


class App:
    def __init__(self, root):
        self.root, self.q, self.busy, self.settings, self.closing = root, queue.Queue(), False, None, False
        root.title("롱폼 자동 업로드")
        import steps
        self.steps, self.rows = steps, []

        # 맨 위: 제목과 도구 버튼
        top = ttk.Frame(root, padding=(12, 10, 12, 4))
        top.pack(fill="x")
        ttk.Label(top, text="롱폼 자동 업로드", font=(FONT, 15, "bold")).pack(side="left")
        # 자주 안 쓰는 것은 '더보기'에 모은다
        more = ttk.Menubutton(top, text="더보기")
        menu = tk.Menu(more, tearoff=0)
        for item in (("영상 폴더 열기", lambda: self.open_dir(core.VIDEO_DIR)), ("빈 묶음 폴더 만들기", self.make_batch),
                          ("완료 폴더 열기", lambda: self.open_dir(core.DONE_DIR)),
                          ("사진·썸네일 넣기", self.add_thumbs),
                          ("엑셀로 열기", self.open_excel), ("작업 기록 보기", lambda: self.open_dir(core.LOG_DIR)), None,
                          ("목록(엑셀)만 다시 만들기", lambda: self.run(steps.build_excel)),
                          ("정보·댓글만 넣기", lambda: self.run(steps.fill_all)), None,
                          ("썸네일 시험 (실제 Claude)", self.thumb_test),
                          ("댓글 답글 달기 (켠 채널만)", lambda: self.run(steps.reply_comments, "댓글 답글")), None,
                          ("업데이트 확인", self.update), ("청소", self.clean_window)):
            menu.add_separator() if item is None else menu.add_command(label=item[0], command=item[1])
        more["menu"] = menu
        more.pack(side="right", padx=2)
        for text, cmd in (("안내", self.open_help), ("⚙ 설정", self.open_settings)):
            ttk.Button(top, text=text, command=cmd).pack(side="right", padx=2)

        body = ttk.PanedWindow(root, orient="horizontal")
        body.pack(fill="both", expand=True, padx=12, pady=4)
        left, right = ttk.Frame(body), ttk.Frame(body)
        body.add(left, weight=0)
        body.add(right, weight=1)

        # 왼쪽: 단계 카드 (다음 할 단계를 노랗게)
        self.cards, self.buttons = {}, {}
        self.step_buttons = []
        for key, title, actions in (
                ("1", "준비", (("SEO 넣기", self.import_seo), ("준비 화면", self.open_prep),
                              ("작업표 열기", self.open_sheet))),
                ("2", "자동 처리", (("▶ 시작", self.auto_run),)),
                ("3", "댓글 고정", (("탭 열기", self.pin_window),)),
                ("4", "예약 걸기", (("실행", lambda: self.run(steps.schedule_all)),))):
            card = tk.Frame(left, bg=COLORS["card"], highlightthickness=1, highlightbackground=COLORS["edge"], padx=10, pady=8)
            card.pack(fill="x", pady=3)
            badge = tk.Label(card, text=key, width=2, font=(FONT, 13, "bold"),
                             bg=COLORS["card"], fg=COLORS["muted"])
            badge.grid(row=0, column=0, rowspan=2, sticky="n", padx=(0, 8))
            name = tk.Label(card, text=title, font=(FONT, 11, "bold"), bg=COLORS["card"], anchor="w")
            name.grid(row=0, column=1, sticky="w")
            info = tk.Label(card, text="", font=(FONT, 9), fg=COLORS["muted"], bg=COLORS["card"], anchor="w",
                            justify="left", wraplength=230)
            info.grid(row=1, column=1, sticky="w")
            stack = tk.Frame(card, bg=COLORS["card"])
            stack.grid(row=0, column=2, rowspan=2, padx=(10, 0))
            for label, cmd in actions:
                button = ttk.Button(stack, text=label, command=cmd, width=11)
                button.pack(pady=1)
                self.step_buttons.append(button)
            card.columnconfigure(1, weight=1)
            self.cards[key] = (card, badge, name, info, stack)
            self.buttons[key] = stack.winfo_children()[0]
        self.drop_hint = ttk.Label(left, foreground=COLORS["muted"], wraplength=380, justify="left")
        self.drop_hint.pack(anchor="w", pady=(6, 0))

        # 오른쪽: 영상 목록
        head = ttk.Frame(right)
        head.pack(fill="x")
        ttk.Label(head, text="영상 목록", font=(FONT, 11, "bold")).pack(side="left")
        self.count = ttk.Label(head, foreground=COLORS["muted"])
        self.count.pack(side="left", padx=8)
        ttk.Button(head, text="새로고침", command=self.refresh_status).pack(side="right")
        self.channel_filter = tk.StringVar(value="전체 채널")
        self.filter_box = ttk.Combobox(head, textvariable=self.channel_filter, state="readonly", width=16)
        self.filter_box.pack(side="right", padx=4)
        self.filter_box.bind("<<ComboboxSelected>>", lambda e: self.fill_table())
        self.only_problems = tk.BooleanVar()
        ttk.Checkbutton(head, text="오류만", variable=self.only_problems, command=self.fill_table).pack(side="right", padx=4)

        cols = (("채널", 100), ("파일", 170), ("제목", 330), ("예약일시", 120), ("상태", 110), ("오류", 260))
        frame = ttk.Frame(right)
        frame.pack(fill="both", expand=True, pady=(4, 0))
        self.table = ttk.Treeview(frame, columns=[c for c, _ in cols], show="headings", selectmode="browse")
        for col, width in cols:
            self.table.heading(col, text=col)
            self.table.column(col, width=width, minwidth=60, stretch=col in ("제목", "오류"))
        scroll = ttk.Scrollbar(frame, orient="vertical", command=self.table.yview)
        self.table.configure(yscrollcommand=scroll.set)
        self.table.pack(side="left", fill="both", expand=True)
        scroll.pack(side="right", fill="y")
        for tag in ("bad", "done", "member", "work"):
            self.table.tag_configure(tag, background=COLORS[tag])
        self.table.bind("<Double-1>", lambda e: self.edit_selected())
        self.table.bind("<Return>", lambda e: self.edit_selected())
        self.table.bind("<Button-3>", self.row_menu)
        ttk.Label(right, foreground=COLORS["muted"], text=(
            "더블클릭: 제목·예약일시 고치기  ·  오른쪽 클릭: Studio 열기·다시 하기·썸네일 없이 등\n"
            "색: 빨강=오류, 파랑=진행 중, 초록=완료, 주황=회원 직접설정")).pack(anchor="w", pady=(4, 0))

        # 아래: 상태 줄과 기록
        bottom = ttk.Frame(root, padding=(12, 2, 12, 8))
        bottom.pack(fill="x")
        self.summary = ttk.Label(bottom, justify="left", foreground=COLORS["muted"])
        self.summary.pack(side="left")
        self.progress = ttk.Progressbar(bottom, mode="determinate", length=180)
        self.progress.pack(side="right")
        self.state = ttk.Label(bottom, text="대기 중")
        self.state.pack(side="right", padx=8)
        self.text = scrolledtext.ScrolledText(root, wrap="word", font=(FONT, 9), height=5)
        self.text.pack(fill="x", padx=12, pady=(0, 10))

        root.protocol("WM_DELETE_WINDOW", self.close)
        root.bind("<F5>", lambda e: self.refresh_status())
        self.first_run_guide()
        self.enable_drop()
        self.refresh_status()
        root.after(100, self.poll)
        self.check_update()
        root.after(10 * 60 * 1000, self.reply_tick)

    # --- 스레드에서 불러도 안전한 함수들 ---
    def log(self, msg):
        self.q.put(("log", msg))
        try:  # 창을 닫아도 남도록 날짜별 기록 파일에도
            core.LOG_DIR.mkdir(parents=True, exist_ok=True)
            with open(core.LOG_DIR / f"{datetime.now():%Y-%m-%d}.log", "a", encoding="utf-8") as f:
                f.write(f"[{datetime.now():%H:%M:%S}] {msg}\n")
        except OSError:
            pass

    def alert(self, msg):
        toast("롱폼 자동 업로드", msg)
        self.log(msg)
        self.q.put(("alert", short(msg)))

    def notify(self, msg):
        toast("롱폼 자동 업로드", msg)
        self.log(msg)

    def later(self, fn):
        """화면을 건드리는 일은 메인 스레드에서 (poll이 실행)."""
        self.q.put(("call", fn))

    def ask(self, question):
        if threading.current_thread() is threading.main_thread():
            return messagebox.askyesno("확인", question)
        answer, done = [], threading.Event()
        self.q.put(("ask", (question, answer, done)))
        done.wait()
        return answer[0]

    def poll(self):
        while not self.q.empty() and not self.closing:
            kind, data = self.q.get()
            if kind == "log":
                self.text.insert("end", data + "\n")
                self.text.see("end")
            elif kind == "alert":
                messagebox.showwarning("알림", data)
            elif kind == "call":
                data()
            else:
                question, answer, done = data
                answer.append(messagebox.askyesno("확인", question))
                done.set()
        if not self.closing:
            self.root.after(100, self.poll)

    # --- 작업 실행 ---
    def set_busy(self, label):
        self.busy = bool(label)
        for b in self.step_buttons:
            b.state(["disabled"] if label else ["!disabled"])
        self.state.config(text=f"작업 중: {label}" if label else "대기 중")
        if label:
            self.progress.config(mode="indeterminate")
            self.progress.start(15)
        else:  # 쉬는 동안엔 빈 막대 (움직이는 조각이 남아 작업 중처럼 보이지 않게)
            self.progress.stop()
            self.progress.config(mode="determinate", value=0)

    def run(self, fn, label=None):
        if self.busy:
            return messagebox.showinfo("잠시만요", "이전 작업이 아직 진행 중입니다.")
        names = {"build_excel": "목록 만들기", "fill_all": "정보·댓글 넣기", "schedule_all": "4. 예약 걸기"}
        self.set_busy(label or names.get(fn.__name__, "작업"))

        def work():
            try:
                fn(self)
            except Exception as e:
                if str(e) == self.steps.QUOTA_MSG:
                    self.later(lambda: self.resume_later(fn, label))
                self.alert(f"멈췄습니다: {e}")
            finally:
                self.later(lambda: (self.set_busy(None), self.refresh_status()))
        threading.Thread(target=work, daemon=True).start()

    def resume_later(self, fn, label):
        """할당량은 미국 서부 자정(한국 오후 4~5시)에 다시 찬다. 창이 켜져 있으면 그때 같은 작업을 다시 한다."""
        now = datetime.now(core.KST)
        at = now.replace(hour=17, minute=5, second=0, microsecond=0)
        at += timedelta(days=1) if at <= now else timedelta()
        self.log(f"창을 켜 두면 {at:%m월 %d일 %H:%M}에 자동으로 이어서 합니다.")
        self.root.after(int((at - now).total_seconds() * 1000),
                        lambda: self.busy or self.closing or self.run(fn, label))

    def _wait_window(self, make, rows):
        """작업 스레드에서 부른다: 창을 띄우고 닫힐 때까지 기다려 그 결과를 돌려준다."""
        result, done = [], threading.Event()
        self.later(lambda: make(self, rows, lambda r: (result.append(r), done.set())))
        done.wait()
        return result[0]

    def review_titles(self, rows):
        """{파일: {'제목': …, '썸네일문구': …}} 또는 None(취소)."""
        return self._wait_window(TitleReview, rows)

    def review_replies(self, drafts):
        """답글 초안 확인: [(초안, 달 글)] 또는 None(취소)."""
        return self._wait_window(ReplyReview, drafts)

    def reply_tick(self):
        """창이 켜져 있으면 1시간마다: '자동으로 달기'로 켠 채널의 새 댓글에 답글 (다른 작업 중이면 다음에)."""
        if self.closing:
            return
        if not self.busy and any(c.get("reply") == "자동으로 달기" for c in core.load_config()["channels"]):
            self.run(lambda app: self.steps.reply_comments(app, auto_only=True), "댓글 답글")
        self.root.after(60 * 60 * 1000, self.reply_tick)

    def review_thumbs(self, rows):
        """만든 썸네일 확인. 진행이면 True."""
        return self._wait_window(ThumbReview, rows)

    def thumb_test(self):
        def work(app):
            image, report = self.steps.thumb_test(app)
            app.later(lambda: [open_path(p) for p in (report, image) if p])
        self.run(work, "썸네일 시험")

    def auto_run(self):
        def work(app):
            if self.steps.auto_all(app):
                app.later(app.pin_window)
        self.run(work, "2. 자동 처리")

    def refresh_status(self):
        try:
            self.rows, cfg = core.read_rows(), core.load_config()
            text = self.steps.status_summary(self.rows, cfg)
            has_videos = core.VIDEO_DIR.exists() and any(p.is_file() for p in core.VIDEO_DIR.rglob("*"))  # 메모장만 넣어도 됨
            infos, nxt = self.steps.card_status(self.rows, has_videos)
            names = ["전체 채널"] + [ch["name"] for ch in cfg["channels"]]
        except Exception as e:
            self.rows, text, infos, nxt, names = [], f"엑셀을 읽지 못했습니다: {e}", {}, None, ["전체 채널"]
        self.summary.config(text=text.replace("\n", "   "))
        self.filter_box.config(values=names)
        if self.channel_filter.get() not in names:
            self.channel_filter.set("전체 채널")
        for key, (card, badge, name, info, stack) in self.cards.items():
            bg = COLORS["next"] if key == nxt else COLORS["card"]
            for w in (card, badge, name, info, stack):
                w.config(bg=bg)
            badge.config(fg=COLORS["accent"] if key == nxt else COLORS["muted"])
            info.config(text=("▶ 지금 할 차례  ·  " if key == nxt else "") + infos.get(key, ""))
        self.fill_table()

    def fill_table(self):
        self.table.delete(*self.table.get_children())
        channel = self.channel_filter.get()
        shown = [i for i, r in enumerate(self.rows) if channel in ("", "전체 채널", r["채널"])
                 and (not self.only_problems.get() or r["오류"])]
        for i in shown:
            r = self.rows[i]
            tag = ("bad" if r["오류"] else "member" if r["상태"] == core.MEMBER_TODO
                   else "done" if r["상태"] in core.DONE_STATES else "work" if r["상태"] else "")
            self.table.insert("", "end", iid=str(i), tags=(tag,), values=(
                r["채널"], Path(r["파일"]).name, r["제목"], r["예약일시"], r["상태"] or "대기", r["오류"]))
        self.count.config(text=f"{len(shown)}개" + (f" / 전체 {len(self.rows)}개" if len(shown) != len(self.rows) else ""))

    def selected_row(self):
        sel = self.table.selection()
        return (int(sel[0]), self.rows[int(sel[0])]) if sel else (None, None)

    def edit_selected(self):
        index, row = self.selected_row()
        if row is None:
            return
        if self.busy:
            return messagebox.showinfo("잠시만요", "작업이 끝난 뒤에 고쳐주세요.")
        Editor(self, index)

    def row_menu(self, event):
        iid = self.table.identify_row(event.y)
        if not iid:
            return
        self.table.selection_set(iid)
        index, r = self.selected_row()
        menu = tk.Menu(self.root, tearoff=0)
        menu.add_command(label="제목·예약일시 고치기…", command=self.edit_selected)
        vid = r["영상ID"]
        menu.add_command(label="Studio에서 열기", state="normal" if vid else "disabled",
                         command=lambda: self.steps.open_tabs([f"https://studio.youtube.com/video/{vid}/edit"]))
        menu.add_command(label="YouTube에서 보기", state="normal" if vid else "disabled",
                         command=lambda: self.steps.open_tabs([f"https://youtu.be/{vid}"]))
        menu.add_command(label="파일 있는 폴더 열기", command=lambda: self.open_dir((core.VIDEO_DIR / r["파일"]).parent))
        menu.add_separator()
        no_thumb = r["썸네일"] == "없음"
        menu.add_command(label="썸네일 다시 찾기" if no_thumb else "썸네일 없이 올리기",
                         state="normal" if r["상태"] == "" else "disabled",
                         command=lambda: self.change_row(index, {"썸네일": "" if no_thumb else "없음"}))
        # 정보완료(댓글 전)·대기 중 오류만 처음부터: 원본 틀 백업에서 다시 합치므로 제목이 두 번 안 들어감
        can_redo = r["상태"] == "정보완료" or (r["상태"] == "" and r["오류"])
        menu.add_command(label="정보 넣기를 처음부터 다시", state="normal" if can_redo else "disabled",
                         command=lambda: self.change_row(index, {"상태": "", "오류": ""}))
        menu.tk_popup(event.x_root, event.y_root)

    def change_row(self, index, changes):
        if self.busy:
            return messagebox.showinfo("잠시만요", "작업이 끝난 뒤에 해주세요.")
        try:
            rows = core.read_rows()
            target = next(r for r in rows if r["파일"] == self.rows[index]["파일"])
            target.update(changes)
            core.write_rows(rows)
        except core.ExcelLocked:
            return messagebox.showwarning("엑셀", "엑셀이 열려 있어서 저장하지 못했습니다. 엑셀을 닫고 다시 해주세요.")
        self.refresh_status()
        self.table.selection_set(str(index))

    def first_run_guide(self):
        import yt
        cfg, todo = core.load_config(), []
        if not yt.secret_file().exists():
            todo.append("• '안내' → 구글 연동 안내대로 열쇠 파일(client_secret_….json)을 _프로그램 폴더에 넣기")
        if not cfg["channels"]:
            todo.append("• '설정' → 채널 추가 → 브라우저에서 그 채널로 로그인 (이름·공개 시간은 자동으로 채워짐)")
        missing = [ch["name"] for ch in cfg["channels"] if not ch["channel_id"]]
        if missing:
            todo.append(f"• 로그인이 필요한 채널: {', '.join(missing)} ('설정'에서 채널 선택 → 로그인)")
        self.log("처음 준비할 것:\n  " + "\n  ".join(todo) if todo else
                 "순서: 1. 영상 준비 → 2. 자동 처리 → 3. 댓글 고정 → 4. 예약 걸기 (노란 카드가 지금 할 차례)")

    def close(self):
        if self.busy and not messagebox.askyesno("닫기", "작업이 진행 중입니다. 정말 닫을까요?\n"
                                                       "(지금 처리 중인 영상은 다음에 같은 버튼을 누르면 이어서 합니다)"):
            return
        self.root.destroy()

    # --- 버튼 ---
    def make_batch(self):
        cfg = core.load_config()
        if not cfg["channels"]:
            return messagebox.showinfo("새 묶음", "설정에서 채널을 먼저 추가해주세요.")
        batch = core.new_batch(cfg)
        self.log(f"새 묶음 폴더를 만들었습니다: 영상/{batch.name}\n"
                 "채널 폴더마다 SEO 메모장·썸네일을 넣고, 영상은 Studio에 올리세요 (영상 파일은 폴더에 안 넣어도 됩니다).")
        open_path(batch)

    def pick_channel(self, title, channels):
        """채널이 하나면 그대로, 여럿이면 버튼으로 고르게 한다. 고르지 않으면 None."""
        if len(channels) <= 1:
            return channels[0] if channels else None
        chosen, win = [], tk.Toplevel(self.root)
        win.title(title)
        win.transient(self.root)
        win.grab_set()
        ttk.Label(win, text=title, padding=12).pack()
        for ch in channels:
            ttk.Button(win, text=ch["name"], command=lambda c=ch: (chosen.append(c), win.destroy())).pack(
                fill="x", padx=12, pady=2)
        ttk.Button(win, text="취소", command=win.destroy).pack(pady=10)
        fit(win)
        self.root.wait_window(win)
        return chosen[0] if chosen else None

    def enable_drop(self):
        """파일·폴더를 창에 끌어다 놓으면 넣는다 (메모장 → SEO 넣기, 그림 → 사진·썸네일 넣기). 부품이 없으면 버튼만."""
        try:  # 창 전체를 받는 곳으로 (main에서 끌어다 놓기용 창으로 만든 경우에만)
            from tkinterdnd2 import DND_FILES
            self.root.drop_target_register(DND_FILES)
            self.root.dnd_bind("<<Drop>>", lambda e: (self.handle_drop(self.root.tk.splitlist(e.data)), e.action)[1])
            self.drop_hint.config(text="※ SEO 메모장·사진 파일을 이 창에 끌어다 놓아도 됩니다 (여러 개 한 번에)")
        except Exception:
            self.drop_hint.config(text="")

    def handle_drop(self, paths):
        """끌어다 놓은 파일·폴더를 채널·순번별로 알아서 나눠 넣는다 (SEO 폴더·썸네일 폴더째 놓아도 됨)."""
        if self.busy:
            return messagebox.showinfo("잠시만요", "작업이 끝난 뒤에 넣어주세요.")
        cfg = core.load_config()
        r = self.steps.route(paths, cfg["channels"])
        if not (r["seo"] or r["pics"] or r["unknown_seo"] or r["unknown_pics"]):
            files = [f for p in map(Path, paths) for f in ([p] if p.is_file() else p.rglob("*"))]
            return messagebox.showinfo("넣기", "SEO 메모장(.txt)이나 그림(.jpg·.png)을 놓아주세요."
                                       + ("\n영상은 Studio에 올리면 됩니다." if any(f.suffix.lower() in core.VIDEO_EXT for f in files) else ""))
        names = sorted({n for n in (self.steps.guess_channel_name(f) for f in r["unknown_seo"]
                                    if re.search(r"seo$", f.stem.strip(), re.I))
                        if re.search(r"[가-힣]{2}", n)})  # '○○seo.txt'처럼 한글 채널 이름이 분명한 것만 (b2d097bf 같은 건 아님)
        if names and messagebox.askyesno("새 채널", f"설정에 없는 채널의 SEO가 {len(names)}개 있어요:\n" + ", ".join(names)
                                         + "\n\n이름만 먼저 추가할까요? (로그인·공개 시간은 나중에 설정에서)"):
            for name in names:
                if name not in {c["name"] for c in cfg["channels"]}:
                    cfg["channels"].append(core.new_channel(name) | {"thumb_auto": True, "_new": True})
            core.save_config(cfg)
            self.log(f"새 채널을 추가했습니다: {', '.join(names)} — 설정에서 채널마다 로그인해 주세요.")
            r = self.steps.route(paths, cfg["channels"])
        leftovers = r["unknown_seo"]
        numbered = [f for f in r["unknown_pics"] if self.steps.drop_number(f) is not None]
        if (leftovers or numbered) and cfg["channels"]:  # 이름에 채널이 없는 것은 한 번 물어서
            ch = self.pick_channel(f"메모장 {len(leftovers)}개 · 그림 {len(numbered)}장 — 어느 채널 것인가요?", cfg["channels"])
            if ch:
                if leftovers:
                    r["seo"].setdefault(ch["name"], []).extend(leftovers)
                for f in numbered:
                    r["pics"].setdefault(ch["name"], {}).setdefault(self.steps.drop_number(f), []).append(f)
                r["unknown_pics"] = [f for f in r["unknown_pics"] if f not in numbered]
        chans = {c["name"]: c for c in cfg["channels"]}
        for name, memos in r["seo"].items():
            if memos:
                self.import_seo(memos, chans[name], open_prep=False)
        off = [n for n in r["pics"] if not chans[n]["thumb_auto"]]
        if off and messagebox.askyesno("썸네일 자동 만들기", "이 채널은 '썸네일 자동 만들기'가 꺼져 있어요:\n" + ", ".join(off)
                                       + "\n\n넣은 그림으로 썸네일을 만들도록 켤까요? ('아니요'면 완성 썸네일로 넣음)"):
            for n in off:
                chans[n]["thumb_auto"] = True
            core.save_config(cfg)
        for name, by_num in r["pics"].items():
            ch = chans[name]
            if not ch["thumb_auto"]:
                self.add_thumbs([f for fs in by_num.values() for f in fs], ch)
                continue
            dirs = core.channel_dirs(ch)
            folder = dirs[-1] if dirs else core.seo_folder(ch)
            for num, fs in by_num.items():
                self.steps.add_candidates(folder, num, fs)
            self.log(f"[{name}] 후보 그림 {sum(len(v) for v in by_num.values())}장을 {len(by_num)}개 영상에 나눠 넣었습니다.")
        self.refresh_status()
        if r["seo"] or r["pics"]:
            self.open_prep(again=True)
        if r["unknown_pics"]:
            messagebox.showinfo("넣기", "어느 채널·몇 번 영상 것인지 모르는 그림이 있어요 (이름에 채널·번호가 없음).\n"
                                "준비 화면의 영상 칸 위에 끌어다 놓아주세요:\n" + "\n".join(f.name for f in r["unknown_pics"][:20]))

    def import_seo(self, paths=None, ch=None, open_prep=True):
        cfg = core.load_config()
        if not cfg["channels"]:
            return messagebox.showinfo("SEO 넣기", "설정에서 채널을 먼저 추가해주세요.")
        paths = paths or filedialog.askopenfilenames(title="SEO 메모장 고르기 (여러 개 가능)",
                                                     filetypes=[("메모장", "*.txt"), ("모든 파일", "*.*")])
        ch = ch or (paths and self.pick_channel("어느 채널의 SEO인가요?", cfg["channels"]))
        if not ch:
            return
        try:
            folder, sheet = core.import_seo(ch, paths)
        except (ValueError, OSError) as e:
            return messagebox.showerror("SEO 넣기", str(e))
        self.log(f"[{ch['name']}] SEO를 넣었습니다: 영상/{core.rel(folder)}\n"
                 "준비 화면에서 영상마다 후보 그림을 넣고 골라 제목·썸네일 문구를 고치세요. 영상은 Studio에 올리면 됩니다.")
        self.refresh_status()
        if open_prep:
            self.open_prep(again=True)

    def _working_channel(self, title):
        cfg = core.load_config()
        channels = [ch for ch in cfg["channels"] if core.channel_dirs(ch)]
        if not channels:
            messagebox.showinfo(title, "먼저 'SEO 넣기'를 해주세요.")
            return None, None
        ch = self.pick_channel(f"{title}: 어느 채널인가요?", channels)
        return ch, ch and core.channel_dirs(ch)[-1]

    def open_sheet(self):
        ch, folder = self._working_channel("작업표 열기")
        if folder:
            sheets = sorted(folder.glob("작업표*.txt"))
            if not sheets:
                return messagebox.showinfo("작업표 열기", "이 묶음에는 작업표가 없습니다. 'SEO 넣기'로 SEO를 넣으면 만들어집니다.")
            open_path(sheets[0])

    def add_thumbs(self, paths=None, ch=None):
        if ch:
            dirs = core.channel_dirs(ch)
            folder = dirs[-1] if dirs else core.seo_folder(ch)  # 아직 SEO 전이면 오늘 날짜 묶음에
        else:
            ch, folder = self._working_channel("사진·썸네일 넣기")
        if not folder:
            return
        what = "재료 사진" if ch["thumb_auto"] else "썸네일"
        if ch["thumb_auto"]:  # 썸네일은 프로그램이 만든다: 깔 사진은 재료 폴더로 (썸네일로 짝짓지 않게)
            folder = folder / core.MATERIAL
            folder.mkdir(exist_ok=True)
        paths = paths or filedialog.askopenfilenames(title=f"[{ch['name']}] {what} 고르기 (여러 개 가능)",
                                                     filetypes=[("그림", "*.png *.jpg *.jpeg"), ("모든 파일", "*.*")])
        if not paths:
            return
        warn = []
        for p in map(Path, paths):
            if not core._nums(p):
                warn.append(f"{p.name}: 이름에 순번 숫자가 없어 어느 영상 것인지 모를 수 있음")
            if not ch["thumb_auto"] and (bad := core.thumb_problem(p)):
                warn.append(f"{p.name}: {bad}")
            shutil.copy2(p, folder / p.name)
        self.log(f"[{ch['name']}] {what} {len(paths)}개를 넣었습니다: 영상/{core.rel(folder)}"
                 + (" — 썸네일은 '2. 자동 처리'에서 문구를 넣어 만듭니다." if ch["thumb_auto"] else ""))
        if warn:
            messagebox.showwarning(what, "\n".join(warn))
        self.refresh_status()

    def open_excel(self):
        if not core.EXCEL.exists():
            return messagebox.showinfo("엑셀", "아직 엑셀이 없습니다. '2. 자동 처리'를 먼저 눌러주세요.")
        self.log("엑셀: 노란 칸만 고치고 저장한 뒤 꼭 닫아주세요 (열려 있으면 작업 결과를 저장하지 못합니다).")
        open_path(core.EXCEL)

    def open_dir(self, path):
        path.mkdir(parents=True, exist_ok=True)
        open_path(path)

    def open_help(self):
        open_path(core.PROG / "사용법.md")
        import yt
        if not yt.secret_file().exists():
            open_path(core.PROG / "구글연동_안내.md")

    def update(self, data=None):
        """새 버전을 받아 바꾸고 프로그램을 다시 켠다. 열쇠·로그인·설정·영상·엑셀은 그대로."""
        def work(app):
            import update
            n = update.apply(data or update.fetch())
            if not n:
                return app.log("업데이트: 이미 최신입니다.")
            app.log(f"업데이트: 파일 {n}개를 새로 바꿨습니다. 프로그램을 다시 켭니다…")
            app.later(app.restart)
        self.run(work, "업데이트")

    def check_update(self):
        """켤 때 새 버전이 있으면 묻지 않고 바로 업데이트하고 다시 켠다 (인터넷이 안 되면 그냥 넘어감).
        이미 작업 버튼을 눌러 진행 중이면 건드리지 않고 다음에 켤 때 한다."""
        def check():
            try:
                import update
                data = update.fetch()
                n = update.apply(data, dry=True)
            except Exception:
                return
            if n:
                self.later(lambda: self.log("새 버전이 있지만 작업 중이라 다음에 켤 때 업데이트합니다.")
                           if self.busy else self.update(data))
        threading.Thread(target=check, daemon=True).start()

    def restart(self):
        self.closing = True  # 창을 닫은 뒤 남은 화면 처리가 돌지 않게
        self.progress.stop()
        subprocess.Popen([sys.executable, str(core.ROOT / "실행.pyw")], cwd=str(core.ROOT))
        self.root.destroy()

    def clean_window(self):
        if self.busy:
            return messagebox.showinfo("청소", "작업이 끝난 뒤에 해주세요.")
        groups = core.clean_targets()
        win = tk.Toplevel(self.root)
        win.title("청소")
        ttk.Label(win, padding=10, justify="left", text=(
            "지울 것을 고르세요. 윈도우에서는 휴지통으로 가서 되살릴 수 있습니다.\n"
            "프로그램 파일, 설정·로그인, 열쇠 파일, 썸네일 틀(로고·폰트)은 지우지 않습니다.")).pack(anchor="w")
        checks = {}
        for name, on, risky in core.CLEAN_GROUPS:
            paths = groups[name]
            var = tk.BooleanVar(value=on and bool(paths))
            size = core.size_of(paths)
            label = f"{'⚠ ' if risky else ''}{name} — " + (f"{len(paths)}개, {size / 1024 / 1024:.1f}MB" if paths else "없음")
            row = ttk.Frame(win)
            row.pack(fill="x", padx=14, pady=1)
            cb = ttk.Checkbutton(row, text=label, variable=var)
            cb.pack(side="left")
            if paths:
                ttk.Button(row, text="목록", width=5, command=lambda n=name, ps=paths: messagebox.showinfo(
                    n, "\n".join(str(p.relative_to(core.ROOT)) for p in ps[:40]) + (f"\n… 외 {len(ps) - 40}개" if len(ps) > 40 else ""),
                    parent=win)).pack(side="right")
            else:
                cb.state(["disabled"])
            checks[name] = (var, risky, paths)
        bar = ttk.Frame(win, padding=10)
        bar.pack(fill="x")

        def select_all():
            for var, _, paths in checks.values():
                var.set(bool(paths))

        def go():
            chosen = [(name, risky, paths) for name, (var, risky, paths) in checks.items() if var.get()]
            if not chosen:
                return messagebox.showinfo("청소", "고른 것이 없습니다.", parent=win)
            risky = [name for name, r, _ in chosen if r]
            if risky and not messagebox.askyesno("정말 지울까요?", "아래는 지우면 다시 준비해야 하거나 영상 파일이 사라집니다:\n- "
                                                 + "\n- ".join(risky) + "\n\n(휴지통에서 되살릴 수는 있습니다)", parent=win):
                return
            win.destroy()

            def work(app):
                every = [p for _, _, paths in chosen for p in paths]
                size = core.size_of(every)
                failed = core.trash(every)
                if failed:
                    app.alert("청소: 지우지 못한 것 (다른 프로그램에서 열려 있는지 확인):\n" + "\n".join(str(f) for f in failed))
                else:  # 기록 파일에는 안 남긴다 (방금 지운 기록 폴더가 다시 생기지 않게)
                    msg = (f"청소: {size / 1024 / 1024:.1f}MB를 지웠습니다. "
                           + ", ".join(name.split(" (")[0] for name, _, _ in chosen))
                    toast("롱폼 자동 업로드", msg)
                    app.q.put(("log", msg))
            self.run(work, "청소")
        ttk.Button(bar, text="모두 선택", command=select_all).pack(side="left")
        ttk.Button(bar, text="청소하기", command=go).pack(side="right")
        fit(win)

    def open_prep(self, again=False):
        prep = getattr(self, "prep", None)
        if prep and prep.win.winfo_exists():
            if not again:
                return prep.win.lift()
            prep.win.destroy()  # 새 SEO·그림이 들어왔으면 새로
        PrepWindow(self)

    def open_settings(self):
        if self.settings and self.settings.win.winfo_exists():
            return self.settings.win.lift()  # 설정 창은 하나만
        self.settings = Settings(self)

    def pin_window(self):
        cfg = core.load_config()
        targets = self.steps.pin_targets(core.read_rows(), cfg)
        if not targets:
            return messagebox.showinfo("고정", "고정할 영상이 없습니다. '2. 자동 처리'를 먼저 해주세요.")
        titles = {ch["name"]: ch["channel_title"] for ch in cfg["channels"]}
        win = tk.Toplevel(self.root)
        win.title("고정 탭 열기")
        ttk.Label(win, padding=10, justify="left", text=(
            "열기 전에 브라우저 오른쪽 위 프로필이 그 채널인지 확인하세요 (다른 채널이면 고정 메뉴가 안 보입니다).\n"
            "댓글 옆 ⋮ → 고정. 수창 채널은 광고 적합성 설문 탭도 같이 열립니다. 끝낸 탭은 닫으면 됩니다.")).pack()
        for name, urls in targets.items():
            b = ttk.Button(win, text=f"{name} (YouTube: {titles.get(name, '')}) — 탭 {len(urls)}개 열기")

            def click(b=b, name=name, urls=urls):
                self.steps.open_tabs(urls)
                b.config(text=f"✓ {name} — 탭 {len(urls)}개 열었음")
                b.state(["disabled"])
            b.config(command=click)
            b.pack(fill="x", padx=10, pady=2)
            if len(targets) == 1:
                click()  # 채널이 하나면 바로 연다 (여러 채널은 브라우저 프로필을 바꿔 가며 하나씩)
        ttk.Button(win, text="고정을 다 했으면 → 4. 예약 걸기", command=lambda: (win.destroy(), self.run(
            self.steps.schedule_all))).pack(fill="x", padx=10, pady=10)
        fit(win)


def fit(win):
    """창이 화면 밖으로 나가지 않게: 크기를 화면 안으로 줄이고 가운데에. 들어가면 크기는 그대로 둔다(내용 따라 커지게)."""
    win.update_idletasks()
    sw, sh = win.winfo_screenwidth(), win.winfo_screenheight()
    w, h = win.winfo_reqwidth(), win.winfo_reqheight()
    size = f"{min(w, sw - 40)}x{min(h, sh - 90)}" if w > sw - 40 or h > sh - 90 else ""
    win.geometry(f"{size}+{max(0, (sw - min(w, sw - 40)) // 2)}+{max(0, (sh - min(h, sh - 90)) // 2 - 30)}")


def photo_image(path, width):
    """작은 미리보기 그림 (JPG·PNG). 못 읽으면 None."""
    try:
        from PIL import Image, ImageTk
        img = Image.open(path)
        img.thumbnail((width, width))
        return ImageTk.PhotoImage(img)
    except Exception:
        return None


def scroll_area(win):
    """세로로 스크롤되는 칸. 안쪽 틀을 돌려준다. 칸 크기는 내용에 맞추되 화면을 넘지 않게."""
    box = ttk.Frame(win)
    box.pack(fill="both", expand=True, padx=12)
    canvas = tk.Canvas(box, highlightthickness=0)
    bar = ttk.Scrollbar(box, orient="vertical", command=canvas.yview)
    inner = ttk.Frame(canvas)
    limit = win.winfo_screenwidth() - 100, win.winfo_screenheight() - 260
    inner.bind("<Configure>", lambda e: canvas.configure(
        scrollregion=canvas.bbox("all"), width=min(inner.winfo_reqwidth(), limit[0]),
        height=min(inner.winfo_reqheight(), limit[1])))
    canvas.create_window((0, 0), window=inner, anchor="nw")
    canvas.configure(yscrollcommand=bar.set)
    canvas.pack(side="left", fill="both", expand=True)
    bar.pack(side="right", fill="y")
    win.bind("<MouseWheel>", lambda e: canvas.yview_scroll(int(-e.delta / 120), "units"))
    return inner


def candidates(text):
    return [x.split(". ", 1)[-1] for x in text.splitlines() if x.strip()]


FINDING = set()  # 실제 사진을 찾는 중인 (채널 폴더, 순번): 준비 화면을 다시 열어도 또 찾지 않게


class PrepWindow:
    """1. 준비 화면: 채널별 탭에 SEO 항목(영상)마다 후보 그림 고르기, 제목·썸네일 문구 고치기, 설명·댓글 보기,
    실제 사진(주제마다 1장, 자동)과 최종 썸네일. 고친 것은 바로 채널 폴더/제작.json 에 저장된다."""
    PREVIEW, FINAL = 200, 320

    def __init__(self, app):
        import thumbs
        app.prep, self.app = self, app
        self.cfg = core.load_config()
        win = self.win = tk.Toplevel(app.root)
        room = win.winfo_screenwidth() - 160  # 영상 칸이 화면 폭을 넘지 않게 그림 크기를 맞춘다
        self.FINAL = max(220, min(320, int(room * 0.24)))
        self.PREVIEW = max(110, min(200, int((room - self.FINAL - 170) / 4.3)))
        win.title("준비 — 후보 그림·제목·썸네일 문구")
        style = ttk.Style(win)  # 구역 제목·고른 항목이 눈에 띄게
        style.configure("Card.TLabelframe.Label", font=(FONT, 12, "bold"), foreground="#1F3A5F")
        style.configure("Section.TLabelframe.Label", font=(FONT, 10, "bold"), foreground="#1F3A5F")
        style.configure("Opt.TLabel", font=(FONT, 10))
        style.configure("Picked.TLabel", font=(FONT, 10, "bold"), foreground="#0B4F9C")
        style.configure("Badge.TLabel", font=(FONT, 9, "bold"), foreground="#2E7D32")
        style.configure("Tag.TLabel", font=(FONT, 9), foreground=COLORS["muted"])
        style.configure("Orig.TLabel", font=(FONT, 9, "bold"), foreground="#8A5A00")
        style.configure("Primary.TButton", font=(FONT, 10, "bold"))
        style.configure("Small.TButton", font=(FONT, 8), padding=(2, 0))
        top = ttk.Frame(win, padding=(12, 10, 12, 4))
        top.pack(fill="x")
        ttk.Label(top, justify="left", text=(
            "영상마다 후보 그림을 클릭해 고르고 제목·썸네일 문구(/ 로 줄 나눔)를 고치세요. 고친 것은 바로 저장됩니다.\n"
            "그림은 영상 칸에 끌어다 놓거나 '그림 넣기'. 실제 사진은 주제마다 자동으로 찾아 합성해 보여줍니다.")).pack(side="left")
        ttk.Button(top, text="최종 썸네일 만들기 (전부)", command=self.make_all).pack(side="right")
        self.only = tk.BooleanVar(value=getattr(app, "prep_only", True))
        ttk.Checkbutton(top, text="그림 넣은 영상만 보기", variable=self.only,
                        command=lambda: (setattr(app, "prep_only", self.only.get()), app.open_prep(again=True))).pack(side="right", padx=8)
        self.tabs = ttk.Notebook(win)
        self.tabs.pack(fill="both", expand=True, padx=8, pady=4)
        self.cards, self.images, self.canvases = {}, [], []
        self.pending = set()  # 실제 사진을 찾거나 자리를 추천받는 중인 영상: 끝날 때까지 미리보기 대신 '추천 중…' (한 번만 그리게)
        for ch in self.cfg["channels"]:
            folder, items = self.app.steps.prep_items(ch)
            if not items:
                continue
            total = len(items)
            if self.only.get() and ch["thumb_auto"]:  # 그림을 넣은 영상만 (SEO에 있어도 그림이 없으면 이번에 안 올리는 영상)
                items = [i for i in items if i["후보"]]
            tab = ttk.Frame(self.tabs)
            self.tabs.add(tab, text=f" {ch['name']} ({len(items)}/{total}) " if len(items) != total else f" {ch['name']} ({total}) ")
            if not items:
                ttk.Label(tab, padding=20, text="아직 그림을 넣은 영상이 없습니다. 그림을 끌어다 놓거나, 위의 '그림 넣은 영상만 보기'를 끄세요.").pack()
                continue
            if not ch["thumb_auto"]:
                ttk.Label(tab, foreground=COLORS["muted"], padding=6, text=(
                    "이 채널은 썸네일을 직접 만든 완성본으로 넣습니다 (설정에서 '썸네일 자동 만들기'를 켜면 여기서 후보를 골라 만듭니다).")).pack(anchor="w")
            elif ctr := thumbs.ctr_text(thumbs.ctr_stats(ch)):
                ttk.Label(tab, foreground=COLORS["muted"], padding=6, wraplength=room, text=(
                    "지난 썸네일 클릭률 (좋았던 쪽을 더 자주 고릅니다): " + ctr)).pack(anchor="w")
            inner = scroll_area(tab)
            self.canvases.append(inner.master)
            for i, item in enumerate(items):
                self.card(inner, ch, folder, item, i)
        if not self.cards:
            ttk.Label(win, padding=20, text="아직 SEO가 없습니다. 메인 창에서 'SEO 넣기'를 하거나 SEO 메모장을 끌어다 놓으세요.").pack()
        win.bind("<MouseWheel>", lambda e: self.current_canvas() and self.current_canvas().yview_scroll(int(-e.delta / 120), "units"))
        try:
            from tkinterdnd2 import DND_FILES
            win.drop_target_register(DND_FILES)
            win.dnd_bind("<<Drop>>", lambda e: (self.drop(win.tk.splitlist(e.data), e.x_root, e.y_root), e.action)[1])
        except Exception:
            pass
        sw, sh = win.winfo_screenwidth(), win.winfo_screenheight()  # 준비 화면은 늘 화면 가득 (그림이 늦게 그려져도 화면 밖으로 안 나가게)
        win.geometry(f"{sw - 40}x{sh - 90}+20+10")
        self.find_photos()
        self.recommend()

    def current_canvas(self):
        i = self.tabs.index("current") if self.tabs.tabs() else None
        return self.canvases[i] if i is not None and i < len(self.canvases) else None

    # --- 영상 칸 하나 ---
    def card(self, parent, ch, folder, item, i):
        """영상 한 편 = 한 칸. 왼쪽: ① 후보 그림 ② 제목 ③ 썸네일 문구 (설명·고정 댓글은 접어 둠), 오른쪽: ④ 최종 썸네일."""
        num = item["num"]
        box = ttk.LabelFrame(parent, text=f"  {num}번 영상  ", padding=10, style="Card.TLabelframe")
        box.grid(row=i, column=0, sticky="ew", pady=(10, 4), padx=4)
        c = self.cards[(ch["name"], num)] = {"ch": ch, "folder": folder, "item": item, "box": box}
        left = ttk.Frame(box)
        left.grid(row=0, column=0, sticky="nw")
        side = ttk.LabelFrame(box, text=" ④ 최종 썸네일 ", padding=8, style="Section.TLabelframe")
        side.grid(row=0, column=1, sticky="ne", padx=(14, 0))

        sec = ttk.LabelFrame(left, text=" ① 후보 그림 — 눌러서 고르기 (빨간 테두리 = 고른 것) ", padding=6, style="Section.TLabelframe")
        sec.grid(row=0, column=0, sticky="ew")
        c["pics"] = ttk.Frame(sec)
        c["pics"].pack(anchor="w")
        for row, (key, label, values, pick, orig) in enumerate((
                ("제목", " ② 제목 ", item["titles"], item.get("title_pick", 0), item["orig"]),
                ("썸네일문구", " ③ 썸네일 문구 ", item["thumb_texts"], item.get("thumb_pick", 0), item.get("orig_thumb", ""))), 1):
            sec = ttk.LabelFrame(left, text=label, padding=6, style="Section.TLabelframe")
            sec.grid(row=row, column=0, sticky="ew", pady=(8, 0))
            self.choices(sec, c, key, values, pick, orig).pack(anchor="w")

        more = ttk.Frame(left)
        toggle = ttk.Button(left, text="설명·고정 댓글 보기 ▸")
        toggle.grid(row=3, column=0, sticky="w", pady=(8, 0))
        for k, (key, h) in enumerate((("설명", 6), ("고정댓글", 4))):
            ttk.Label(more, text="설명" if key == "설명" else "고정 댓글", foreground=COLORS["muted"]).grid(row=k, column=0, sticky="nw", padx=(0, 6))
            text = c[key] = scrolledtext.ScrolledText(more, height=h, width=70, wrap="word", font=(FONT, 9))
            text.insert("1.0", item[key])
            text.grid(row=k, column=1, sticky="w", pady=2)
            text.bind("<FocusOut>", lambda e, kk=key: self.save(c, kk))
        toggle.config(command=lambda: (more.grid_remove(), toggle.config(text="설명·고정 댓글 보기 ▸")) if more.winfo_ismapped()
                      else (more.grid(row=4, column=0, sticky="w"), toggle.config(text="설명·고정 댓글 접기 ▾")))

        c["final"] = tk.Label(side, text="최종 썸네일 아직 없음\n\n'최종 만들기'를 누르세요", width=40, height=9, relief="groove",
                              cursor="hand2")
        c["final"].pack()
        c["final"].bind("<Button-1>", lambda e: c["item"]["최종"] and view_image(self.win, c["item"]["최종"], f"{c['item']['num']}번 최종 썸네일"))
        if ch["thumb_auto"]:
            ttk.Button(side, text="최종 만들기 ▶", style="Primary.TButton", command=lambda: self.make(c, force=True)).pack(fill="x", pady=(6, 0))
            ph = ttk.LabelFrame(side, text=" 실제 사진 ", padding=4)
            ph.pack(fill="x", pady=(8, 0))
            c["photo_note"] = ttk.Label(ph, foreground=COLORS["muted"], wraplength=self.FINAL)
            c["photo_note"].pack(anchor="w")
            row = ttk.Frame(ph)
            row.pack(fill="x", pady=(4, 0))
            for text, cmd in (("크게 보기", lambda: self.view_photo(c)), ("위치·크기", lambda: self.place_photo(c)),
                              ("다른 사진 찾기", lambda: self.find_photos([c], force=True))):
                ttk.Button(row, text=text, command=cmd).pack(side="left", padx=(0, 4))
        else:
            c["photo_note"] = ttk.Label(side)
        c["err"] = ttk.Label(side, foreground=COLORS["accent"], wraplength=self.FINAL)
        c["err"].pack(anchor="w", pady=(6, 0))
        c["score"] = ttk.Label(side, justify="left", wraplength=self.FINAL)
        c["score"].pack(anchor="w", pady=(4, 0))
        self.show_pics(c)
        self.show_final(c)

    def choices(self, parent, c, key, values, pick, orig=""):
        """SEO 후보를 한눈에 전부, 그 아래 원본(따로 한 줄), 직접 입력. 눌러서 고르면 바로 저장되고 고른 줄은 진하게."""
        frame = ttk.Frame(parent)
        current = c["item"][key]
        var = c[key] = tk.StringVar(value=current)
        opts = list(values) + ([orig] if orig and orig not in values else [])
        choice = tk.StringVar(value=str(opts.index(current)) if current in opts else "own")
        own = tk.StringVar(value="" if current in opts else current)
        width = max(320, self.win.winfo_screenwidth() - self.FINAL - 600)  # 오른쪽에 고쳐 쓰기·복사 버튼 자리
        limit = None
        if key == "썸네일문구":
            import thumbs
            limit = thumbs.max_lines(thumbs.load_template(c["ch"]))

        def note(text):
            if key == "제목":
                return f"  ({len(text)}자)"
            n = len([s for s in text.split("/") if s.strip()])
            return f"  ({n}줄 — 이 채널은 {limit}줄까지, 직접 입력에서 줄여주세요)" if limit and n > limit else ""

        def pick_value(v):
            var.set(v)
            self.save(c, key)

        rows = {}

        def rewrite(v):  # 이 문구를 직접 입력 칸에 넣고 바로 고치게
            own.set(v)
            typed()
            entry.focus_set()
            entry.icursor("end")
            self.save(c, key)

        def option(n, v, tag, tag_style, at):
            row = ttk.Frame(frame)
            row.grid(row=at, column=0, sticky="w", pady=1)
            ttk.Radiobutton(row, value=str(n), variable=choice, command=lambda: pick_value(v)).pack(side="left", anchor="n")
            if tag:
                ttk.Label(row, text=tag, style=tag_style, width=8).pack(side="left", anchor="n")
            lab = selectable(row, v + note(v), width)  # 마우스로 끌어 고르고 Ctrl+C·오른쪽 클릭 → 복사
            lab.pack(side="left", anchor="w")
            lab.bind("<ButtonRelease-1>", lambda e: None if lab.tag_ranges("sel") else (choice.set(str(n)), pick_value(v)))
            ttk.Button(row, text="고쳐 쓰기", style="Small.TButton", command=lambda: rewrite(v)).pack(side="left", padx=(8, 0), anchor="n")
            rows[str(n)] = lab

        for n, v in enumerate(values):
            option(n, v, "SEO 추천" if n == pick else f"SEO {n + 1}안", "Badge.TLabel" if n == pick else "Tag.TLabel", n)
        at = len(values)
        if len(opts) > len(values):  # 원본은 따로 떨어진 줄로
            ttk.Separator(frame).grid(row=at, column=0, sticky="ew", pady=4)
            option(len(values), orig, "원본", "Orig.TLabel", at + 1)
            at += 2
        row = ttk.Frame(frame)
        row.grid(row=at, column=0, sticky="w", pady=(4, 0))
        ttk.Radiobutton(row, value="own", variable=choice,
                        command=lambda: own.get().strip() and pick_value(own.get().strip())).pack(side="left")
        ttk.Label(row, text="직접 입력", style="Tag.TLabel", width=8).pack(side="left")
        entry = ttk.Entry(row, textvariable=own, width=60 if key == "제목" else 40)
        entry.pack(side="left", padx=4)
        size = ttk.Label(row, foreground=COLORS["muted"], text=note(own.get()) if own.get() else "")
        size.pack(side="left")
        for ev in ("<<Paste>>", "<<Cut>>"):  # 붙여넣기·잘라내기도 바로 반영
            entry.bind(ev, lambda e: self.win.after(10, typed), add="+")

        def typed(e=None):
            choice.set("own")
            var.set(own.get().strip())
            size.config(text=note(own.get().strip()) if own.get().strip() else "")
        entry.bind("<KeyRelease>", typed)
        for ev in ("<FocusOut>", "<Return>"):
            entry.bind(ev, lambda e: own.get().strip() and self.save(c, key))
        if key == "썸네일문구":
            ttk.Label(frame, foreground=COLORS["muted"], text="/ 를 넣으면 거기서 줄이 바뀝니다 (예: 진짜 죽음은 / 등 뒤에 있었다)").grid(
                row=at + 1, column=0, sticky="w", pady=(2, 0))

        def mark(*_):  # 고른 줄은 진하게
            for k, lab in rows.items():
                picked = k == choice.get()
                lab.config(font=(FONT, 10, "bold") if picked else (FONT, 10), foreground="#0B4F9C" if picked else "black")
        choice.trace_add("write", mark)
        mark()
        return frame

    def show_pics(self, c):
        """후보 그림들 (실제 사진이 있으면 합성한 미리보기). 클릭하면 고름."""
        for w in c["pics"].winfo_children():
            w.destroy()
        item = c["item"]
        if c["ch"]["thumb_auto"]:
            if not item["후보"]:
                ttk.Label(c["pics"], foreground=COLORS["muted"], text="후보 그림이 없습니다 — 이 칸에 끌어다 놓거나 '그림 넣기'").grid(
                    row=0, column=0, padx=(0, 8))
            waiting = self.key(c) in self.pending
            for k, path in enumerate(item["후보"]):  # 4개씩 한 줄
                chosen = item["선택"] and Path(path) == Path(item["선택"])
                lab = tk.Label(c["pics"], text="실제 사진 자리\n추천 중…" if waiting else "…", width=20, height=5,
                               highlightthickness=4, cursor="hand2", foreground=COLORS["muted"],
                               highlightbackground=COLORS["accent"] if chosen else COLORS["card"])
                lab.grid(row=k // 4, column=k % 4, padx=2, pady=2)
                lab.bind("<Button-1>", lambda e, p=path: self.choose(c, p))
                if not waiting:
                    self.render(lab, c, path)
            n = len(item["후보"])
            ttk.Button(c["pics"], text="그림 넣기", command=lambda: self.add(c)).grid(
                row=max(n - 1, 0) // 4, column=4 if n else 1, padx=6, sticky="w")
            if item.get("그림요청"):
                ttk.Button(c["pics"], text="그림 프롬프트 복사", command=lambda: self.copy_prompt(c)).grid(
                    row=max(n - 1, 0) // 4 + 1, column=4 if n else 1, padx=6, sticky="w")
        photo = item["실제사진"]
        note = json.loads((photo.with_suffix(".json")).read_text("utf-8")).get("credit", "") if photo and photo.with_suffix(".json").exists() else ""
        c["photo_note"].config(text=("실제 사진: 찾는 중…" if self.key(c) in self.pending and not photo else
                                     f"실제 사진: {note}" if photo else
                                     "실제 사진: 아직 (API 키가 있으면 자동으로 찾음)" if c["ch"]["thumb_auto"] else ""))

    @staticmethod
    def key(c):
        return str(c["folder"]), c["item"]["num"]

    def wait_for(self, cards, job, label):
        """cards를 '추천 중…'으로 두고 job을 돌린 뒤, 끝나면 한 번만 다시 그린다."""
        for c in cards:
            self.pending.add(self.key(c))
            self.show_pics(c)

        def run(c):
            try:
                job(c)
            finally:
                self.pending.discard(self.key(c))
        self._background(cards, run, label)

    def copy_prompt(self, c):
        """SEO의 썸네일 그림 프롬프트 + 글자·채널 마크 자리를 비워 두라는 구도 안내 → 클립보드 (Gemini 등에 붙여넣기)."""
        self.win.clipboard_clear()
        self.win.clipboard_append(c["item"]["그림요청"].rstrip(" .") + ". " + self.app.steps.PROMPT_TIP)
        self.app.log(f"[{c['ch']['name']}] {c['item']['num']}번 그림 프롬프트를 복사했습니다 — Gemini 등에 붙여넣으세요.")

    def render(self, label, c, path):
        """미리보기를 작업 스레드에서 만들고 화면에는 메인 스레드에서 붙인다."""
        def work():
            try:
                import thumbs
                from PIL import Image
                photo = None if c["item"].get("사진빼기") else c["item"]["실제사진"]
                img = thumbs.compose(c["ch"], path, "", {"side": "left", "photo_box": c["item"].get("사진자리")}, photo) if photo \
                    else Image.open(path).convert("RGB")
                img.thumbnail((self.PREVIEW, self.PREVIEW))
            except Exception as e:
                img = str(e)

            def put():
                if not label.winfo_exists():
                    return
                if isinstance(img, str):
                    return label.config(text=img[:40])
                from PIL import ImageTk
                tkimg = ImageTk.PhotoImage(img)
                self.images.append(tkimg)
                label.config(image=tkimg, text="", width=0, height=0)
            self.app.later(put)
        threading.Thread(target=work, daemon=True).start()

    def show_final(self, c):
        final = c["item"]["최종"]
        img = photo_image(final, self.FINAL) if final else None
        if img:
            self.images.append(img)
        c["final"].config(image=img or "", text="" if img else "최종 썸네일 아직 없음",
                          width=0 if img else 40, height=0 if img else 9)
        import thumbs
        checks, points, review = thumbs.report(final) if final else ([], None, None)
        lines = []
        if review:
            lines.append(f"Claude 점수 {review.get('total', 0)}/100" if review.get("policy_ok", True)
                         else f"⛔ 정책 위험: {review.get('policy_reason', '')}")
            lines += [f"· {tip}" for tip in review.get("tips", [])]
            if review.get("title_score") is not None:
                lines.append(f"제목 점수 {review['title_score']}/100")
                lines += [f"· {tip}" for tip in review.get("title_tips", [])]
        if points is not None:
            lines.append(f"자동 점검 {points}/100" + "".join(f"\n⚠ {m}" for ok, m in checks if not ok))
        c["score"].config(text="\n".join(lines), foreground=COLORS["accent"] if review and not review.get("policy_ok", True) else "")

    def view_photo(self, c):
        photo = c["item"]["실제사진"]
        if not photo:
            return messagebox.showinfo("실제 사진", "아직 찾은 실제 사진이 없습니다 (Claude API 키가 있으면 자동으로 찾음).", parent=self.win)
        note = photo.with_suffix(".json")
        credit = json.loads(note.read_text("utf-8")).get("credit", "") if note.exists() else ""
        view_image(self.win, photo, f"{c['item']['num']}번 실제 사진 — {credit or photo.name}",
                   f"위키미디어 공용 (저작권이 풀린 사진): {credit}" if credit else "")

    def place_photo(self, c):
        if not (c["item"]["선택"] and c["item"]["실제사진"]):
            return messagebox.showinfo("사진 위치", "후보 그림과 실제 사진이 있어야 위치를 정할 수 있습니다.", parent=self.win)
        PhotoPlacer(self, c)

    def refresh(self, c, draw=True):
        _, items = self.app.steps.prep_items(c["ch"])
        c["item"] = next((i for i in items if i["num"] == c["item"]["num"]), c["item"])
        if draw:
            self.show_pics(c)
            self.show_final(c)

    # --- 고르기·저장·넣기 ---
    def choose(self, c, path):
        self.app.steps.remember(c["folder"], c["item"]["num"], 선택=str(Path(path).relative_to(c["folder"])))
        self.refresh(c, draw=False)
        item = c["item"]
        if c["ch"]["thumb_auto"] and item["실제사진"] and not item["사진빼기"] and (not item["사진자리"] or self.auto_box(c)):
            self.recommend([c], again=True)  # 배경이 바뀌었으니 추천 자리도 새로 (끝나면 한 번에 그림)
        else:  # 직접 옮긴 자리는 그대로
            self.show_pics(c)
            self.show_final(c)

    def auto_box(self, c):
        return core.load_prep(c["folder"]).get(str(c["item"]["num"]), {}).get("사진자리_추천", False)

    def recommend(self, cards=None, again=False):
        """실제 사진 추천 자리를 정해 둔다 (아직 없거나, again이면 프로그램이 정한 자리만 새로). 직접 옮긴 자리는 건드리지 않음."""
        todo = [c for c in (cards or self.cards.values()) if c["ch"]["thumb_auto"] and c["item"]["선택"] and c["item"]["실제사진"]
                and not c["item"]["사진빼기"] and (again or not c["item"]["사진자리"])]
        todo = [c for c in todo if self.key(c) not in self.pending]  # 이미 찾는 중이면 그 일이 끝나며 추천까지 함
        if todo:
            self.wait_for(todo, lambda c: self.recommend_one(c, again), "사진 자리 추천")

    def recommend_one(self, c, again=False):
        import thumbs
        folder, num = c["folder"], c["item"]["num"]
        mine = core.load_prep(folder).get(str(num), {})
        base, photo = core.material_for(folder, num), folder / core.PHOTOS / f"{num}.jpg"
        if not base or not photo.exists() or mine.get("사진빼기"):
            return
        if mine.get("사진자리") and (not mine.get("사진자리_추천") or not again):
            return  # 직접 옮긴 자리, 또는 이미 추천한 자리
        box = thumbs.recommend_box(c["ch"], base, photo, self.cfg.get("claude_key", ""))
        if box:
            self.app.steps.remember(folder, num, 사진자리=box, 사진자리_추천=True)

    def save(self, c, key):
        widget = c[key]
        value = widget.get().strip() if isinstance(widget, tk.StringVar) else widget.get("1.0", "end").strip()
        if value and value != c["item"][key]:
            self.app.steps.remember(c["folder"], c["item"]["num"], **{key: value})
            c["item"][key] = value

    def add(self, c, paths=None):
        paths = paths or filedialog.askopenfilenames(parent=self.win, title=f"{c['item']['num']}번 후보 그림 (여러 개 가능)",
                                                     filetypes=[("그림", "*.png *.jpg *.jpeg"), ("모든 파일", "*.*")])
        pics = [p for p in map(Path, paths or []) if p.suffix.lower() in core.IMAGE_EXT]
        if pics:
            self.app.steps.add_candidates(c["folder"], c["item"]["num"], pics)
            self.refresh(c)

    def drop(self, paths, x, y):
        """영상 칸 위에 놓으면 그 영상의 후보, 다른 곳이면 파일·폴더 이름의 번호로 나눈다."""
        files = []
        for p in map(Path, paths):
            files += sorted(f for f in p.rglob("*") if f.is_file()) if p.is_dir() else [p]
        pics = [f for f in files if f.suffix.lower() in core.IMAGE_EXT]
        w = self.win.winfo_containing(x, y)
        while w is not None and not any(c["box"] is w for c in self.cards.values()):
            w = w.master
        target = next((c for c in self.cards.values() if c["box"] is w), None)
        if target:
            return self.add(target, pics)
        tab = self.current_channel()
        unknown = []
        for f in pics:
            c = self.cards.get((tab, self.app.steps.drop_number(f)))
            (c and self.add(c, [f])) if c else unknown.append(f.name)
        if unknown:
            messagebox.showinfo("그림 넣기", "어느 영상 것인지 모르는 그림이 있어요. 영상 칸 위에 끌어다 놓아주세요:\n"
                                + "\n".join(unknown[:20]), parent=self.win)

    def current_channel(self):
        i = self.tabs.index("current") if self.tabs.tabs() else None
        names = [ch["name"] for ch in self.cfg["channels"] if any(k[0] == ch["name"] for k in self.cards)]
        return names[i] if i is not None and i < len(names) else None

    # --- 실제 사진·최종 썸네일 (작업 스레드) ---
    def _background(self, cards, job, label):
        def work():
            from concurrent.futures import ThreadPoolExecutor

            def one(c):
                err = ""
                try:
                    job(c)
                except Exception as e:
                    err = f"✗ {label}: {e}"
                    self.app.log(f"  ✗ [{c['ch']['name']}] {c['item']['num']}번 {label}: {e}")
                self.app.later(lambda: self.win.winfo_exists() and (self.refresh(c), c["err"].config(text=err)))
            with ThreadPoolExecutor(4) as pool:
                list(pool.map(one, cards))
            self.app.log(f"{label}: 끝났습니다.")
        threading.Thread(target=work, daemon=True).start()

    def find_photos(self, cards=None, force=False):
        import thumbs
        key = self.cfg.get("claude_key", "")
        if not (key or os.environ.get("ANTHROPIC_API_KEY")):
            return
        # 후보 그림을 넣은 영상만 (아직 그림이 없는 영상까지 찾으면 Claude 비용만 듦), 이미 찾는 중인 것은 빼고
        todo = [c for c in (cards or self.cards.values()) if c["ch"]["thumb_auto"] and c["item"]["선택"]
                and (force or not c["item"]["실제사진"]) and (c["folder"], c["item"]["num"]) not in FINDING]
        todo = [c for c in todo if force or not (c["folder"] / core.PHOTOS / f"{c['item']['num']}.json").exists()]
        if todo:
            FINDING.update((c["folder"], c["item"]["num"]) for c in todo)
            self.app.log(f"실제 사진을 찾습니다: {len(todo)}개 주제")
            def find(c):
                try:
                    thumbs.fetch_photo(c["folder"], c["item"]["num"], c["item"]["제목"], c["item"]["설명"],
                                       c["item"]["썸네일문구"], key, self.app.log, force)
                    self.recommend_one(c, again=force)  # 찾은 사진의 추천 자리
                finally:
                    FINDING.discard((c["folder"], c["item"]["num"]))
            self.wait_for(todo, find, "실제 사진 찾기")

    def make(self, c, force=False):
        self.make_all([c], force)

    def make_all(self, cards=None, force=False):
        import thumbs
        for c in self.cards.values():  # 아직 칸에서 안 빠져나온 고친 값도 저장
            for k in ("제목", "썸네일문구"):
                self.save(c, k)
        todo = [c for c in (cards or self.cards.values()) if c["ch"]["thumb_auto"] and c["item"]["선택"]]
        if not todo:
            return messagebox.showinfo("최종 썸네일", "후보 그림이 있는 영상이 없습니다.", parent=self.win)
        key = self.cfg.get("claude_key", "")
        self.app.log(f"최종 썸네일을 만듭니다: {len(todo)}개")
        self._background(todo, lambda c: thumbs.make_for(
            c["ch"], c["folder"], c["item"]["num"], c["item"]["썸네일문구"], c["item"]["제목"], c["item"]["설명"],
            key, self.app.log, force), "최종 썸네일")


def selectable(parent, text, width_px, font=None):
    """보기만 하는 글자인데 마우스로 끌어 고르고 복사할 수 있는 칸 (테두리 없이 글자처럼 보임, 자동 줄바꿈)."""
    font = font or (FONT, 10)
    f = tkfont.Font(font=font)
    chars = max(10, width_px // max(1, f.measure("0")))
    w = tk.Text(parent, wrap="word", font=font, relief="flat", borderwidth=0, highlightthickness=0, cursor="xterm",
                width=chars, height=1, padx=0, pady=1,
                background=ttk.Style().lookup("TFrame", "background") or parent.winfo_toplevel().cget("background"))
    w.insert("1.0", text)
    w.config(state="disabled")  # 고칠 수는 없고 고르기·복사만
    px = chars * f.measure("0")  # 실제 칸 너비
    bold = tkfont.Font(font=(font[0], font[1], "bold")).measure(text)  # 고르면 굵게 바뀌니 굵은 글자 기준
    w.config(height=max(1, -(-bold // max(1, px))))  # 줄바꿈될 만큼 높이
    return w


def view_image(parent, path, title, note=""):
    """그림을 화면에 맞게 크게 보기."""
    win = tk.Toplevel(parent)
    win.title(title)
    w, h = int(win.winfo_screenwidth() * 0.8), int(win.winfo_screenheight() * 0.75)
    img = photo_image(path, w)
    if img and img.height() > h:
        img = photo_image(path, int(w * h / img.height()))
    label = tk.Label(win, image=img or "", text="" if img else "그림을 못 읽음")
    label.image = img
    label.pack(padx=8, pady=8)
    if note:
        ttk.Label(win, text=note, foreground=COLORS["muted"]).pack(pady=(0, 8))
    ttk.Button(win, text="닫기", command=win.destroy).pack(pady=(0, 10))
    win.bind("<Escape>", lambda e: win.destroy())
    fit(win)


class PhotoPlacer:
    """실제 사진 위치·크기 직접 정하기: 끌어서 옮기고, 휠(또는 +/−)로 크기. 저장하면 미리보기·최종 썸네일·자동 처리에 그대로."""
    W, H = 640, 360  # 1280x720의 절반으로 보여줌

    def __init__(self, prep, c):
        import thumbs
        from PIL import Image
        self.prep, self.c = prep, c
        item, ch = c["item"], c["ch"]
        win = self.win = tk.Toplevel(prep.win)
        win.title(f"{item['num']}번 실제 사진 위치·크기")
        win.transient(prep.win)
        win.grab_set()
        ttk.Label(win, padding=(12, 10, 12, 4), justify="left", text=(
            "사진을 끌어서 옮기고, 마우스 휠(또는 + / −)로 크기를 바꾸세요. 얼굴을 가리지 않는 곳으로.\n"
            "글자 자리는 최종 썸네일에서 정해지니, 최종이 있으면 그 글자도 함께 보여줍니다.")).pack(anchor="w")
        plan = {"side": "left"}
        text = ""
        final = item["최종"]
        if final and final.with_suffix(".json").exists():  # 최종이 있으면 그 글자 자리·글씨체 그대로 깔아서 보여줌
            plan = json.loads(final.with_suffix(".json").read_text("utf-8")).get("plan") or plan
            text = item["썸네일문구"]
        plan = {k: v for k, v in plan.items() if k != "photo_box"}
        try:
            bg = thumbs.compose(ch, item["선택"], text, plan, None, random.Random(0))
        except Exception:
            bg = thumbs.compose(ch, item["선택"], "", {"side": "left"}, None, random.Random(0))
        self.src = Image.open(item["실제사진"]).convert("RGB")
        self.ratio = min(1.9, max(0.75, self.src.width / self.src.height))  # 프로그램과 같은 비율 (너무 길쭉하면 조금 자름)
        box = item.get("사진자리") or thumbs.auto_box(ch, item["선택"], item["실제사진"]) or [700, 20, 540, 540 / self.ratio]
        self.w = float(box[2])
        self.x, self.y = float(box[0]), float(box[1])
        from PIL import ImageTk
        self.bg = ImageTk.PhotoImage(bg.resize((self.W, self.H)))
        self.canvas = tk.Canvas(win, width=self.W, height=self.H, highlightthickness=0)
        self.canvas.pack(padx=12)
        self.canvas.create_image(0, 0, image=self.bg, anchor="nw")
        self.item = self.canvas.create_image(0, 0, anchor="nw")
        self.frame = self.canvas.create_rectangle(0, 0, 0, 0, outline="#FFEB00", width=2)
        self.canvas.bind("<ButtonPress-1>", self.grab)
        self.canvas.bind("<B1-Motion>", self.drag)
        self.canvas.bind("<MouseWheel>", lambda e: self.resize(1.05 if e.delta > 0 else 1 / 1.05))
        self.canvas.bind("<Button-4>", lambda e: self.resize(1.05))  # 리눅스 휠
        self.canvas.bind("<Button-5>", lambda e: self.resize(1 / 1.05))
        bar = ttk.Frame(win, padding=12)
        bar.pack(fill="x")
        ttk.Button(bar, text="−", width=3, command=lambda: self.resize(1 / 1.1)).pack(side="left")
        ttk.Button(bar, text="+", width=3, command=lambda: self.resize(1.1)).pack(side="left", padx=4)
        ttk.Button(bar, text="이 자리로 저장 ▶", command=self.save).pack(side="right")
        ttk.Button(bar, text="취소", command=win.destroy).pack(side="right", padx=6)
        ttk.Button(bar, text="추천 자리로", command=lambda: self.done(사진자리=None, 사진자리_추천=False, 사진빼기=False)).pack(
            side="right", padx=6)
        ttk.Button(bar, text="실제 사진 빼기", command=lambda: self.done(사진자리=None, 사진빼기=True)).pack(side="right")
        self.draw()
        fit(win)

    def box(self):
        return [round(self.x), round(self.y), round(self.w), round(self.w / self.ratio)]

    def draw(self):
        from PIL import ImageTk
        import thumbs
        self.w = min(1280.0, max(160.0, self.w))
        h = self.w / self.ratio
        if h > 720:
            self.w, h = 720 * self.ratio, 720.0
        self.x = min(1280 - self.w, max(0.0, self.x))
        self.y = min(720 - h, max(0.0, self.y))
        s = self.W / 1280
        pic = thumbs._cover(self.src, max(1, round(self.w * s)), max(1, round(h * s)))
        self.pic = ImageTk.PhotoImage(pic)
        self.canvas.itemconfig(self.item, image=self.pic)
        self.canvas.coords(self.item, self.x * s, self.y * s)
        self.canvas.coords(self.frame, self.x * s, self.y * s, (self.x + self.w) * s, (self.y + h) * s)

    def grab(self, e):
        self.start = (e.x, e.y, self.x, self.y)

    def drag(self, e):
        s = 1280 / self.W
        x0, y0, bx, by = self.start
        self.x, self.y = bx + (e.x - x0) * s, by + (e.y - y0) * s
        self.draw()

    def resize(self, k):
        cx, cy = self.x + self.w / 2, self.y + self.w / self.ratio / 2  # 가운데를 기준으로
        self.w *= k
        self.x, self.y = cx - self.w / 2, cy - self.w / self.ratio / 2
        self.draw()

    def save(self):
        self.done(사진자리=self.box(), 사진자리_추천=False, 사진빼기=False)

    def done(self, **fields):
        c = self.c
        self.prep.app.steps.remember(c["folder"], c["item"]["num"], **fields)
        self.win.destroy()
        had_final = bool(c["item"]["최종"])
        self.prep.refresh(c)
        if fields.get("사진자리") is None and not fields.get("사진빼기"):  # 추천 자리로: 다시 추천받고, 최종이 있었으면 그 자리로 다시
            import thumbs
            prep, key = self.prep, self.prep.cfg.get("claude_key", "")

            def job(c):
                prep.recommend_one(c, again=True)
                if had_final:
                    item = c["item"]
                    thumbs.make_for(c["ch"], c["folder"], item["num"], item["썸네일문구"], item["제목"], item["설명"], key, prep.app.log)
            prep.wait_for([c], job, "사진 자리 추천")
        elif had_final:  # 최종이 있었으면 사진만 옮겨 다시 만든다 (글자 디자인은 그대로)
            self.prep.make(c)


class ReplyReview:
    """댓글 답글 초안 확인: 고칠 것은 고치고, 달지 않을 것은 체크를 빼고 '선택한 답글 달기'. 고친 것은 다음 초안이 배운다."""

    def __init__(self, app, drafts, done):
        self.done = done
        win = self.win = tk.Toplevel(app.root)
        win.title("댓글 답글 확인")
        win.transient(app.root)
        win.grab_set()
        win.protocol("WM_DELETE_WINDOW", lambda: self.finish(None))
        ttk.Label(win, padding=(12, 10, 12, 4), text=(
            f"[{drafts[0]['ch']}] 새 댓글 {len(drafts)}개에 대한 답글 초안 — 고칠 것은 고치고, 달지 않을 것은 체크를 빼세요.\n"
            "고친 답글은 기억해 두었다가 다음 초안을 그 말투에 맞춥니다.")).pack(anchor="w")
        inner = scroll_area(win)
        self.items = []
        for i, d in enumerate(drafts):
            box = ttk.Frame(inner, padding=(8, 6))
            box.grid(row=i, column=0, sticky="we")
            on = tk.BooleanVar(value=True)
            ttk.Checkbutton(box, variable=on).grid(row=0, column=0, rowspan=2, sticky="n")
            ttk.Label(box, foreground=COLORS["muted"], wraplength=640, justify="left",
                      text=f"{d['video'][:40]} · {d['comment']['author']}: {d['comment']['text'][:300]}").grid(row=0, column=1, sticky="w")
            text = tk.Text(box, width=80, height=3, wrap="word")
            text.insert("1.0", d["draft"])
            text.grid(row=1, column=1, sticky="we", pady=(2, 0))
            self.items.append((d, on, text))
        buttons = ttk.Frame(win, padding=12)
        buttons.pack(fill="x")
        ttk.Button(buttons, text="선택한 답글 달기 ▶", command=self.send).pack(side="right")
        ttk.Button(buttons, text="취소", command=lambda: self.finish(None)).pack(side="right", padx=6)
        fit(win)

    def send(self):
        self.finish([(d, text.get("1.0", "end").strip()) for d, on, text in self.items
                     if on.get() and text.get("1.0", "end").strip()])

    def finish(self, result):
        self.win.destroy()
        self.done(result)


class TitleReview:
    """2. 자동 처리 중: 새 영상들의 제목(과 썸네일 자동 만들기 채널은 썸네일 문구)을 한 화면에서 고르거나 고친다."""

    def __init__(self, app, rows, done):
        app.title_review, self.rows, self.done = self, rows, done
        auto = {c["name"] for c in core.load_config()["channels"] if c["thumb_auto"]}
        win = self.win = tk.Toplevel(app.root)
        win.title("제목 확인")
        win.transient(app.root)
        win.grab_set()
        win.protocol("WM_DELETE_WINDOW", self.cancel)
        ttk.Label(win, padding=(12, 10, 12, 4), text=(
            f"새 영상 {len(rows)}개 — 제목을 ▾에서 고르거나 직접 고치세요. 처음 값은 SEO 추천입니다."
            + ("\n썸네일 문구는 / 로 줄을 나눕니다. 이 문구로 썸네일을 자동으로 만듭니다." if auto else ""))).pack(anchor="w")
        inner = scroll_area(win)
        self.vars, self.images = {}, []
        for i, r in enumerate(rows):
            is_auto = r["채널"] in auto
            pic = core.material(r) if is_auto else (
                core.VIDEO_DIR / r["썸네일"] if r["썸네일"] not in ("", "없음") else None)
            img = photo_image(pic, 170) if pic else None
            if img:
                self.images.append(img)  # 참조를 남겨야 그림이 사라지지 않는다
            (tk.Label(inner, image=img) if img else ttk.Label(
                inner, width=24, foreground=COLORS["muted"], wraplength=170,
                text=Path(pic).name if pic else ("재료 사진 없음" if is_auto else "썸네일 없음"))).grid(
                row=3 * i, column=0, rowspan=3, sticky="nw", padx=(0, 10), pady=6)
            ttk.Label(inner, foreground=COLORS["muted"], text=f"[{r['채널']}] {Path(r['파일']).name}").grid(
                row=3 * i, column=1, columnspan=3, sticky="sw", pady=(6, 0))
            fields = {"제목": "제목후보"} | ({"썸네일문구": "썸네일문구후보"} if is_auto else {})
            self.vars[r["파일"]] = {}
            for j, (key, cands) in enumerate(fields.items(), 1):
                ttk.Label(inner, text="제목" if key == "제목" else "썸네일 문구", foreground=COLORS["muted"]).grid(
                    row=3 * i + j, column=1, sticky="nw", padx=(0, 6))
                var = self.vars[r["파일"]][key] = tk.StringVar(value=r[key])
                ttk.Combobox(inner, textvariable=var, width=64, values=candidates(r[cands])).grid(
                    row=3 * i + j, column=2, sticky="nw")
                if key == "제목":
                    length = ttk.Label(inner, width=12)
                    length.grid(row=3 * i + j, column=3, sticky="nw", padx=6)
                    var.trace_add("write", lambda *a, v=var, w=length: self.show_length(v, w))
                    self.show_length(var, length)
        buttons = ttk.Frame(win, padding=12)
        buttons.pack(fill="x")
        ttk.Button(buttons, text="이대로 진행 ▶", command=self.ok).pack(side="right")
        ttk.Button(buttons, text="취소", command=self.cancel).pack(side="right", padx=6)
        fit(win)

    @staticmethod
    def show_length(var, label):
        n = len(var.get().strip())
        label.config(text=f"{n}자" + (" ⚠ 길어요" if n > 68 else ""),
                     foreground=COLORS["accent"] if n > 68 else COLORS["muted"])

    def ok(self):
        picked = {f: {k: v.get().strip() for k, v in d.items()} for f, d in self.vars.items()}
        bad = [Path(f).name for f, d in picked.items() if any(not v or "\n" in v for v in d.values())]
        if bad:
            return messagebox.showerror("제목 확인", "제목이나 썸네일 문구가 비어 있습니다:\n" + "\n".join(bad), parent=self.win)
        self.finish(picked)

    def cancel(self):
        self.finish(None)

    def finish(self, result):
        self.win.destroy()
        self.done(result)


class ThumbReview:
    """2. 자동 처리 중: 자동으로 만든 썸네일 확인. 마음에 안 드는 것만 '다시 만들기' (색·쪽·실제 사진이 새로 정해짐)."""

    def __init__(self, app, rows, done):
        app.thumb_review, self.app, self.done = self, app, done
        win = self.win = tk.Toplevel(app.root)
        win.title("썸네일 확인")
        win.transient(app.root)
        win.grab_set()
        win.protocol("WM_DELETE_WINDOW", lambda: self.finish(False))
        ttk.Label(win, padding=(12, 10, 12, 4), text=(
            f"썸네일 {len(rows)}개 — 마음에 안 드는 것만 '다시 만들기'. 문구를 바꾸려면 취소 → '2. 자동 처리'를 다시 누르세요.")).pack(anchor="w")
        inner = scroll_area(win)
        self.images, self.labels, self.busy = {}, {}, set()
        for i, r in enumerate(rows):
            cell = ttk.Frame(inner, padding=6)
            cell.grid(row=i // 2, column=i % 2, sticky="nw")
            self.labels[r["파일"]] = tk.Label(cell)
            self.labels[r["파일"]].pack()
            ttk.Label(cell, foreground=COLORS["muted"], text=f"[{r['채널']}] {Path(r['파일']).name}").pack(side="left")
            button = ttk.Button(cell, text="다시 만들기")
            button.config(command=lambda r=r, b=button: self.remake(r, b))
            button.pack(side="right")
            self.show(r)
        buttons = ttk.Frame(win, padding=12)
        buttons.pack(fill="x")
        ttk.Button(buttons, text="이대로 진행 ▶", command=lambda: self.finish(True)).pack(side="right")
        ttk.Button(buttons, text="취소", command=lambda: self.finish(False)).pack(side="right", padx=6)
        fit(win)

    def show(self, r):
        img = photo_image(core.VIDEO_DIR / r["썸네일"], 448)
        self.images[r["파일"]] = img
        self.labels[r["파일"]].config(image=img or "", text="" if img else "그림을 못 읽음")

    def remake(self, r, button):
        import thumbs
        cfg = core.load_config()
        ch = next(c for c in cfg["channels"] if c["name"] == r["채널"])
        button.state(["disabled"])
        button.config(text="만드는 중…")
        self.busy.add(r["파일"])

        def work():
            try:
                thumbs.make(ch, r, cfg.get("claude_key", ""), self.app.log, force=True)
                err = None
            except Exception as e:
                err = str(e)

            def after():
                self.busy.discard(r["파일"])
                if not self.win.winfo_exists():
                    return
                button.state(["!disabled"])
                button.config(text="다시 만들기")
                if err:
                    messagebox.showerror("다시 만들기", err, parent=self.win)
                else:
                    self.show(r)
            self.app.later(after)
        threading.Thread(target=work, daemon=True).start()

    def finish(self, ok):
        if ok and self.busy:
            return messagebox.showinfo("썸네일 확인", "다시 만드는 중인 썸네일이 있습니다. 끝난 뒤에 눌러주세요.", parent=self.win)
        self.win.destroy()
        self.done(ok)


class Editor:
    """영상 하나의 노란 칸(제목·예약일시·회원우선·썸네일·영상ID)을 엑셀 없이 고치는 창. '저장하고 다음'으로 빠르게 넘김."""

    def __init__(self, app, index):
        self.app, self.index = app, index
        win = self.win = tk.Toplevel(app.root)
        win.title("영상 고치기")
        win.transient(app.root)
        win.grab_set()
        self.vars = {k: tk.StringVar() for k in ("제목", "예약일시", "영상ID")}
        self.member, self.no_thumb = tk.BooleanVar(), tk.BooleanVar()
        f = ttk.Frame(win, padding=14)
        f.pack(fill="both", expand=True)
        self.where = ttk.Label(f, font=(FONT, 10, "bold"))
        self.where.grid(row=0, column=0, columnspan=3, sticky="w")
        self.note = ttk.Label(f, foreground=COLORS["accent"])
        self.note.grid(row=1, column=0, columnspan=3, sticky="w", pady=(0, 6))
        ttk.Label(f, text="제목").grid(row=2, column=0, sticky="w")
        self.title_entry = ttk.Entry(f, textvariable=self.vars["제목"], width=72)
        self.title_entry.grid(row=2, column=1, sticky="we")
        self.length = ttk.Label(f, width=16)
        self.length.grid(row=2, column=2, sticky="w", padx=6)
        self.vars["제목"].trace_add("write", lambda *a: self.update_length())
        ttk.Label(f, text="제목 후보").grid(row=3, column=0, sticky="nw", pady=4)
        self.candidates = ttk.Frame(f)
        self.candidates.grid(row=3, column=1, columnspan=2, sticky="w", pady=4)
        ttk.Label(f, text="예약일시").grid(row=4, column=0, sticky="w")
        ttk.Entry(f, textvariable=self.vars["예약일시"], width=20).grid(row=4, column=1, sticky="w")
        ttk.Label(f, foreground=COLORS["muted"], text="예) 2026-10-21 12:15 · 날짜만 적으면 그 요일 공개 시간 · 비우면 자동").grid(
            row=5, column=1, sticky="w")
        ttk.Checkbutton(f, text="회원 우선 공개 (회원 전용으로 직접 설정할 영상)", variable=self.member).grid(
            row=6, column=1, sticky="w", pady=(6, 0))
        self.thumb_check = ttk.Checkbutton(f, variable=self.no_thumb)
        self.thumb_check.grid(row=7, column=1, sticky="w")
        ttk.Label(f, text="영상ID").grid(row=8, column=0, sticky="w", pady=(6, 0))
        ttk.Entry(f, textvariable=self.vars["영상ID"], width=40).grid(row=8, column=1, sticky="w", pady=(6, 0))
        ttk.Label(f, foreground=COLORS["muted"], text="보통 비워둠. '초안을 못 찾았습니다'면 영상 주소를 통째로 붙여넣기").grid(
            row=9, column=1, sticky="w")
        ttk.Label(f, text="설명").grid(row=10, column=0, sticky="nw", pady=(8, 0))
        self.desc = scrolledtext.ScrolledText(f, height=5, width=72, wrap="word", font=(FONT, 9))
        self.desc.grid(row=10, column=1, columnspan=2, sticky="we", pady=(8, 0))
        ttk.Label(f, text="고정 댓글").grid(row=11, column=0, sticky="nw", pady=(4, 0))
        self.comment = scrolledtext.ScrolledText(f, height=3, width=72, wrap="word", font=(FONT, 9))
        self.comment.grid(row=11, column=1, columnspan=2, sticky="we", pady=(4, 0))
        ttk.Label(f, foreground=COLORS["muted"], text="설명·고정 댓글은 SEO 메모장 그대로 (여기서는 보기만)").grid(
            row=12, column=1, sticky="w")
        bar = ttk.Frame(f)
        bar.grid(row=13, column=0, columnspan=3, sticky="we", pady=(12, 0))
        ttk.Button(bar, text="◀ 이전", command=lambda: self.save_and_move(-1)).pack(side="left")
        ttk.Button(bar, text="저장하고 다음 ▶", command=lambda: self.save_and_move(1)).pack(side="left", padx=6)
        ttk.Button(bar, text="닫기", command=win.destroy).pack(side="right")
        ttk.Button(bar, text="저장", command=lambda: self.save_and_move(0)).pack(side="right", padx=6)
        win.bind("<Return>", lambda e: self.save_and_move(1))
        win.bind("<Escape>", lambda e: win.destroy())
        self.load()
        fit(win)

    def load(self):
        r = self.row = self.app.rows[self.index]
        self.where.config(text=f"[{r['채널']}] {Path(r['파일']).name}   ({self.index + 1}/{len(self.app.rows)})")
        editable = r["상태"] == ""
        self.note.config(text="" if editable else
                         f"이미 YouTube에 넣은 영상입니다 (상태: {r['상태']}). 제목은 바꿔도 반영되지 않습니다.")
        for k, var in self.vars.items():
            var.set(r[k])
        self.member.set(core.is_member(r["회원우선"]))
        self.no_thumb.set(r["썸네일"] == "없음")
        self.thumb_check.config(text="썸네일 없이 올리기" + ("" if r["썸네일"] in ("", "없음")
                                                         else f"  (지금 썸네일: {Path(r['썸네일']).name})"))
        self.title_entry.state(["!disabled"] if editable else ["disabled"])
        for w in self.candidates.winfo_children():
            w.destroy()
        for line in [x for x in r["제목후보"].splitlines() if x.strip()]:
            text = line.split(". ", 1)[-1]
            ttk.Button(self.candidates, text=line if len(line) < 70 else line[:68] + "…",
                       command=lambda t=text: self.vars["제목"].set(t)).pack(anchor="w")
        for box, value in ((self.desc, r["설명"]), (self.comment, r["고정댓글"])):
            box.config(state="normal")
            box.delete("1.0", "end")
            box.insert("1.0", value)
            box.config(state="disabled")
        self.title_entry.focus_set()
        self.update_length()

    def update_length(self):
        n = len(self.vars["제목"].get())
        # 초안 제목 틀이 보통 30자 정도라 엑셀 제목은 70자 아래가 안전 (전체 100자 한도)
        self.length.config(text=f"{n}자" + (" ⚠ 길어요" if n > 68 else ""),
                           foreground=COLORS["accent"] if n > 68 else COLORS["muted"])

    def save(self):
        changes = {"제목": self.vars["제목"].get().strip(), "영상ID": core.video_id(self.vars["영상ID"].get()),
                   "회원우선": "O" if self.member.get() else ""}
        when = self.vars["예약일시"].get().strip()
        if when:
            ch = next((c for c in core.load_config()["channels"] if c["name"] == self.row["채널"]), None)
            try:
                t = core.parse_dt(when, core.parse_slots(ch["slots"], ch["name"]) if ch else None)
            except ValueError as e:
                messagebox.showerror("예약일시", str(e), parent=self.win)
                return False
            when = core.fmt_dt(t)
        changes["예약일시"] = when
        if self.no_thumb.get():
            changes["썸네일"] = "없음"
        elif self.row["썸네일"] == "없음":
            changes["썸네일"] = ""  # 다시 찾도록 ('2. 자동 처리'를 다시 누르면 채워짐)
        if all(self.row.get(k) == v for k, v in changes.items()):
            return True
        try:
            rows = core.read_rows()
            target = next(r for r in rows if r["파일"] == self.row["파일"])
            target.update(changes)
            core.write_rows(rows)
        except core.ExcelLocked:
            messagebox.showwarning("엑셀", "엑셀이 열려 있어서 저장하지 못했습니다. 엑셀을 닫고 다시 저장해주세요.",
                                   parent=self.win)
            return False
        self.app.refresh_status()
        return True

    def save_and_move(self, step):
        if not self.save():
            return
        nxt = self.index + step
        if step == 0 or not 0 <= nxt < len(self.app.rows):
            if step:
                messagebox.showinfo("영상 고치기", "마지막 영상입니다." if step > 0 else "첫 영상입니다.", parent=self.win)
            return
        self.index = nxt
        self.app.table.selection_set(str(nxt))
        self.app.table.see(str(nxt))
        self.load()


# 설정 칸: (키, 이름, 종류) — 종류가 목록이면 고르는 칸
FIELDS = [  # 늘 보이는 칸
    ("name", "채널 이름", "entry"),
    ("slots", "공개 요일·시간 (예: 월 12:15, 수 12:15)", "entry"),
    ("ypp", "수익 창출(YPP) 채널", "check"),
    ("members", "회원(멤버십) 기능 사용", "check"),
    ("playlist", "재생목록 (비우면 안 넣음, 여러 개는 쉼표로)", "entry"),
    ("thumb_auto", "썸네일 자동 만들기 (재료 사진 + 문구)", "check"),
    ("reply", "댓글 답글 (공개 후 48시간, Claude가 채널 말투로)", list(core.REPLY_MODES)),
]
ADVANCED = [  # '고급 설정'을 눌러야 보이는 칸 (보통 그대로)
    ("folder", "영상 폴더 이름 (보통 채널 이름과 같음)", "entry"),
    ("title_mode", "제목 넣는 자리", core.MODES),
    ("title_marker", "제목 기준 글자", "entry"),
    ("desc_mode", "설명 넣는 자리", core.MODES),
    ("desc_marker", "설명 기준 글자", "entry"),
    ("synthetic", "AI 변경·합성 콘텐츠 표시", [core.KEEP, "예", "아니요"]),
    ("reply_voice", "답글 말투 메모 (예: 따뜻한 존댓말, 짧게, 역사 이야기는 덧붙여)", "entry"),
]


class Settings:
    def __init__(self, app):
        self.app, self.cfg, self.cur, self.saved = app, core.load_config(), None, True
        win = self.win = tk.Toplevel(app.root)
        win.title("설정")
        win.protocol("WM_DELETE_WINDOW", self.close)
        left = ttk.Frame(win, padding=8)
        left.pack(side="left", fill="y")
        # 버튼을 먼저 아래에 붙인다: 화면이 작아 창이 줄어도 저장 버튼이 잘리지 않게
        ttk.Button(left, text="저장", command=self.save).pack(side="bottom", fill="x", pady=(10, 2), ipady=4)
        for text, cmd in reversed((("채널 추가", self.add), ("채널 삭제", self.remove), ("로그인", self.login))):
            ttk.Button(left, text=text, command=cmd).pack(side="bottom", fill="x", pady=2)
        self.listbox = tk.Listbox(left, width=26, exportselection=False, font=(FONT, 10))
        self.listbox.pack(fill="both", expand=True)
        self.listbox.bind("<<ListboxSelect>>", lambda e: self.select())

        form = ttk.Frame(win, padding=8)
        form.pack(side="left", fill="both", expand=True)
        self.login_label = ttk.Label(form, foreground="#555")
        self.login_label.grid(row=0, column=0, columnspan=2, sticky="w", pady=(0, 6))
        self.login_label.bind("<Button-1>", lambda e: self.open_channel())  # 로그인한 채널이 맞는지 YouTube에서 확인
        self.vars = {}

        def field(parent, row, key, label, kind):
            ttk.Label(parent, text=label).grid(row=row, column=0, sticky="w", pady=2, padx=(0, 8))
            var = tk.BooleanVar() if kind == "check" else tk.StringVar()
            widget = (ttk.Checkbutton(parent, variable=var) if kind == "check" else
                      ttk.Entry(parent, textvariable=var, width=40) if kind == "entry" else
                      ttk.Combobox(parent, textvariable=var, values=kind, state="readonly", width=38))
            widget.grid(row=row, column=1, sticky="w")
            var.trace_add("write", lambda *a: setattr(self, "saved", False))
            return var

        for i, (key, label, kind) in enumerate(FIELDS, 1):
            self.vars[key] = field(form, i, key, label, kind)
        n = len(FIELDS) + 1
        ttk.Label(form, foreground="#555", text="※ '채널 추가'를 누르고 브라우저에서 채널을 고르면 이름·공개 시간은 자동으로 채워집니다.").grid(
            row=n, column=0, columnspan=2, sticky="w", pady=(6, 0))

        adv = ttk.Frame(form)
        toggle = ttk.Button(form, text="고급 설정 ▸ (보통 안 건드려도 됨)")
        toggle.config(command=lambda: ((adv.grid_remove(), toggle.config(text="고급 설정 ▸ (보통 안 건드려도 됨)"))
                                       if adv.winfo_ismapped() else (adv.grid(), toggle.config(text="고급 설정 ▾")), fit(win)))
        toggle.grid(row=n + 1, column=0, columnspan=2, sticky="w", pady=(10, 4))
        adv.grid(row=n + 2, column=0, columnspan=2, sticky="w")
        for i, (key, label, kind) in enumerate(ADVANCED):
            self.vars[key] = field(adv, i, key, label, kind)
        self.max_fails = tk.StringVar(value=str(self.cfg["max_fails"]))
        self.mode = tk.StringVar(value=self.cfg["mode"])
        for var in (self.max_fails, self.mode):
            var.trace_add("write", lambda *a: setattr(self, "saved", False))
        m = len(ADVANCED)
        ttk.Label(adv, text="연속 실패 몇 번이면 멈춤").grid(row=m, column=0, sticky="w", pady=2)
        ttk.Spinbox(adv, from_=1, to=20, textvariable=self.max_fails, width=5).grid(row=m, column=1, sticky="w")
        ttk.Label(adv, text="진행 방식").grid(row=m + 1, column=0, sticky="w", pady=2)
        ttk.Combobox(adv, textvariable=self.mode, values=["한 번에 전체", "영상마다 확인"],
                     state="readonly", width=38).grid(row=m + 1, column=1, sticky="w")
        self.key = tk.StringVar(value=self.cfg["claude_key"])
        self.key.trace_add("write", lambda *a: setattr(self, "saved", False))
        ttk.Label(adv, text="Claude API 키 (썸네일 자동 만들기)").grid(row=m + 2, column=0, sticky="w", pady=2)
        ttk.Entry(adv, textvariable=self.key, width=40).grid(row=m + 2, column=1, sticky="w")
        ttk.Button(adv, text="이 채널 썸네일 틀 폴더 열기 (로고·폰트)", command=self.open_template).grid(
            row=m + 3, column=1, sticky="w", pady=4)
        ttk.Label(adv, foreground="#555", justify="left", text=(
            "※ 제목: '기준 글자 뒤' + ']' → Studio 제목 틀의 ] 바로 뒤에 들어감\n"
            "    예) [숨겨진 동양사] ＜제목＞ l 역사 인물 l 고전 | 수면오디오북\n"
            "※ '건들지 않음' = Studio 업로드 기본 설정 그대로\n"
            "※ API 키가 없으면 썸네일은 실제 사진 없이 만들고, 강조는 마지막 줄")).grid(
            row=m + 4, column=0, columnspan=2, sticky="w", pady=(6, 0))
        adv.grid_remove()
        self.refresh(0)
        self.saved = True
        fit(win)

    def commit(self):
        if self.cur is not None:
            ch = self.cfg["channels"][self.cur]
            old_name = ch["name"]
            for key, var in self.vars.items():
                ch[key] = var.get().strip() if isinstance(var.get(), str) else var.get()
            if ch["folder"] == old_name and ch["name"] != old_name and not core.channel_dirs({**ch, "folder": old_name}):
                ch["folder"] = ch["name"]  # 폴더 이름을 따로 안 바꿨고 그 폴더로 진행 중인 묶음이 없으면 이름을 따라간다
                self.vars["folder"].set(ch["folder"])  # 화면 칸도 같이 (안 그러면 다음 저장 때 옛 이름으로 돌아감)

    def refresh(self, index):
        self.listbox.delete(0, "end")
        for ch in self.cfg["channels"]:
            self.listbox.insert("end", ch["name"] + ("" if ch["channel_id"] else "  (로그인 필요)"))
        self.cur = None
        if self.cfg["channels"]:
            self.listbox.selection_set(min(index, len(self.cfg["channels"]) - 1))
            self.select()
        else:
            self.login_label.config(text="왼쪽 '채널 추가'를 먼저 누른 뒤 칸을 채우세요.")

    def select(self):
        sel = self.listbox.curselection()
        if not sel:
            return
        saved = self.saved
        self.commit()
        self.cur = sel[0]
        ch = self.cfg["channels"][self.cur]
        for key, var in self.vars.items():
            var.set(ch[key])
        self.saved = saved
        self.login_label.config(text=f"로그인됨: YouTube 채널 '{ch['channel_title']}' ({ch['channel_id']}) — 눌러서 채널 페이지 확인"
                                if ch["channel_id"] else "로그인 안 됨 — 왼쪽 '로그인'을 눌러주세요",
                                cursor="hand2" if ch["channel_id"] else "")

    def open_channel(self):
        import webbrowser
        ch = self.cfg["channels"][self.cur] if self.cur is not None and self.cur < len(self.cfg["channels"]) else None
        if ch and ch["channel_id"]:
            webbrowser.open(f"https://www.youtube.com/channel/{ch['channel_id']}")

    def add(self):
        self.commit()
        ch = core.new_channel(f"채널{len(self.cfg['channels']) + 1}")
        self.cfg["channels"].append(ch)
        self.saved = False
        self.refresh(len(self.cfg["channels"]) - 1)
        self.login()  # 이름·공개 시간은 로그인한 채널에서 알아낸다

    def remove(self):
        if self.cur is None or not messagebox.askyesno("삭제", "이 채널 설정을 지울까요?", parent=self.win):
            return
        del self.cfg["channels"][self.cur]
        self.cur, self.saved = None, False
        self.refresh(0)

    def login(self):
        if self.cur is None:
            return
        self.commit()
        ch = self.cfg["channels"][self.cur]
        had_id = bool(ch["channel_id"])
        messagebox.showinfo("로그인", f"브라우저가 열리면 '{ch['name']}' 채널의 구글 계정으로 로그인하고,\n"
                                    "채널 선택 화면에서 이 채널을 고르세요. (5분 안에)", parent=self.win)

        def work(app):
            import yt
            client = yt.build_client(yt.login(ch["key"], interactive=True, fresh=True))
            ch["channel_id"], ch["channel_title"] = yt.whoami(client)
            try:  # 썸네일 채널 마크용: 프로필 사진(800px)을 받아 둔다 (넣어 준 로고보다 크면 이걸 씀)
                import thumbs
                if url := yt.avatar_url(client):
                    thumbs.save_channel_logo(ch, url)
                    app.log(f"[{ch['name']}] 채널 프로필 사진을 받아 썸네일 마크로 쓸 수 있게 했습니다")
            except Exception as e:
                app.log(f"[{ch['name']}] (프로필 사진을 못 받았습니다: {e})")
            try:
                slots = core.detect_slots(yt.publish_times(client))
            except Exception:
                slots = ""  # 못 알아내면 칸은 그대로
            if had_id:
                self.note_slots(app, ch, slots)
            disk = core.load_config()  # 창에서 아직 저장 안 한 다른 수정은 건드리지 않는다
            same = next((c for c in disk["channels"] if c["key"] == ch["key"]), None)
            if same:
                same["channel_id"], same["channel_title"] = ch["channel_id"], ch["channel_title"]
                core.save_config(disk)
            app.log(f"[{ch['name']}] 로그인 완료: YouTube 채널 '{ch['channel_title']}'")

            def show():
                if not self.win.winfo_exists():
                    return
                self.commit()
                if ch not in self.cfg["channels"]:
                    return
                self.refresh(self.cfg["channels"].index(ch))
                if not had_id:
                    self.fill_from_youtube(ch, slots)
            app.later(show)
        self.app.run(work, "로그인")

    def fill_from_youtube(self, ch, slots):
        """처음 로그인한 채널: 이름·폴더·공개 시간을 채우고, 새로 추가한 채널이면 두 가지만 물어본 뒤 저장."""
        v, placeholder = self.vars, r"채널\d+"
        name = re.sub(r'[\\/:*?"<>|]', "", ch["channel_title"]).strip()
        is_new = bool(re.fullmatch(placeholder, v["name"].get())) or bool(ch.pop("_new", False))
        if name and re.fullmatch(placeholder, v["name"].get()) and name not in (c["name"] for c in self.cfg["channels"]):
            v["name"].set(name)  # SEO 이름으로 먼저 추가한 채널은 이름 그대로 (SEO 파일 이름과 짝이 맞게)
        if name and re.fullmatch(placeholder, v["folder"].get()):
            v["folder"].set(v["name"].get())
        if slots and is_new:
            v["slots"].set(slots)
        if is_new:
            v["ypp"].set(messagebox.askyesno("채널 정보", f"'{ch['channel_title']}' 채널은 수익 창출(YPP)이 된 채널인가요?",
                                             parent=self.win))
            v["members"].set(messagebox.askyesno("채널 정보", "이 채널에서 회원(멤버십) 기능을 쓰나요?", parent=self.win))
        if self.save():
            if not is_new:  # 직접 정해 둔 공개 시간은 바꾸지 않고 다르면 알려만 준다
                return self.note_slots(self.app, ch, slots)
            self.app.log(f"[{v['name'].get()}] " + (f"최근 공개 기록으로 공개 요일·시간을 '{slots}'로 맞췄습니다." if slots else
                         f"최근 공개 기록이 부족해 공개 요일·시간은 '{v['slots'].get()}' 그대로입니다. 다르면 설정에서 고쳐주세요."))

    @staticmethod
    def note_slots(app, ch, slots):
        try:
            if slots and set(core.parse_slots(slots)) != set(core.parse_slots(ch["slots"], ch["name"])):
                app.log(f"[{ch['name']}] 참고: 최근 실제 공개 시간은 '{slots}'인데 설정은 '{ch['slots']}'입니다. 바꾸려면 설정에서 고쳐주세요.")
        except ValueError:
            pass

    def open_template(self):
        if self.cur is None:
            return
        import thumbs
        folder = thumbs.template_dir(self.cfg["channels"][self.cur])
        folder.mkdir(parents=True, exist_ok=True)
        guide = folder / "여기에 넣을 것.txt"
        if not guide.exists():
            guide.write_text("로고.png  — 채널 마크 (배경이 투명한 PNG). 모든 썸네일에 같은 자리·같은 크기로 그대로 붙습니다\n"
                             "폰트 파일 (.ttf / .otf) — 썸네일 글씨체. 없으면 맑은 고딕 굵게\n"
                             "틀.json — 글자 자리·크기·색 조합·사진 칸 (예시 썸네일을 Claude에게 보내면 만들어 줍니다)\n", "utf-8")
        open_path(folder)

    def save(self):
        self.commit()
        try:
            fails = int(self.max_fails.get())
            if not 1 <= fails <= 50:
                raise ValueError
        except ValueError:
            return messagebox.showerror("저장 실패", "연속 실패 횟수는 1~50 사이 숫자로 적어주세요.", parent=self.win)
        self.cfg["max_fails"], self.cfg["mode"], self.cfg["claude_key"] = fails, self.mode.get(), self.key.get().strip()
        try:
            core.save_config(self.cfg)
        except ValueError as e:
            return messagebox.showerror("저장 실패", str(e), parent=self.win)
        self.saved = True
        self.app.log("설정을 저장했습니다.")
        self.app.refresh_status()
        self.refresh(self.cur or 0)
        return True

    def close(self):
        self.commit()
        if not self.saved:
            answer = messagebox.askyesnocancel("설정", "바꾼 설정을 저장할까요?", parent=self.win)
            if answer is None or (answer and not self.save()):
                return
        self.win.destroy()


def setup_display(root):
    """고해상도 화면에서 글자가 흐리거나 작지 않게."""
    for name in ("TkDefaultFont", "TkTextFont", "TkMenuFont", "TkHeadingFont"):
        try:
            tkfont.nametofont(name).configure(family=FONT, size=10)
        except tk.TclError:
            pass
    scale = root.winfo_fpixels("1i") / 96
    sw, sh = root.winfo_screenwidth(), root.winfo_screenheight()
    w, h = min(int(1240 * scale), sw - 20), min(int(760 * scale), sh - 90)  # 작은 화면·큰 배율에서도 화면 안에
    root.geometry(f"{w}x{h}+{max(0, (sw - w) // 2)}+{max(0, (sh - h) // 2 - 30)}")
    root.minsize(min(int(960 * scale), w), min(int(560 * scale), h))
    edit_menu(root)


def edit_menu(root):
    """모든 입력 칸(한 줄·여러 줄)에서 오른쪽 클릭 → 잘라내기·복사·붙여넣기·모두 선택, Ctrl+A로 모두 선택."""
    menu = tk.Menu(root, tearoff=0)
    target = {}

    def do(what):
        w = target.get("w")
        if not w:
            return
        if what == "all":
            if isinstance(w, tk.Text):
                w.tag_add("sel", "1.0", "end-1c")
            else:
                w.select_range(0, "end")
                w.icursor("end")
        else:
            w.event_generate(what)
    for label, what in (("잘라내기", "<<Cut>>"), ("복사", "<<Copy>>"), ("붙여넣기", "<<Paste>>"), (None, None), ("모두 선택", "all")):
        menu.add_separator() if label is None else menu.add_command(label=label, command=lambda w=what: do(w))

    def show(e):
        target["w"] = e.widget
        e.widget.focus_set()
        menu.tk_popup(e.x_root, e.y_root)
    for cls in ("TEntry", "Entry", "Text", "TCombobox"):
        root.bind_class(cls, "<Button-3>", show, add="+")
        root.bind_class(cls, "<Control-a>", lambda e: (target.update(w=e.widget), do("all"), "break")[-1], add="+")


def main():
    if sys.platform == "win32":
        try:
            import ctypes
            ctypes.windll.shcore.SetProcessDpiAwareness(1)
        except Exception:
            pass
    try:  # 파일을 끌어다 놓을 수 있는 창 (부품이 아직 없으면 보통 창 — 다음에 켤 때부터)
        from tkinterdnd2 import TkinterDnD
        root = TkinterDnD.Tk()
    except Exception:
        root = tk.Tk()
    if sys.version_info < (3, 9):
        messagebox.showerror("파이썬 버전", f"파이썬 3.9 이상이 필요합니다. 지금: {sys.version.split()[0]}")
        return
    root.title("롱폼 자동 업로드")
    setup_display(root)
    wait = ttk.Label(root, padding=30, text="처음 실행: 필요한 부품을 설치하는 중입니다 (1~2분).\n"
                                          "창이 멈춘 것처럼 보여도 기다려주세요.")
    wait.pack()
    root.update()
    try:
        ensure_packages()
    except Exception as e:
        messagebox.showerror("설치 실패", f"필요한 부품 설치에 실패했습니다.\n인터넷 연결을 확인해주세요.\n\n{e}")
        return
    wait.destroy()
    app = App(root)

    def on_error(exc, value, tb):  # 버튼을 누르다 난 오류: 검은 창이 없어 안 보이므로 화면과 기록에 남긴다
        import traceback
        text = "".join(traceback.format_exception(exc, value, tb))
        app.log(f"오류가 났습니다 (이 내용을 Claude에게 보내주세요):\n{text}")
        messagebox.showerror("오류", f"오류가 났습니다. 기록 폴더의 오늘 날짜 파일을 Claude에게 보내주세요.\n\n{value}")
    root.report_callback_exception = on_error
    root.mainloop()
