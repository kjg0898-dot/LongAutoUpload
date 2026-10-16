"""더블클릭하면 창이 뜬다."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "_프로그램"))
import gui  # noqa: E402

gui.main()
