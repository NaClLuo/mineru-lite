"""OLE 拖放目标（IDropTarget）。

替代 WM_DROPFILES + 窗口过程子类化：
- 不改写 Tk 的窗口过程，避免破坏 Tk 的消息分发；
- 文件列表由 OLE 通过 IDataObject 送进本进程，不依赖跨进程共享内存；
- 回调里只把路径写入队列，绝不调用 Tk，由主循环消费。
"""

from __future__ import annotations

import ctypes
import sys
from ctypes import wintypes

HRESULT = ctypes.c_long
DWORD = ctypes.c_ulong
LONG = ctypes.c_long

CF_HDROP = 15
DVASPECT_CONTENT = 1
TYMED_HGLOBAL = 1
DROPEFFECT_NONE = 0
DROPEFFECT_COPY = 1

S_OK = 0


class FORMATETC(ctypes.Structure):
    _fields_ = [
        ("cfFormat", ctypes.c_ushort),
        ("ptd", ctypes.c_void_p),
        ("dwAspect", DWORD),
        ("lindex", LONG),
        ("tymed", DWORD),
    ]


class _STGMEDIUM_UNION(ctypes.Union):
    _fields_ = [
        ("hGlobal", ctypes.c_void_p),
        ("pstm", ctypes.c_void_p),
        ("pstg", ctypes.c_void_p),
    ]


class STGMEDIUM(ctypes.Structure):
    _anonymous_ = ("u",)
    _fields_ = [
        ("tymEd", DWORD),
        ("u", _STGMEDIUM_UNION),
        ("pUnkForRelease", ctypes.c_void_p),
    ]


class POINTL(ctypes.Structure):
    _fields_ = [("x", LONG), ("y", LONG)]


_QI_FN = ctypes.WINFUNCTYPE(HRESULT, ctypes.c_void_p, ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p))
_REF_FN = ctypes.WINFUNCTYPE(DWORD, ctypes.c_void_p)
_ENTER_FN = ctypes.WINFUNCTYPE(HRESULT, ctypes.c_void_p, ctypes.c_void_p, DWORD,
                               POINTL, ctypes.POINTER(DWORD))
_OVER_FN = ctypes.WINFUNCTYPE(HRESULT, ctypes.c_void_p, DWORD, POINTL, ctypes.POINTER(DWORD))
_LEAVE_FN = ctypes.WINFUNCTYPE(HRESULT, ctypes.c_void_p)
_DROP_FN = ctypes.WINFUNCTYPE(HRESULT, ctypes.c_void_p, ctypes.c_void_p, DWORD,
                              POINTL, ctypes.POINTER(DWORD))
_GETDATA_FN = ctypes.WINFUNCTYPE(HRESULT, ctypes.c_void_p,
                                 ctypes.POINTER(FORMATETC), ctypes.POINTER(STGMEDIUM))
_QUERYGETDATA_FN = ctypes.WINFUNCTYPE(HRESULT, ctypes.c_void_p, ctypes.POINTER(FORMATETC))


class _DropTargetVtbl(ctypes.Structure):
    _fields_ = [
        ("QueryInterface", _QI_FN),
        ("AddRef", _REF_FN),
        ("Release", _REF_FN),
        ("DragEnter", _ENTER_FN),
        ("DragOver", _OVER_FN),
        ("DragLeave", _LEAVE_FN),
        ("Drop", _DROP_FN),
    ]


class _DropTargetObject(ctypes.Structure):
    _fields_ = [("lpVtbl", ctypes.POINTER(_DropTargetVtbl))]


def _vtable_func(idata_object: ctypes.c_void_p, index: int, prototype):
    """从 IDataObject 的虚表里取第 index 个方法。"""
    table = ctypes.cast(idata_object, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p))).contents
    return prototype(table[index])


class DropTarget:
    """把窗口注册成文件拖放目标。"""

    def __init__(self, hwnd: int, on_files, log=None):
        if sys.platform != "win32":
            raise OSError("仅支持 Windows")

        self._on_files = on_files
        self._log = log
        self._hwnd = hwnd
        self._refs = 1
        self._ole32 = ctypes.windll.ole32
        self._shell32 = ctypes.windll.shell32
        self._registered = False

        self._ole32.OleInitialize.argtypes = [ctypes.c_void_p]
        self._ole32.RegisterDragDrop.argtypes = [wintypes.HWND, ctypes.POINTER(_DropTargetObject)]
        self._ole32.RegisterDragDrop.restype = HRESULT
        self._ole32.RevokeDragDrop.argtypes = [wintypes.HWND]
        self._ole32.ReleaseStgMedium.argtypes = [ctypes.POINTER(STGMEDIUM)]
        self._shell32.DragQueryFileW.argtypes = [wintypes.HANDLE, wintypes.UINT,
                                                 wintypes.LPWSTR, wintypes.UINT]
        self._shell32.DragQueryFileW.restype = wintypes.UINT
        self._shell32.DragFinish.argtypes = [wintypes.HANDLE]

        # STA：Tk 主线程本来没有初始化 COM，这里补上（重复初始化只会返回 S_FALSE）
        self._ole32.OleInitialize(None)

        self._vtbl = _DropTargetVtbl(
            _QI_FN(self._query_interface),
            _REF_FN(self._add_ref),
            _REF_FN(self._release),
            _ENTER_FN(self._drag_enter),
            _OVER_FN(self._drag_over),
            _LEAVE_FN(self._drag_leave),
            _DROP_FN(self._drop),
        )
        self._object = _DropTargetObject()
        self._object.lpVtbl = ctypes.pointer(self._vtbl)

        result = self._ole32.RegisterDragDrop(wintypes.HWND(hwnd),
                                              ctypes.byref(self._object))
        if result != S_OK:
            raise OSError(f"RegisterDragDrop 失败：0x{result & 0xFFFFFFFF:08X}")
        self._registered = True

    # ------------------------------------------------------------ IUnknown
    def _query_interface(self, this, riid, out):
        try:
            if out:
                iid = bytes((ctypes.c_ubyte * 16).from_address(riid))
                # IID_IUnknown / IID_IDropTarget 的最后一组都是 000000000046
                if iid[8:] == b"\xc0\x00\x00\x00\x00\x00\x00\x46" and (
                    iid[:4] in (b"\x00\x00\x00\x00", b"\x22\x01\x00\x00")
                ):
                    out[0] = this
                    return S_OK
                out[0] = None
        except Exception:
            if out:
                out[0] = None
        return -2147467262  # E_NOINTERFACE

    def _add_ref(self, this):
        self._refs += 1
        return self._refs

    def _release(self, this):
        self._refs -= 1
        return self._refs

    # ------------------------------------------------------------ 拖放回调
    def _has_files(self, data_object) -> bool:
        if not data_object:
            return False
        try:
            format_etc = FORMATETC(CF_HDROP, None, DVASPECT_CONTENT, -1, TYMED_HGLOBAL)
            query = _vtable_func(data_object, 5, _QUERYGETDATA_FN)
            return query(data_object, ctypes.byref(format_etc)) == S_OK
        except Exception:
            return False

    def _drag_enter(self, this, data_object, key_state, point, effect):
        try:
            if effect:
                effect[0] = DROPEFFECT_COPY if self._has_files(data_object) else DROPEFFECT_NONE
        except Exception as exc:
            if self._log:
                self._log(f"[dnd] enter {exc!r}")
        return S_OK

    def _drag_over(self, this, key_state, point, effect):
        try:
            if effect and effect[0] != DROPEFFECT_COPY:
                effect[0] = DROPEFFECT_COPY
        except Exception:
            pass
        return S_OK

    def _drag_leave(self, this):
        return S_OK

    def _drop(self, this, data_object, key_state, point, effect):
        paths: list[str] = []
        try:
            if effect:
                effect[0] = DROPEFFECT_NONE
            if data_object:
                format_etc = FORMATETC(CF_HDROP, None, DVASPECT_CONTENT, -1, TYMED_HGLOBAL)
                medium = STGMEDIUM()
                get_data = _vtable_func(data_object, 3, _GETDATA_FN)
                if get_data(data_object, ctypes.byref(format_etc), ctypes.byref(medium)) == S_OK:
                    handle = ctypes.c_void_p(medium.hGlobal) if medium.hGlobal else None
                    if handle:
                        paths = self._read_paths(handle)
                    self._ole32.ReleaseStgMedium(ctypes.byref(medium))
                    if paths:
                        self._on_files(paths)
                        if effect:
                            effect[0] = DROPEFFECT_COPY
        except Exception as exc:
            if self._log:
                self._log(f"[dnd] drop {exc!r}")
        return S_OK

    def _read_paths(self, handle) -> list[str]:
        count = int(self._shell32.DragQueryFileW(handle, 0xFFFFFFFF, None, 0))
        if count <= 0:
            return []
        paths: list[str] = []
        for index in range(count):
            length = int(self._shell32.DragQueryFileW(handle, index, None, 0))
            if length <= 0:
                continue
            buffer = ctypes.create_unicode_buffer(length + 1)
            self._shell32.DragQueryFileW(handle, index, buffer, length + 1)
            paths.append(buffer.value)
        self._shell32.DragFinish(handle)
        return paths

    def close(self):
        if not self._registered:
            return
        try:
            self._ole32.RevokeDragDrop(wintypes.HWND(self._hwnd))
        except Exception:
            pass
        self._registered = False
