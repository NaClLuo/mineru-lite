"""开发期端到端测试：把样例文件塞进界面并真实跑一次转换。

用法：python tools/e2e.py <包含样例文件的目录>
"""

import shutil
import sys
import tkinter as tk
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "tools"))

import app  # noqa: E402
from capture import capture  # noqa: E402

IN_DIR = Path(sys.argv[1]).resolve() if len(sys.argv) > 1 else ROOT / "samples"
OUT_DIR = ROOT / ".probe" / "out"


def main():
    shutil.rmtree(OUT_DIR, ignore_errors=True)
    OUT_DIR.mkdir(parents=True, exist_ok=True)

    app.enable_dpi_awareness()
    root = tk.Tk()
    window = app.App(root)
    window._add_files([str(p) for p in sorted(IN_DIR.iterdir())])
    window.out_mode_var.set("subfolder")
    window.fmt_var.set("both")
    window._update_dir_entry_state()

    root.after(600, window._start)
    root.after(9000, lambda: capture("MinerU Lite", str(ROOT / ".probe" / "shot_running.png")))

    def wait_done(tries=0):
        if window.running and tries < 240:
            root.after(500, wait_done, tries + 1)
            return
        capture("MinerU Lite", str(ROOT / ".probe" / "shot_done.png"))
        for job in window.jobs:
            print(f"{job.path.name}\t{job.status}\t{job.progress}\t{job.outputs}")
        print("status:", window.status_label.cget("text"))
        root.after(400, root.destroy)

    root.after(2000, wait_done)
    root.mainloop()

    print("\n输出目录:")
    for path in sorted(OUT_DIR.glob("*")):
        print(f"  {path.name}  {path.stat().st_size} bytes")


if __name__ == "__main__":
    main()
