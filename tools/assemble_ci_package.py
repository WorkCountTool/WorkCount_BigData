#!/usr/bin/env python3
"""Add private workbook templates to the Windows CI-built archive."""

import argparse
import zipfile
from pathlib import Path

PROJECT = Path(__file__).resolve().parent.parent
TEMPLATES = (
    "个人学时统计表模板.xlsx",
    "决算工作量表模板.xlsx",
    "张子豪-表1：人工智能学院（部）2026-2027学年第一学期工作量预算汇总表.xlsx",
)


def assemble(source: Path, platform: str) -> Path:
    root = f"WorkCount-{platform}"
    destination = PROJECT / "release" / f"{root}.zip"
    if destination.exists():
        raise FileExistsError(f"交付压缩包已存在：{destination}")
    for name in TEMPLATES:
        if not (PROJECT / "templates" / name).is_file():
            raise FileNotFoundError(f"缺少模板：{name}")
    with zipfile.ZipFile(source) as package:
        entries = package.infolist()
        if not entries or any(not entry.filename.startswith(root + "/") or ".." in Path(entry.filename).parts for entry in entries):
            raise ValueError("构建压缩包的根目录不符")
        expected = "WorkCountServer/WorkCountServer.exe"
        if root + "/" + expected not in package.namelist():
            raise ValueError("构建压缩包缺少主程序")
        with zipfile.ZipFile(destination, "w") as output:
            for entry in entries:
                output.writestr(entry, package.read(entry))
            for name in TEMPLATES:
                output.write(PROJECT / "templates" / name, f"{root}/templates/{name}", compress_type=zipfile.ZIP_DEFLATED)
    return destination


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("archive", type=Path, help="GitHub Actions 下载的不含模板 ZIP")
    parser.add_argument("platform", choices=("Windows-x64",))
    args = parser.parse_args()
    print(assemble(args.archive, args.platform))
