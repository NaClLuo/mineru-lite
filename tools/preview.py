"""开发期预览：启动界面并直接切到指定状态后截图。

用法：python tools/preview.py advanced|settings|main out.png
"""

import sys
import tkinter as tk
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "tools"))

import app  # noqa: E402
from capture import capture  # noqa: E402


def main():
    mode = sys.argv[1] if len(sys.argv) > 1 else "main"
    out = sys.argv[2] if len(sys.argv) > 2 else "shot.png"

    app.enable_dpi_awareness()
    root = tk.Tk()
    window = app.App(root)

    if mode == "advanced":
        window._toggle_advanced()
    elif mode == "settings":
        root.after(300, window._open_settings)
    if len(sys.argv) > 3:
        root.geometry(sys.argv[3])

    def shoot():
        try:
            capture("设置" if mode == "settings" else "MinerU Lite", out)
        finally:
            root.destroy()

    root.after(1500, shoot)
    root.mainloop()


if __name__ == "__main__":
    main()
