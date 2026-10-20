"""打包为单文件 exe。

用法（在装有 pyinstaller 的解释器下运行）：
    python build.py
产物：dist/MinerU Lite.exe
"""

import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
APP_NAME = "MinerU Lite"
VERSION = (1, 0, 0, 0)

VERSION_TEMPLATE = """VSVersionInfo(
  ffi=FixedFileInfo(
    filevers={version}, prodvers={version},
    mask=0x3f, flags=0x0, OS=0x40004, fileType=0x1, subtype=0x0, date=(0, 0)
  ),
  kids=[
    StringFileInfo([
      StringTable('080404b0', [
        StringStruct('CompanyName', ''),
        StringStruct('FileDescription', 'MinerU 文档转换工具'),
        StringStruct('FileVersion', '{version_text}'),
        StringStruct('InternalName', 'MinerU Lite'),
        StringStruct('OriginalFilename', 'MinerU Lite.exe'),
        StringStruct('ProductName', 'MinerU Lite'),
        StringStruct('ProductVersion', '{version_text}')
      ])
    ]),
    VarFileInfo([VarStruct('Translation', [2052, 1200])])
  ]
)
"""


def main() -> int:
    # 在虚拟环境中构建时，Tcl/Tk 库目录不在 venv 下，需要显式告知
    base = Path(sys.base_prefix)
    for name, sub in (("TCL_LIBRARY", "tcl8.6"), ("TK_LIBRARY", "tk8.6")):
        candidate = base / "tcl" / sub
        if candidate.is_dir():
            os.environ.setdefault(name, str(candidate))

    icon = ROOT / "assets" / "icon.ico"
    if not icon.exists():
        subprocess.run([sys.executable, str(ROOT / "tools" / "make_icon.py")], check=True)

    version_file = ROOT / "build" / "version_info.txt"
    version_file.parent.mkdir(parents=True, exist_ok=True)
    version_file.write_text(
        VERSION_TEMPLATE.format(version=VERSION, version_text=".".join(map(str, VERSION))),
        encoding="utf-8",
    )

    args = [
        sys.executable, "-m", "PyInstaller",
        "--noconfirm", "--clean", "--onefile", "--windowed",
        "--name", APP_NAME,
        "--icon", str(icon),
        "--add-data", f"{icon}{os.pathsep}assets",
        "--version-file", str(version_file),
        "--distpath", str(ROOT / "dist"),
        "--workpath", str(ROOT / "build"),
        "--specpath", str(ROOT / "build"),
        "--exclude-module", "unittest",
        "--exclude-module", "pydoc",
        "--exclude-module", "test",
        "--exclude-module", "numpy",
        "--exclude-module", "PIL",
        "--log-level", "WARN",
        str(ROOT / "src" / "app.py"),
    ]

    print("building...")
    result = subprocess.run(args, cwd=ROOT)
    if result.returncode != 0:
        return result.returncode

    exe = ROOT / "dist" / f"{APP_NAME}.exe"
    if exe.exists():
        print(f"done: {exe}  ({exe.stat().st_size / 1024 / 1024:.1f} MB)")
    else:
        print("build finished but exe not found")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
