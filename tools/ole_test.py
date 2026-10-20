"""验证 OLE DropTarget 的 vtable 解析与路径提取。

构造一个最小的 IDataObject（GetData 返回 CF_HDROP），直接调用 DropTarget._drop，
等价于 OLE 在真实拖放时对目标对象的调用。
"""

import ctypes
import sys
import tkinter as tk
from ctypes import wintypes
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "tools"))

import dnd  # noqa: E402
from drop_test import build_hdrop  # noqa: E402

TARGET = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("sample.pdf")


def make_data_object(hdrop: int):
    HRESULT = ctypes.c_long
    GENERIC = ctypes.WINFUNCTYPE(HRESULT, ctypes.c_void_p)
    GETDATA = ctypes.WINFUNCTYPE(HRESULT, ctypes.c_void_p,
                                 ctypes.POINTER(dnd.FORMATETC), ctypes.POINTER(dnd.STGMEDIUM))

    def get_data(this, format_etc, medium):
        if format_etc.contents.cfFormat != dnd.CF_HDROP:
            return -2147467259
        medium.contents.tymEd = dnd.TYMED_HGLOBAL
        medium.contents.hGlobal = hdrop
        return 0

    def query_get_data(this, format_etc):
        return 0 if format_etc.contents.cfFormat == dnd.CF_HDROP else -2147467259

    callbacks = []
    callbacks.append(GENERIC(lambda this: 0))              # QueryInterface
    callbacks.append(GENERIC(lambda this: 1))              # AddRef
    callbacks.append(GENERIC(lambda this: 0))              # Release
    callbacks.append(GETDATA(get_data))                    # GetData
    callbacks.append(GENERIC(lambda this: -2147467263))    # GetDataHere
    callbacks.append(ctypes.WINFUNCTYPE(HRESULT, ctypes.c_void_p,
                                        ctypes.POINTER(dnd.FORMATETC))(query_get_data))
    for _ in range(6):
        callbacks.append(GENERIC(lambda this: -2147467263))

    array_type = ctypes.c_void_p * len(callbacks)
    vtable = array_type(*[ctypes.cast(fn, ctypes.c_void_p) for fn in callbacks])

    class FakeObject(ctypes.Structure):
        _fields_ = [("lpVtbl", ctypes.c_void_p)]

    obj = FakeObject(ctypes.cast(vtable, ctypes.c_void_p))
    return obj, callbacks, vtable


def main():
    root = tk.Tk()
    root.withdraw()

    received: list[list[str]] = []
    target = dnd.DropTarget(root.winfo_id(), lambda paths: received.append(list(paths)),
                            log=lambda m: print("log:", m))
    print("RegisterDragDrop ok")

    handle = build_hdrop([str(TARGET)])
    obj, callbacks, vtable = make_data_object(handle)
    effect = ctypes.c_ulong(0)
    hr = target._drop(None, ctypes.cast(ctypes.byref(obj), ctypes.c_void_p),
                      0, dnd.POINTL(0, 0), ctypes.pointer(effect))
    print("drop hr:", hr, "effect:", effect.value)
    print("received:", received)

    target.close()
    root.destroy()

    ok = received and received[0] and received[0][0].endswith("demo.pdf")
    print("OLE 数据路径" + ("正确" if ok else "异常"))
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
