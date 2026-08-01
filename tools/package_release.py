#!/usr/bin/env python3
"""生成只包含技能运行时文件的可复现 ZIP。"""

import argparse
import os
import sys
import tempfile
import zipfile
from pathlib import Path


SKILL_NAME = "build-exam-anki"
RUNTIME_FILES = ("SKILL.md", "requirements.txt", "LICENSE")
RUNTIME_DIRECTORIES = ("agents", "assets", "references", "scripts")
FIXED_TIME = (2026, 1, 1, 0, 0, 0)


def read_version(root: Path) -> str:
    version = (root / "VERSION").read_text(encoding="utf-8").strip()
    parts = version.split(".")
    if len(parts) != 3 or not all(part.isdigit() for part in parts):
        raise ValueError("VERSION 必须是三段数字语义版本")
    return version


def runtime_paths(root: Path):
    for name in RUNTIME_FILES:
        path = root / name
        if path.is_symlink():
            raise ValueError("运行文件不得是符号链接：{}".format(name))
        if not path.is_file():
            raise ValueError("缺少运行文件：{}".format(name))
        yield path
    for directory_name in RUNTIME_DIRECTORIES:
        directory = root / directory_name
        if directory.is_symlink():
            raise ValueError("运行目录不得是符号链接：{}".format(directory_name))
        if not directory.is_dir():
            raise ValueError("缺少运行目录：{}".format(directory_name))
        for path in sorted(directory.rglob("*")):
            if path.is_symlink():
                raise ValueError(
                    "运行内容不得是符号链接：{}".format(path.relative_to(root))
                )
            if not path.is_file():
                continue
            if path.name == ".DS_Store" or path.suffix == ".pyc":
                continue
            if "__pycache__" in path.parts:
                continue
            yield path


def build_package(root: Path, output: Path, force: bool) -> Path:
    if output.exists() and not force:
        raise FileExistsError("发布包已存在；替换时请添加 --force：{}".format(output))
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        prefix=".release-", suffix=".zip", dir=str(output.parent), delete=False
    ) as temporary:
        temporary_path = Path(temporary.name)
    try:
        with zipfile.ZipFile(
            str(temporary_path), "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9
        ) as archive:
            for path in runtime_paths(root):
                relative = path.relative_to(root).as_posix()
                info = zipfile.ZipInfo("{}/{}".format(SKILL_NAME, relative), FIXED_TIME)
                info.compress_type = zipfile.ZIP_DEFLATED
                info.external_attr = (0o755 if path.suffix == ".py" else 0o644) << 16
                archive.writestr(info, path.read_bytes())
        os.replace(str(temporary_path), str(output))
    except Exception:
        if temporary_path.exists():
            temporary_path.unlink()
        raise
    return output


def main() -> int:
    parser = argparse.ArgumentParser(description="构建 build-exam-anki 发布压缩包")
    parser.add_argument("--output", type=Path, help="自定义 ZIP 路径")
    parser.add_argument("--force", action="store_true", help="替换已有发布包")
    arguments = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    try:
        version = read_version(root)
        output = arguments.output or root / "dist" / "{}-v{}.zip".format(
            SKILL_NAME, version
        )
        result = build_package(root, output.expanduser().absolute(), arguments.force)
    except (OSError, ValueError) as error:
        print("发布包构建失败：{}".format(error), file=sys.stderr)
        return 1
    print("发布包已生成：{}".format(result))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
