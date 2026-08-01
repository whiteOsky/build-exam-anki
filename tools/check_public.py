#!/usr/bin/env python3
"""检查公开仓库结构、敏感路径、常见令牌和大文件。"""

import re
import sys
from pathlib import Path


REQUIRED = (
    "SKILL.md",
    "README.md",
    "LICENSE",
    "VERSION",
    "requirements.txt",
    "agents/openai.yaml",
)
IGNORED_PARTS = {".git", ".venv", "__pycache__", "dist"}
FORBIDDEN_TEXT = {
    "/Users/": "macOS 用户绝对路径",
    "Mobile Documents": "iCloud 本地路径",
    "com~apple~CloudDocs": "iCloud 容器路径",
    "BEGIN OPENSSH PRIVATE KEY": "SSH 私钥",
    "BEGIN RSA PRIVATE KEY": "RSA 私钥",
    "BEGIN PRIVATE KEY": "私钥",
}
TOKEN_PATTERNS = (
    re.compile(r"github_pat_[A-Za-z0-9_]{20,}"),
    re.compile(r"gh[pousr]_[A-Za-z0-9]{30,}"),
    re.compile(r"sk-[A-Za-z0-9_-]{30,}"),
    re.compile(r"AKIA[0-9A-Z]{16}"),
    re.compile(r"xox[baprs]-[A-Za-z0-9-]{20,}"),
    re.compile(r"glpat-[A-Za-z0-9_-]{20,}"),
    re.compile(r"npm_[A-Za-z0-9]{30,}"),
    re.compile(r"AIza[0-9A-Za-z_-]{30,}"),
)
MAX_PUBLIC_FILE = 2 * 1024 * 1024


def iter_public_files(root: Path):
    for path in sorted(root.rglob("*")):
        if not path.is_file() or any(part in IGNORED_PARTS for part in path.parts):
            continue
        if path.relative_to(root).as_posix() == "tools/check_public.py":
            continue
        yield path


def check_repository(root: Path):
    errors = []
    for relative in REQUIRED:
        if not (root / relative).is_file():
            errors.append("缺少发布必需文件：{}".format(relative))

    skill = root / "SKILL.md"
    if skill.is_file():
        text = skill.read_text(encoding="utf-8")
        if not text.startswith("---\nname: build-exam-anki\n"):
            errors.append("SKILL.md 根入口或名称不符合安装契约")

    for path in root.rglob("*"):
        if path.is_symlink() and not any(part in IGNORED_PARTS for part in path.parts):
            errors.append("公开仓库不得包含符号链接：{}".format(path.relative_to(root)))

    for path in iter_public_files(root):
        relative = path.relative_to(root)
        if path.stat().st_size > MAX_PUBLIC_FILE:
            errors.append("公开文件超过 2 MiB：{}".format(relative))
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue
        for value, label in FORBIDDEN_TEXT.items():
            if value in text:
                errors.append("{} 包含{}".format(relative, label))
        for pattern in TOKEN_PATTERNS:
            if pattern.search(text):
                errors.append("{} 疑似包含访问令牌".format(relative))
    return errors


def main() -> int:
    root = Path(__file__).resolve().parents[1]
    errors = check_repository(root)
    if errors:
        print("公开内容检查失败：", file=sys.stderr)
        for error in errors:
            print("- {}".format(error), file=sys.stderr)
        return 1
    print("公开内容检查通过：未发现个人绝对路径、常见令牌或异常大文件。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
