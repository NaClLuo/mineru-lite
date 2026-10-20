"""验证 Windows 拖拽：构造 HDROP 并投递 WM_DROPFILES 给窗口。

用法：python tools/drop_test.py <文件路径>
"""

import ctypes
import struct
import sys
import threading
import tkinter as tk
from ctypes import wintypes
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

import app  # noqa: E402


def build_hdrop(files: list[str]):
    """按 DROPFILES 结构在全局内存里拼出文件列表。"""
    kernel32 = ctypes.windll.kernel32
    payload = "\0".join(files) + "\0\0"
    header = struct.pack("<IiiII", 20, 0, 0, 0, 1)  # pFiles, pt, fNC, fWide=1
    body = header + payload.encode("utf-16-le")

    kernel32.GlobalAlloc.restype = ctypes.c_void_p
    kernel32.GlobalAlloc.argtypes = [wintypes.UINT, ctypes.c_size_t]
    handle = kernel32.GlobalAlloc(0x0042, len(body))  # GMEM_MOVEABLE | GMEM_ZEROINIT
    if not handle:
        raise OSError("GlobalAlloc failed")

    kernel32.GlobalLock.restype = ctypes.c_void_p
    kernel32.GlobalLock.argtypes = [ctypes.c_void_p]
    pointer = kernel32.GlobalLock(handle)
    ctypes.memmove(pointer, body, len(body))
    kernel32.GlobalUnlock.argtypes = [ctypes.c_void_p]
    kernel32.GlobalUnlock.restype = wintypes.BOOL
    kernel32.GlobalUnlock(handle)
    return handle


def main():
    target = Path(sys.argv[1] if len(sys.argv) > 1 else ROOT / ".probe" / "in" / "demo.pdf")
    if not target.exists():
        raise SystemExit(f"测试文件不存在：{target}")

    app.enable_dpi_awareness()
    root = tk.Tk()
    window = app.App(root)
    root.update()

    result = {"total": 0}

    def run():
        hwnd = wintypes.HWND(root.winfo_id())
        handle = build_hdrop([str(target)])
        user32 = ctypes.windll.user32
        user32.PostMessageW.restype = wintypes.BOOL
        user32.PostMessageW.argtypes = [wintypes.HWND, wintypes.UINT, ctypes.c_size_t, ctypes.c_ssize_t]
        # 用 PostMessage 从工作线程投递，模拟 shell 的异步投递（窗口过程由消息泵派发）
        threading.Timer(0.15, lambda: user32.PostMessageW(hwnd, 0x0233, handle, 0)).start()
        root.after(700, check)

    def check():
        result["total"] = len(window.jobs)
        root.after(200, root.destroy)

    root.after(500, run)
    root.mainloop()

    print("jobs after drop:", result["total"])
    print("拖拽" + ("成功" if result["total"] == 1 else "失败"))
    return 0 if result["total"] == 1 else 1


if __name__ == "__main__":
    raise SystemExit(main())
