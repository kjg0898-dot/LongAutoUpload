"""더블클릭하면 창이 뜬다. 프로그램이 망가져 안 켜지면 최신 버전을 다시 받아 스스로 복구한다."""
import subprocess
import sys
import traceback
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / "_프로그램"))


def rescue(error):
    """켜다가 오류가 나면: 오류를 기록하고, GitHub에서 최신 버전을 받아 바뀐 게 있으면 다시 켠다."""
    log = ROOT / "_프로그램" / "켜기_오류.txt"
    log.write_text(error, encoding="utf-8")
    try:
        import update
        changed = update.apply(update.fetch())
    except Exception:
        changed = 0
    if changed:
        subprocess.Popen([sys.executable, str(ROOT / "실행.pyw")], cwd=str(ROOT))
        return
    import tkinter as tk
    from tkinter import messagebox
    tk.Tk().withdraw()
    messagebox.showerror("롱폼 자동 업로드", "프로그램을 켜다가 오류가 났습니다.\n"
                         f"{log} 내용을 Claude에게 보내주세요.\n\n{error[-600:]}")


if __name__ == "__main__":
    try:
        import gui
        gui.main()
    except Exception:
        rescue(traceback.format_exc())
