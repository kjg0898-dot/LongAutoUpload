"""창 화면. 오래 걸리는 일은 별도 스레드에서 돌리고, 화면 갱신은 메인 스레드에서만 한다."""
import os
import queue
import subprocess
import sys
import threading
import tkinter as tk
from tkinter import messagebox, scrolledtext, ttk

import core

PACKAGES = ["openpyxl", "google-api-python-client", "google-auth-oauthlib"]
NO_WINDOW = 0x08000000  # 설치·알림 때 검은 창이 뜨지 않게


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


class App:
    def __init__(self, root):
        self.root, self.q, self.busy = root, queue.Queue(), False
        root.title("롱폼 자동 업로드")
        root.geometry("760x520")
        bar = ttk.Frame(root, padding=8)
        bar.pack(fill="x")
        import steps
        ttk.Button(bar, text="새 묶음 폴더", command=self.make_batch).pack(side="left", padx=3)
        for text, fn in (("1. 엑셀 만들기", steps.build_excel), ("2. 정보·댓글 넣기", steps.fill_all),
                         ("3. 고정 탭 열기", None), ("4. 예약 걸기", steps.schedule_all)):
            cmd = self.pin_window if fn is None else (lambda f=fn: self.run(f))
            ttk.Button(bar, text=text, command=cmd).pack(side="left", padx=3)
        ttk.Button(bar, text="설정", command=lambda: Settings(self)).pack(side="right", padx=3)
        ttk.Button(bar, text="엑셀 열기", command=self.open_excel).pack(side="right", padx=3)
        self.text = scrolledtext.ScrolledText(root, wrap="word", font=("맑은 고딕", 10))
        self.text.pack(fill="both", expand=True, padx=8, pady=(0, 8))
        self.log("순서: 새 묶음 폴더에 파일 넣기 + Studio에 영상 올리기 → 1 → 엑셀에서 제목 수정 → 2 → 3(고정) → 4")
        root.after(100, self.poll)

    # --- 스레드에서 불러도 안전한 함수들 ---
    def log(self, msg):
        self.q.put(("log", msg))

    def alert(self, msg):
        toast("롱폼 자동 업로드", msg)
        self.q.put(("alert", msg))

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
                self.text.insert("end", data + "\n")
                self.text.see("end")
                messagebox.showwarning("알림", data)
            elif kind == "call":
                data()
            else:
                question, answer, done = data
                answer.append(messagebox.askyesno("확인", question))
                done.set()
        self.root.after(100, self.poll)

    def run(self, fn):
        if self.busy:
            return messagebox.showinfo("잠시만요", "이전 작업이 아직 진행 중입니다.")
        self.busy = True

        def work():
            try:
                fn(self)
            except Exception as e:
                self.alert(f"멈췄습니다: {e}")
            finally:
                self.busy = False
        threading.Thread(target=work, daemon=True).start()

    def make_batch(self):
        cfg = core.load_config()
        if not cfg["channels"]:
            return messagebox.showinfo("새 묶음", "설정에서 채널을 먼저 추가해주세요.")
        batch = core.new_batch(cfg)
        self.log(f"새 묶음 폴더를 만들었습니다: {batch.name}\n"
                 "채널 폴더마다 영상·SEO 메모장·썸네일을 넣으세요. 폴더 안에서는 아무렇게나 둬도 됩니다.")
        os.startfile(batch)

    def open_excel(self):
        if not core.EXCEL.exists():
            return messagebox.showinfo("엑셀", "아직 엑셀이 없습니다. 1번을 먼저 눌러주세요.")
        os.startfile(core.EXCEL)

    def pin_window(self):
        import steps
        targets = steps.pin_targets(core.read_rows(), core.load_config())
        if not targets:
            return messagebox.showinfo("고정", "고정할 영상이 없습니다. 2번을 먼저 진행해주세요.")
        win = tk.Toplevel(self.root)
        win.title("고정 탭 열기")
        ttk.Label(win, padding=10, text="채널마다 브라우저가 그 채널 계정으로 로그인돼 있어야 고정할 수 있습니다.\n"
                                       "고정(수창 채널은 광고 설문까지)을 끝낸 탭은 닫으면 됩니다.").pack()
        for name, urls in targets.items():
            ttk.Button(win, text=f"{name} — 탭 {len(urls)}개 열기",
                       command=lambda u=urls: steps.open_tabs(u)).pack(fill="x", padx=10, pady=2)
        ttk.Button(win, text="다 했으면 4. 예약 걸기", command=lambda: (win.destroy(), self.run(
            steps.schedule_all))).pack(fill="x", padx=10, pady=10)


# 설정 칸: (키, 이름, 종류) — 종류가 목록이면 고르는 칸
FIELDS = [
    ("name", "채널 이름", "entry"),
    ("folder", "영상 폴더 (영상/ 아래 이름)", "entry"),
    ("ypp", "수익 창출(YPP) 채널", "check"),
    ("members", "회원(멤버십) 기능 사용", "check"),
    ("slots", "공개 요일·시간 (예: 월 18:00, 수 18:00)", "entry"),
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


class Settings:
    def __init__(self, app):
        self.app, self.cfg, self.cur = app, core.load_config(), None
        win = self.win = tk.Toplevel(app.root)
        win.title("설정")
        left = ttk.Frame(win, padding=8)
        left.pack(side="left", fill="y")
        self.listbox = tk.Listbox(left, width=18, exportselection=False)
        self.listbox.pack(fill="y", expand=True)
        self.listbox.bind("<<ListboxSelect>>", lambda e: self.select())
        for text, cmd in (("채널 추가", self.add), ("채널 삭제", self.remove), ("로그인", self.login)):
            ttk.Button(left, text=text, command=cmd).pack(fill="x", pady=2)

        form = ttk.Frame(win, padding=8)
        form.pack(side="left", fill="both", expand=True)
        self.login_label = ttk.Label(form, foreground="#555")
        self.login_label.grid(row=0, column=0, columnspan=2, sticky="w", pady=(0, 6))
        self.vars = {}
        for i, (key, label, kind) in enumerate(FIELDS, 1):
            ttk.Label(form, text=label).grid(row=i, column=0, sticky="w", pady=1)
            if kind == "check":
                var = tk.BooleanVar()
                ttk.Checkbutton(form, variable=var).grid(row=i, column=1, sticky="w")
            else:
                var = tk.StringVar()
                widget = ttk.Entry(form, textvariable=var, width=34) if kind == "entry" else \
                    ttk.Combobox(form, textvariable=var, values=kind, state="readonly", width=32)
                widget.grid(row=i, column=1, sticky="w")
            self.vars[key] = var

        n = len(FIELDS) + 1
        ttk.Separator(form).grid(row=n, column=0, columnspan=2, sticky="ew", pady=8)
        self.max_fails = tk.IntVar(value=self.cfg["max_fails"])
        self.mode = tk.StringVar(value=self.cfg["mode"])
        ttk.Label(form, text="연속 실패 몇 번이면 멈춤").grid(row=n + 1, column=0, sticky="w")
        ttk.Spinbox(form, from_=1, to=20, textvariable=self.max_fails, width=5).grid(row=n + 1, column=1, sticky="w")
        ttk.Label(form, text="진행 방식").grid(row=n + 2, column=0, sticky="w")
        ttk.Combobox(form, textvariable=self.mode, values=["한 번에 전체", "영상마다 확인"],
                     state="readonly", width=32).grid(row=n + 2, column=1, sticky="w")
        ttk.Button(form, text="저장", command=self.save).grid(row=n + 3, column=1, sticky="e", pady=10)
        self.refresh(0)

    def commit(self):
        if self.cur is not None:
            ch = self.cfg["channels"][self.cur]
            for key, var in self.vars.items():
                ch[key] = var.get()

    def refresh(self, index):
        self.listbox.delete(0, "end")
        for ch in self.cfg["channels"]:
            self.listbox.insert("end", ch["name"])
        self.cur = None
        if self.cfg["channels"]:
            self.listbox.selection_set(index)
            self.select()

    def select(self):
        sel = self.listbox.curselection()
        if not sel:
            return
        self.commit()
        self.cur = sel[0]
        ch = self.cfg["channels"][self.cur]
        for key, var in self.vars.items():
            var.set(ch[key])
        self.login_label.config(text=f"로그인: {ch['channel_title']} ({ch['channel_id']})" if ch["channel_id"]
                                else "로그인 안 됨 — 왼쪽 '로그인'을 눌러주세요")

    def add(self):
        self.commit()
        self.cfg["channels"].append(core.new_channel(f"채널{len(self.cfg['channels']) + 1}"))
        self.refresh(len(self.cfg["channels"]) - 1)

    def remove(self):
        if self.cur is None or not messagebox.askyesno("삭제", "이 채널 설정을 지울까요?", parent=self.win):
            return
        del self.cfg["channels"][self.cur]
        self.cur = None
        self.refresh(0)

    def login(self):
        if self.cur is None:
            return
        self.commit()
        ch = self.cfg["channels"][self.cur]
        messagebox.showinfo("로그인", f"브라우저가 열리면 '{ch['name']}' 채널의 구글 계정으로 로그인하고,\n"
                                    "채널 선택 화면에서 이 채널을 고르세요.", parent=self.win)

        def work(app):
            import yt
            client = yt.build_client(yt.login(ch["key"], interactive=True))
            ch["channel_id"], ch["channel_title"] = yt.whoami(client)
            core.save_config(self.cfg)
            app.log(f"[{ch['name']}] 로그인 완료: {ch['channel_title']}")
            app.later(lambda: self.win.winfo_exists() and self.refresh(self.cur or 0))
        self.app.run(work)

    def save(self):
        self.commit()
        self.cfg["max_fails"], self.cfg["mode"] = int(self.max_fails.get()), self.mode.get()
        try:
            for ch in self.cfg["channels"]:
                core.parse_slots(ch["slots"])
            core.save_config(self.cfg)
        except ValueError as e:
            return messagebox.showerror("저장 실패", str(e), parent=self.win)
        self.app.log("설정을 저장했습니다.")
        self.refresh(self.cur or 0)


def main():
    root = tk.Tk()
    if sys.version_info < (3, 9):
        messagebox.showerror("파이썬 버전", f"파이썬 3.9 이상이 필요합니다. 지금: {sys.version.split()[0]}")
        return
    root.title("롱폼 자동 업로드")
    wait = ttk.Label(root, padding=30, text="처음 실행: 필요한 부품을 설치하는 중입니다...")
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
