"""창 화면. 오래 걸리는 일은 별도 스레드에서 돌리고, 화면 갱신은 메인 스레드에서만 한다."""
import os
import queue
import subprocess
import sys
import threading
import tkinter as tk
from datetime import datetime
from pathlib import Path
from tkinter import font as tkfont
from tkinter import messagebox, scrolledtext, ttk

import core

PACKAGES = ["openpyxl", "google-api-python-client", "google-auth-oauthlib"]
NO_WINDOW = 0x08000000  # 설치·알림 때 검은 창이 뜨지 않게
FONT = "맑은 고딕"


def ensure_packages():
    try:
        import google_auth_oauthlib  # noqa: F401
        import googleapiclient  # noqa: F401
        import openpyxl  # noqa: F401
    except ImportError:
        subprocess.check_call([sys.executable, "-m", "pip", "install", "--quiet", *PACKAGES],
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
        for text, cmd in (("청소", self.clean_window), ("업데이트", self.update), ("기록", lambda: self.open_dir(core.LOG_DIR)),
                          ("완료 폴더", lambda: self.open_dir(core.DONE_DIR)), ("영상 폴더", lambda: self.open_dir(core.VIDEO_DIR)),
                          ("안내", self.open_help), ("⚙ 설정", self.open_settings)):
            ttk.Button(top, text=text, command=cmd).pack(side="right", padx=2)

        body = ttk.PanedWindow(root, orient="horizontal")
        body.pack(fill="both", expand=True, padx=12, pady=4)
        left, right = ttk.Frame(body), ttk.Frame(body)
        body.add(left, weight=0)
        body.add(right, weight=1)

        # 왼쪽: 단계 카드 (다음 할 단계를 노랗게)
        self.cards, self.buttons = {}, {}
        for key, title, label, cmd in (
                ("0", "새 묶음 폴더", "폴더 만들기", self.make_batch),
                ("1", "엑셀(목록) 만들기", "실행", lambda: self.run(steps.build_excel)),
                ("제목", "제목 확인", "엑셀로 열기", self.open_excel),
                ("2", "정보·댓글 넣기", "실행", lambda: self.run(steps.fill_all)),
                ("3", "댓글 고정", "탭 열기", self.pin_window),
                ("4", "예약 걸기", "실행", lambda: self.run(steps.schedule_all))):
            card = tk.Frame(left, bg=COLORS["card"], highlightthickness=1, highlightbackground=COLORS["edge"], padx=10, pady=8)
            card.pack(fill="x", pady=3)
            badge = tk.Label(card, text="✎" if key == "제목" else key, width=2, font=(FONT, 13, "bold"),
                             bg=COLORS["card"], fg=COLORS["muted"])
            badge.grid(row=0, column=0, rowspan=2, sticky="n", padx=(0, 8))
            name = tk.Label(card, text=title, font=(FONT, 11, "bold"), bg=COLORS["card"], anchor="w")
            name.grid(row=0, column=1, sticky="w")
            info = tk.Label(card, text="", font=(FONT, 9), fg=COLORS["muted"], bg=COLORS["card"], anchor="w",
                            justify="left", wraplength=230)
            info.grid(row=1, column=1, sticky="w")
            button = ttk.Button(card, text=label, command=cmd, width=10)
            button.grid(row=0, column=2, rowspan=2, padx=(10, 0))
            card.columnconfigure(1, weight=1)
            self.cards[key] = (card, badge, name, info)
            self.buttons[key] = button
        self.step_buttons = list(self.buttons.values())

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
        self.text = scrolledtext.ScrolledText(root, wrap="word", font=(FONT, 9), height=7)
        self.text.pack(fill="x", padx=12, pady=(0, 10))

        root.protocol("WM_DELETE_WINDOW", self.close)
        root.bind("<F5>", lambda e: self.refresh_status())
        self.first_run_guide()
        self.refresh_status()
        root.after(100, self.poll)
        self.check_update()

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
        names = {"build_excel": "1. 엑셀 만들기", "fill_all": "2. 정보·댓글 넣기", "schedule_all": "4. 예약 걸기"}
        self.set_busy(label or names.get(fn.__name__, "작업"))

        def work():
            try:
                fn(self)
            except Exception as e:
                self.alert(f"멈췄습니다: {e}")
            finally:
                self.later(lambda: (self.set_busy(None), self.refresh_status()))
        threading.Thread(target=work, daemon=True).start()

    def refresh_status(self):
        try:
            self.rows, cfg = core.read_rows(), core.load_config()
            text = self.steps.status_summary(self.rows, cfg)
            has_videos = core.VIDEO_DIR.exists() and any(
                p.suffix.lower() in core.VIDEO_EXT for p in core.VIDEO_DIR.rglob("*"))
            infos, nxt = self.steps.card_status(self.rows, has_videos)
            names = ["전체 채널"] + [ch["name"] for ch in cfg["channels"]]
        except Exception as e:
            self.rows, text, infos, nxt, names = [], f"엑셀을 읽지 못했습니다: {e}", {}, None, ["전체 채널"]
        self.summary.config(text=text.replace("\n", "   "))
        self.filter_box.config(values=names)
        if self.channel_filter.get() not in names:
            self.channel_filter.set("전체 채널")
        for key, (card, badge, name, info) in self.cards.items():
            bg = COLORS["next"] if key == nxt else COLORS["card"]
            for w in (card, badge, name, info):
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
        menu.add_command(label="썸네일 다시 찾기 (1단계)" if no_thumb else "썸네일 없이 올리기",
                         state="normal" if r["상태"] == "" else "disabled",
                         command=lambda: self.change_row(index, {"썸네일": "" if no_thumb else "없음"}))
        # 정보완료(댓글 전)·대기 중 오류만 처음부터: 원본 틀 백업에서 다시 합치므로 제목이 두 번 안 들어감
        can_redo = r["상태"] == "정보완료" or (r["상태"] == "" and r["오류"])
        menu.add_command(label="2단계 처음부터 다시 하기", state="normal" if can_redo else "disabled",
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
            todo.append("① '안내' → 구글 연동 안내대로 열쇠 파일(client_secret_….json)을 _프로그램 폴더에 넣기")
        if not cfg["channels"]:
            todo.append("② '설정' → 채널 추가 → 칸 채우기 → 로그인 → 저장")
        missing = [ch["name"] for ch in cfg["channels"] if not ch["channel_id"]]
        if missing:
            todo.append(f"③ 로그인이 필요한 채널: {', '.join(missing)} ('설정'에서 채널 선택 → 로그인)")
        self.log("처음 준비할 것:\n  " + "\n  ".join(todo) if todo else
                 "순서: 0 → 영상 넣고 Studio에 업로드 → 1 → 목록에서 제목 확인 → 2 → 3(고정) → 4")

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
                 "채널 폴더마다 영상·SEO 메모장·썸네일을 넣으세요. 폴더 안에서는 아무렇게나 둬도 됩니다.")
        open_path(batch)

    def open_excel(self):
        if not core.EXCEL.exists():
            return messagebox.showinfo("엑셀", "아직 엑셀이 없습니다. 1번을 먼저 눌러주세요.")
        self.log("엑셀: 노란 칸만 고치고 저장한 뒤 꼭 닫아주세요 (열려 있으면 2·4단계가 저장하지 못합니다).")
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
            "프로그램 파일은 지우지 않습니다 (지워도 켤 때 자동 업데이트로 다시 받음).")).pack(anchor="w")
        checks = {}
        for name, on, risky in core.CLEAN_GROUPS:
            paths = groups[name]
            var = tk.BooleanVar(value=on and bool(paths))
            size = core.size_of(paths)
            label = f"{'⚠ ' if risky else ''}{name} — " + (f"{len(paths)}개, {size / 1024 / 1024:.1f}MB" if paths else "없음")
            cb = ttk.Checkbutton(win, text=label, variable=var)
            cb.pack(anchor="w", padx=14, pady=1)
            if not paths:
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
                failed = core.trash([p for _, _, paths in chosen for p in paths])
                if failed:
                    app.alert("청소: 지우지 못한 것 (다른 프로그램에서 열려 있는지 확인):\n" + "\n".join(str(f) for f in failed))
                else:  # 기록 파일에는 안 남긴다 (방금 지운 기록 폴더가 다시 생기지 않게)
                    msg = "청소: 깨끗하게 지웠습니다. " + ", ".join(name.split(" (")[0] for name, _, _ in chosen)
                    toast("롱폼 자동 업로드", msg)
                    app.q.put(("log", msg))
            self.run(work, "청소")
        ttk.Button(bar, text="모두 선택 (완전 초기화)", command=select_all).pack(side="left")
        ttk.Button(bar, text="청소하기", command=go).pack(side="right")

    def open_settings(self):
        if self.settings and self.settings.win.winfo_exists():
            return self.settings.win.lift()  # 설정 창은 하나만
        self.settings = Settings(self)

    def pin_window(self):
        cfg = core.load_config()
        targets = self.steps.pin_targets(core.read_rows(), cfg)
        if not targets:
            return messagebox.showinfo("고정", "고정할 영상이 없습니다. 2번을 먼저 진행해주세요.")
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
        ttk.Button(win, text="고정을 다 했으면 → 4. 예약 걸기", command=lambda: (win.destroy(), self.run(
            self.steps.schedule_all))).pack(fill="x", padx=10, pady=10)


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
            changes["썸네일"] = ""  # 다시 찾도록 (1단계를 다시 누르면 채워짐)
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
FIELDS = [
    ("name", "채널 이름", "entry"),
    ("folder", "영상 폴더 이름 (묶음 폴더 안)", "entry"),
    ("ypp", "수익 창출(YPP) 채널", "check"),
    ("members", "회원(멤버십) 기능 사용", "check"),
    ("slots", "공개 요일·시간 (예: 월 12:15, 수 12:15)", "entry"),
    ("title_mode", "제목 넣는 자리", core.MODES),
    ("title_marker", "제목 기준 글자", "entry"),
    ("desc_mode", "설명 넣는 자리", core.MODES),
    ("desc_marker", "설명 기준 글자", "entry"),
    ("tags_mode", "태그 넣는 자리", ["뒤에 추가", "앞에 추가"]),
    ("playlist", "재생목록 ID (비우면 안 넣음)", "entry"),
    ("category", "카테고리", [core.KEEP, *core.CATEGORIES]),
    ("language", "언어", [core.KEEP, *core.LANGUAGES]),
    ("synthetic", "AI 변경·합성 콘텐츠", [core.KEEP, "예", "아니요"]),
    ("kids", "아동용", [core.KEEP, "예", "아니요"]),
    ("embeddable", "퍼가기", [core.KEEP, "허용", "허용 안 함"]),
    ("stats", "공개 통계 표시", [core.KEEP, "공개", "비공개"]),
    ("license", "라이선스", [core.KEEP, "표준 YouTube 라이선스", "크리에이티브 커먼즈"]),
]
COPY_SKIP = {"key", "name", "folder", "channel_id", "channel_title"}  # 채널 복제 때 새로 정하는 칸


class Settings:
    def __init__(self, app):
        self.app, self.cfg, self.cur, self.saved = app, core.load_config(), None, True
        win = self.win = tk.Toplevel(app.root)
        win.title("설정")
        win.protocol("WM_DELETE_WINDOW", self.close)
        left = ttk.Frame(win, padding=8)
        left.pack(side="left", fill="y")
        self.listbox = tk.Listbox(left, width=26, exportselection=False, font=(FONT, 10))
        self.listbox.pack(fill="y", expand=True)
        self.listbox.bind("<<ListboxSelect>>", lambda e: self.select())
        for text, cmd in (("채널 추가", self.add), ("이 채널 복제", self.duplicate), ("채널 삭제", self.remove),
                          ("로그인", self.login)):
            ttk.Button(left, text=text, command=cmd).pack(fill="x", pady=2)

        form = ttk.Frame(win, padding=8)
        form.pack(side="left", fill="both", expand=True)
        self.login_label = ttk.Label(form, foreground="#555")
        self.login_label.grid(row=0, column=0, columnspan=2, sticky="w", pady=(0, 6))
        self.vars = {}
        for i, (key, label, kind) in enumerate(FIELDS, 1):
            ttk.Label(form, text=label).grid(row=i, column=0, sticky="w", pady=1, padx=(0, 8))
            if kind == "check":
                var = tk.BooleanVar()
                ttk.Checkbutton(form, variable=var).grid(row=i, column=1, sticky="w")
            else:
                var = tk.StringVar()
                widget = ttk.Entry(form, textvariable=var, width=34) if kind == "entry" else \
                    ttk.Combobox(form, textvariable=var, values=kind, state="readonly", width=32)
                widget.grid(row=i, column=1, sticky="w")
            var.trace_add("write", lambda *a: setattr(self, "saved", False))
            self.vars[key] = var

        n = len(FIELDS) + 1
        ttk.Label(form, foreground="#555", justify="left", text=(
            "※ 보통 바꿀 것: 채널 이름, 공개 요일·시간, 수익 창출·회원 체크. 나머지는 그대로 두면 됩니다.\n"
            "※ 제목: '기준 글자 뒤' + ']' → Studio 제목 틀의 ] 바로 뒤에 들어감\n"
            "    예) [숨겨진 동양사] ＜엑셀 제목＞ l 역사 인물 l 고전 | 수면오디오북\n"
            "※ '건들지 않음' = Studio 업로드 기본 설정 그대로 둠")).grid(
            row=n, column=0, columnspan=2, sticky="w", pady=(6, 0))
        ttk.Separator(form).grid(row=n + 1, column=0, columnspan=2, sticky="ew", pady=8)
        self.max_fails = tk.StringVar(value=str(self.cfg["max_fails"]))
        self.mode = tk.StringVar(value=self.cfg["mode"])
        for var in (self.max_fails, self.mode):
            var.trace_add("write", lambda *a: setattr(self, "saved", False))
        ttk.Label(form, text="연속 실패 몇 번이면 멈춤").grid(row=n + 2, column=0, sticky="w")
        ttk.Spinbox(form, from_=1, to=20, textvariable=self.max_fails, width=5).grid(row=n + 2, column=1, sticky="w")
        ttk.Label(form, text="진행 방식").grid(row=n + 3, column=0, sticky="w")
        ttk.Combobox(form, textvariable=self.mode, values=["한 번에 전체", "영상마다 확인"],
                     state="readonly", width=32).grid(row=n + 3, column=1, sticky="w")
        ttk.Button(form, text="저장", command=self.save).grid(row=n + 4, column=1, sticky="e", pady=10)
        self.refresh(0)
        self.saved = True

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
        self.login_label.config(text=f"로그인됨: YouTube 채널 '{ch['channel_title']}'" if ch["channel_id"]
                                else "로그인 안 됨 — 왼쪽 '로그인'을 눌러주세요")

    def add(self, base=None):
        self.commit()
        n = len(self.cfg["channels"]) + 1
        ch = core.new_channel(f"채널{n}")
        if base:
            ch |= {k: v for k, v in base.items() if k not in COPY_SKIP} | {"name": f"{base['name']} 복사",
                                                                           "folder": f"{base['folder']} 복사"}
        self.cfg["channels"].append(ch)
        self.saved = False
        self.refresh(len(self.cfg["channels"]) - 1)

    def duplicate(self):
        if self.cur is None:
            return
        self.commit()
        self.add(dict(self.cfg["channels"][self.cur]))

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
        messagebox.showinfo("로그인", f"브라우저가 열리면 '{ch['name']}' 채널의 구글 계정으로 로그인하고,\n"
                                    "채널 선택 화면에서 이 채널을 고르세요. (5분 안에)", parent=self.win)

        def work(app):
            import yt
            client = yt.build_client(yt.login(ch["key"], interactive=True, fresh=True))
            ch["channel_id"], ch["channel_title"] = yt.whoami(client)
            disk = core.load_config()  # 창에서 아직 저장 안 한 다른 수정은 건드리지 않는다
            same = next((c for c in disk["channels"] if c["key"] == ch["key"]), None)
            if same:
                same["channel_id"], same["channel_title"] = ch["channel_id"], ch["channel_title"]
                core.save_config(disk)
            else:
                app.log(f"[{ch['name']}] 새 채널이라 설정 창에서 '저장'을 눌러야 로그인이 기록됩니다.")
            app.log(f"[{ch['name']}] 로그인 완료: YouTube 채널 '{ch['channel_title']}'")

            def show():
                if self.win.winfo_exists():
                    self.commit()
                    self.refresh(self.cur or 0)
            app.later(show)
        self.app.run(work, "로그인")

    def save(self):
        self.commit()
        try:
            fails = int(self.max_fails.get())
            if not 1 <= fails <= 50:
                raise ValueError
        except ValueError:
            return messagebox.showerror("저장 실패", "연속 실패 횟수는 1~50 사이 숫자로 적어주세요.", parent=self.win)
        self.cfg["max_fails"], self.cfg["mode"] = fails, self.mode.get()
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
    root.geometry(f"{int(1240 * scale)}x{int(760 * scale)}")
    root.minsize(int(960 * scale), int(600 * scale))


def main():
    if sys.platform == "win32":
        try:
            import ctypes
            ctypes.windll.shcore.SetProcessDpiAwareness(1)
        except Exception:
            pass
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
