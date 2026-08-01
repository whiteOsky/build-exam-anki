#!/usr/bin/env python3
"""把仓库中的运行时技能安全安装到 Codex 技能目录。"""

import argparse
import os
import re
import shutil
import subprocess
import sys
import tempfile
import uuid
from pathlib import Path


SKILL_NAME = "build-exam-anki"
RUNTIME_FILES = ("SKILL.md", "requirements.txt", "LICENSE")
RUNTIME_DIRECTORIES = ("agents", "assets", "references", "scripts")
SKILL_NAME_PATTERN = re.compile(r"(?m)^name:\s*build-exam-anki\s*$")


def default_target() -> Path:
    codex_home = Path(os.environ.get("CODEX_HOME", Path.home() / ".codex"))
    return codex_home / "skills" / SKILL_NAME


def validate_runtime_source(source: Path) -> None:
    for name in RUNTIME_FILES:
        item = source / name
        if item.is_symlink():
            raise ValueError("运行文件不得是符号链接：{}".format(name))
        if not item.is_file():
            raise ValueError("发布仓库缺少运行文件：{}".format(name))
    for name in RUNTIME_DIRECTORIES:
        item = source / name
        if item.is_symlink():
            raise ValueError("运行目录不得是符号链接：{}".format(name))
        if not item.is_dir():
            raise ValueError("发布仓库缺少运行目录：{}".format(name))
        for current, directories, files in os.walk(str(item), followlinks=False):
            for child_name in directories + files:
                child = Path(current) / child_name
                if child.is_symlink():
                    raise ValueError(
                        "运行内容不得是符号链接：{}".format(
                            child.relative_to(source)
                        )
                    )
                if child_name in files and not child.is_file():
                    raise ValueError(
                        "运行内容必须是普通文件：{}".format(
                            child.relative_to(source)
                        )
                    )


def copy_runtime(source: Path, destination: Path) -> None:
    validate_runtime_source(source)
    destination.mkdir(parents=True)
    for name in RUNTIME_FILES:
        shutil.copy2(str(source / name), str(destination / name))
    for name in RUNTIME_DIRECTORIES:
        item = source / name
        shutil.copytree(
            str(item),
            str(destination / name),
            ignore=shutil.ignore_patterns("__pycache__", "*.pyc", ".DS_Store"),
        )


def is_existing_skill_install(target: Path) -> bool:
    marker = target / "SKILL.md"
    if not target.is_dir() or marker.is_symlink() or not marker.is_file():
        return False
    try:
        content = marker.read_text(encoding="utf-8")
    except (OSError, UnicodeError):
        return False
    return bool(SKILL_NAME_PATTERN.search(content))


def install_dependencies(staged_skill: Path) -> None:
    environment = staged_skill / ".venv"
    subprocess.run([sys.executable, "-m", "venv", str(environment)], check=True)
    python = environment / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    subprocess.run(
        [
            str(python),
            "-m",
            "pip",
            "install",
            "--disable-pip-version-check",
            "-r",
            str(staged_skill / "requirements.txt"),
        ],
        check=True,
    )


def install(source: Path, target: Path, force: bool, skip_dependencies: bool) -> None:
    source = source.resolve()
    target = target.expanduser().absolute()
    if target.name != SKILL_NAME or target.parent.name != "skills":
        raise ValueError(
            "安装目标必须是某个 skills 目录下的 build-exam-anki"
        )
    if target.parent.is_symlink():
        raise ValueError("安装目标的 skills 目录不得是符号链接")
    if target.is_symlink():
        raise ValueError("安装目标不得是符号链接：{}".format(target))
    if target.exists() and not force:
        raise FileExistsError("目标已存在；升级时请显式添加 --force：{}".format(target))
    if target.exists() and force and not is_existing_skill_install(target):
        raise ValueError("拒绝替换无法证明属于 build-exam-anki 的目录")
    if target.exists() and target.resolve() == source:
        raise ValueError("安装目标不能与发布仓库本身相同")

    target.parent.mkdir(parents=True, exist_ok=True)
    backup = target.with_name(".{}-backup-{}".format(SKILL_NAME, uuid.uuid4().hex))
    with tempfile.TemporaryDirectory(
        prefix=".{}-install-".format(SKILL_NAME), dir=str(target.parent)
    ) as temporary_name:
        staged = Path(temporary_name) / SKILL_NAME
        copy_runtime(source, staged)
        if not skip_dependencies:
            install_dependencies(staged)

        moved_old = False
        try:
            if target.exists():
                os.replace(str(target), str(backup))
                moved_old = True
            os.replace(str(staged), str(target))
        except Exception:
            if moved_old and backup.exists() and not target.exists():
                os.replace(str(backup), str(target))
            raise
        if moved_old:
            shutil.rmtree(str(backup))


def main() -> int:
    parser = argparse.ArgumentParser(description="安装 build-exam-anki Codex 技能")
    parser.add_argument("--target", type=Path, default=default_target(), help="技能安装目录")
    parser.add_argument("--force", action="store_true", help="原子替换已有安装")
    parser.add_argument(
        "--skip-deps", action="store_true", help="仅复制技能，不创建虚拟环境"
    )
    arguments = parser.parse_args()
    source = Path(__file__).resolve().parents[1]
    try:
        install(source, arguments.target, arguments.force, arguments.skip_deps)
    except (OSError, ValueError, subprocess.CalledProcessError) as error:
        print("安装失败：{}".format(error), file=sys.stderr)
        return 1
    print("安装完成：{}".format(arguments.target.expanduser().absolute()))
    print("请新建 Codex 对话并说：调用 build-exam-anki 处理我的课程资料。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
