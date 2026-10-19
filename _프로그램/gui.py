"""창 화면. 오래 걸리는 일은 별도 스레드에서 돌리고, 화면 갱신은 메인 스레드에서만 한다."""
import os
import queue
import subprocess
import sys
import threading
import tkinter as tk
from datetime import datetime
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


class App:
    def __init__(self, root):
        self.root, self.q, self.busy, self.settings = root, queue.Queue(), False, None
        root.title("롱폼 자동 업로드")
        import steps
        self.steps = steps

        bar = ttk.Frame(root, padding=(8, 8, 8, 2))
        bar.pack(fill="x")
        self.step_buttons = []
        for text, cmd in (("0. 새 묶음 폴더", self.make_batch), ("1. 엑셀 만들기", lambda: self.run(steps.build_excel)),
                          ("엑셀 열기(제목 수정)", self.open_excel), ("2. 정보·댓글 넣기", lambda: self.run(steps.fill_all)),
                          ("3. 고정 탭 열기", self.pin_window), ("4. 예약 걸기", lambda: self.run(steps.schedule_all))):
            b = ttk.Button(bar, text=text, command=cmd)
            b.pack(side="left", padx=2)
            self.step_buttons.append(b)
        bar2 = ttk.Frame(root, padding=(8, 2, 8, 4))
        bar2.pack(fill="x")
        for text, cmd in (("설정", self.open_settings), ("안내", self.open_help), ("영상 폴더", lambda: self.open_dir(core.VIDEO_DIR)),
                          ("완료 폴더", lambda: self.open_dir(core.DONE_DIR)), ("기록 폴더", lambda: self.open_dir(core.LOG_DIR))):
            ttk.Button(bar2, text=text, command=cmd).pack(side="left", padx=2)

        box = ttk.LabelFrame(root, text="진행 상황", padding=6)
        box.pack(fill="x", padx=8, pady=4)
        self.summary = ttk.Label(box, justify="left")
        self.summary.pack(anchor="w")
        line = ttk.Frame(box)
        line.pack(fill="x", pady=(4, 0))
        self.state = ttk.Label(line, text="대기 중")
        self.state.pack(side="left")
        self.progress = ttk.Progressbar(line, mode="indeterminate", length=180)
        self.progress.pack(side="right")

        self.text = scrolledtext.ScrolledText(root, wrap="word", font=(FONT, 10), height=16)
        self.text.pack(fill="both", expand=True, padx=8, pady=(0, 8))
        root.protocol("WM_DELETE_WINDOW", self.close)
        self.first_run_guide()
        self.refresh_status()
        root.after(100, self.poll)

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
        while not self.q.empty():
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
        self.root.after(100, self.poll)

    # --- 작업 실행 ---
    def set_busy(self, label):
        self.busy = bool(label)
        for b in self.step_buttons:
            b.state(["disabled"] if label else ["!disabled"])
        self.state.config(text=f"작업 중: {label}" if label else "대기 중")
        if label:
            self.progress.start(15)
        else:
            self.progress.stop()
            self.progress["value"] = 0

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
            text = self.steps.status_summary(core.read_rows(), core.load_config())
        except Exception as e:
            text = f"엑셀을 읽지 못했습니다: {e}"
        self.summary.config(text=text)

    def first_run_guide(self):
        import yt
        cfg, todo = core.load_config(), []
        if not yt.SECRET.exists():
            todo.append("① '안내' → 구글 연동 안내대로 client_secret.json을 _프로그램 폴더에 넣기")
        if not cfg["channels"]:
            todo.append("② '설정' → 채널 추가 → 칸 채우기 → 로그인 → 저장")
        missing = [ch["name"] for ch in cfg["channels"] if not ch["channel_id"]]
        if missing:
            todo.append(f"③ 로그인이 필요한 채널: {', '.join(missing)} ('설정'에서 채널 선택 → 로그인)")
        self.log("처음 준비할 것:\n  " + "\n  ".join(todo) if todo else
                 "순서: 0 → 영상 넣고 Studio에 업로드 → 1 → 엑셀에서 제목 확인 → 2 → 3(고정) → 4")

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
        if not yt.SECRET.exists():
            open_path(core.PROG / "구글연동_안내.md")

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
        self.listbox = tk.Listbox(left, width=18, exportselection=False, font=(FONT, 10))
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
        ttk.Label(form, foreground="#555", text="※ '건들지 않음' = Studio 업로드 기본 설정 그대로 둠").grid(
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

    def refresh(self, index):
        self.listbox.delete(0, "end")
        for ch in self.cfg["channels"]:
            self.listbox.insert("end", ch["name"] + ("" if ch["channel_id"] else "  (로그인 필요)"))
        self.cur = None
        if self.cfg["channels"]:
            self.listbox.selection_set(min(index, len(self.cfg["channels"]) - 1))
            self.select()

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
    root.geometry(f"{int(900 * scale)}x{int(620 * scale)}")


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
    App(root)
    root.mainloop()
