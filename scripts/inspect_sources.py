"""递归检查考试制卡项目中的原始来源文件。"""

import json
import os
import re
import sys
import tempfile
from pathlib import Path
from typing import Dict, Optional, Tuple, Union

try:
    from .common import (
        ChineseArgumentParser,
        atomic_write_text,
        ensure_safe_output_path,
        fsync_directory,
    )
except ImportError:
    from common import (
        ChineseArgumentParser,
        atomic_write_text,
        ensure_safe_output_path,
        fsync_directory,
    )


PathLike = Union[str, Path]

SOURCE_TYPES = {
    ".pdf": "PDF 文档",
    ".md": "Markdown 文档",
    ".pptx": "PowerPoint 文档",
    ".docx": "Word 文档",
    ".tsv": "TSV 卡片",
    ".apkg": "Anki 卡包",
}

GENERATED_DIRECTORIES = {
    "cleaned",
    "notes",
    "cards",
    "reports",
    "print",
    "assets",
    "build",
    "__pycache__",
}

CHAPTER_PATTERN = re.compile(
    r"第\s*(?:\d+|[一二三四五六七八九十百]+)\s*[章讲节]",
    re.IGNORECASE,
)


def _is_excluded(relative_path: Path) -> bool:
    for part in relative_path.parts:
        if part.startswith("."):
            return True
    return bool(
        relative_path.parts
        and relative_path.parts[0] in GENERATED_DIRECTORIES
    )


def _infer_chapter(relative_path: Path) -> str:
    match = CHAPTER_PATTERN.search(relative_path.as_posix())
    if not match:
        return "未识别"
    return re.sub(r"\s+", "", match.group(0))


def inspect_sources(root: PathLike) -> Dict[str, object]:
    """扫描受支持的来源类型并返回稳定排序的清单。"""
    project_root = Path(root).expanduser().resolve()
    if not project_root.is_dir():
        raise ValueError("根目录不存在或不是目录：{}".format(project_root))

    files = []
    for path in project_root.rglob("*"):
        if not path.is_file():
            continue
        relative_path = path.relative_to(project_root)
        if _is_excluded(relative_path):
            continue
        suffix = path.suffix.lower()
        if suffix not in SOURCE_TYPES:
            continue
        files.append(
            {
                "路径": relative_path.as_posix(),
                "类型": SOURCE_TYPES[suffix],
                "字节数": path.stat().st_size,
                "推测章节": _infer_chapter(relative_path),
            }
        )

    files.sort(key=lambda item: str(item["路径"]).casefold())
    return {"根目录": str(project_root), "文件": files}


def write_source_report(
    root: PathLike,
    inventory: Dict[str, object],
    report_path: Optional[PathLike] = None,
) -> Path:
    """将来源清单安全写入 Markdown 报告。"""
    project_root = Path(root).expanduser().resolve()
    destination = (
        Path(report_path)
        if report_path is not None
        else project_root / "reports/来源清单.md"
    )
    destination = ensure_safe_output_path(project_root, destination)

    return atomic_write_text(
        destination,
        _format_source_report(inventory),
        root=project_root,
    )


def _format_source_report(inventory: Dict[str, object]) -> str:
    lines = [
        "# 来源清单",
        "",
        "根目录：{}".format(inventory["根目录"]),
        "",
        "| 路径 | 类型 | 字节数 | 推测章节 |",
        "| --- | --- | ---: | --- |",
    ]
    files = inventory.get("文件", [])
    if isinstance(files, list):
        for item in files:
            escaped = {
                key: _escape_markdown_cell(value)
                for key, value in item.items()
            }
            lines.append(
                "| {路径} | {类型} | {字节数} | {推测章节} |".format(
                    **escaped
                )
            )
    return "\n".join(lines) + "\n"


def _escape_markdown_cell(value: object) -> str:
    text = str(value).replace("\r\n", "\n").replace("\r", "\n")
    return text.replace("|", r"\|").replace("\n", "<br>")


def _fixed_output_path(
    project_root: Path,
    path: PathLike,
    directory_name: str,
    option_name: str,
) -> Path:
    requested = Path(path).expanduser()
    if not requested.is_absolute():
        requested = project_root / requested
    unresolved = Path(os.path.abspath(str(requested)))
    fixed_root = project_root / directory_name
    current = unresolved
    reached_project = False
    while True:
        if current.is_symlink():
            raise ValueError(
                "{} 输出路径不得包含符号链接：{}".format(
                    option_name, current
                )
            )
        if current.exists() and _same_file(current, project_root):
            reached_project = True
            break
        if current.parent == current:
            break
        current = current.parent

    destination = unresolved.resolve(strict=False)
    try:
        destination.relative_to(fixed_root.resolve(strict=False))
    except ValueError:
        raise ValueError(
            "{} 只允许写入项目 {}/ 目录：{}".format(
                option_name, directory_name, destination
            )
        )
    if not reached_project or destination == fixed_root.resolve(strict=False):
        raise ValueError("{} 必须指定输出文件，不能是目录".format(option_name))
    try:
        destination.resolve(strict=False).relative_to(
            fixed_root.resolve(strict=False)
        )
    except ValueError:
        raise ValueError(
            "{} 输出路径包含符号链接或越出项目 {}/ 目录：{}".format(
                option_name, directory_name, destination
            )
        )
    return destination


def _same_file(first: Path, second: Path) -> bool:
    try:
        return os.path.samefile(str(first), str(second))
    except FileNotFoundError:
        return False


def _validate_output_links(
    project_root: Path,
    inventory: Dict[str, object],
    json_path: Path,
    report_path: Path,
) -> None:
    for label, destination in (("--json", json_path), ("--report", report_path)):
        if destination.is_symlink():
            raise ValueError("{} 输出不得使用符号链接：{}".format(label, destination))
        if destination.exists() and not destination.is_file():
            raise ValueError("{} 输出必须是普通文件：{}".format(label, destination))

    if _same_file(json_path, report_path):
        raise ValueError("--json 与 --report 不得指向同一文件或硬链接")

    files = inventory.get("文件", [])
    if not isinstance(files, list):
        raise ValueError("来源清单中的文件列表无效")
    for item in files:
        if not isinstance(item, dict) or "路径" not in item:
            raise ValueError("来源清单中的文件记录无效")
        source = project_root / str(item["路径"])
        for label, destination in (("--json", json_path), ("--report", report_path)):
            if _same_file(source, destination):
                raise ValueError(
                    "{} 输出不得通过硬链接指向来源文件：{}".format(
                        label, source
                    )
                )


def _stage_text(destination: Path, content: str) -> Path:
    destination.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        dir=str(destination.parent),
        prefix=".{}.tmp-".format(destination.name),
        text=True,
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
    except BaseException:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass
        raise
    return temporary


def _reserve_backup_path(destination: Path) -> Path:
    descriptor, backup_name = tempfile.mkstemp(
        dir=str(destination.parent),
        prefix=".{}.bak-".format(destination.name),
    )
    os.close(descriptor)
    backup = Path(backup_name)
    backup.unlink()
    return backup


def _remove_if_present(path: Path) -> None:
    try:
        path.unlink()
    except FileNotFoundError:
        pass


def _commit_outputs(
    pairs: Tuple[Tuple[Path, Path], Tuple[Path, Path]],
    force: bool,
) -> None:
    backups = {}
    committed = []
    try:
        if force:
            for destination, _ in pairs:
                if destination.exists():
                    backup = _reserve_backup_path(destination)
                    os.replace(str(destination), str(backup))
                    backups[destination] = backup
        else:
            existing = [
                destination
                for destination, _ in pairs
                if os.path.lexists(str(destination))
            ]
            if existing:
                raise FileExistsError(
                    "来源清单输出已存在，默认不覆盖；请明确使用 --force：{}".format(
                        "、".join(str(path) for path in existing)
                    )
                )

        for destination, staged in pairs:
            if force:
                os.replace(str(staged), str(destination))
            else:
                try:
                    os.link(str(staged), str(destination))
                except FileExistsError:
                    raise FileExistsError(
                        "来源清单输出已存在，默认不覆盖；请明确使用 --force：{}".format(
                            destination
                        )
                    ) from None
                staged.unlink()
            committed.append(destination)
        for parent in {destination.parent for destination, _ in pairs}:
            fsync_directory(parent)
    except BaseException as error:
        rollback_errors = []
        for destination in reversed(committed):
            try:
                _remove_if_present(destination)
            except OSError as rollback_error:
                rollback_errors.append(str(rollback_error))
        for destination, backup in backups.items():
            try:
                if backup.exists():
                    os.replace(str(backup), str(destination))
            except OSError as rollback_error:
                rollback_errors.append(str(rollback_error))
        if rollback_errors:
            raise RuntimeError(
                "来源清单双文件事务失败且回滚不完整：{}；原始错误：{}".format(
                    "；".join(rollback_errors), error
                )
            )
        raise
    finally:
        for _, staged in pairs:
            _remove_if_present(staged)
        for backup in backups.values():
            _remove_if_present(backup)


def write_source_outputs(
    root: PathLike,
    inventory: Dict[str, object],
    json_path: PathLike,
    report_path: PathLike,
    force: bool = False,
) -> Tuple[Path, Path]:
    """把 JSON 与 Markdown 清单作为一个事务提交。"""
    project_root = Path(root).expanduser().resolve()
    if not project_root.is_dir():
        raise ValueError("根目录不存在或不是目录：{}".format(project_root))
    json_destination = _fixed_output_path(
        project_root, json_path, "build", "--json"
    )
    report_destination = _fixed_output_path(
        project_root, report_path, "reports", "--report"
    )
    _validate_output_links(
        project_root, inventory, json_destination, report_destination
    )
    if not force:
        existing = [
            path
            for path in (json_destination, report_destination)
            if os.path.lexists(str(path))
        ]
        if existing:
            raise FileExistsError(
                "来源清单输出已存在，默认不覆盖；请明确使用 --force：{}".format(
                    "、".join(str(path) for path in existing)
                )
            )

    try:
        json_content = json.dumps(inventory, ensure_ascii=False, indent=2) + "\n"
    except (TypeError, ValueError):
        raise ValueError("JSON 写入失败：数据无法序列化")
    staged_json = _stage_text(json_destination, json_content)
    try:
        staged_report = _stage_text(
            report_destination, _format_source_report(inventory)
        )
    except BaseException:
        _remove_if_present(staged_json)
        raise

    _validate_output_links(
        project_root, inventory, json_destination, report_destination
    )
    _commit_outputs(
        (
            (json_destination, staged_json),
            (report_destination, staged_report),
        ),
        force,
    )
    return json_destination, report_destination


def main() -> int:
    parser = ChineseArgumentParser(description="扫描来源并生成来源清单")
    parser.add_argument("目标目录", help="待扫描的项目根目录")
    parser.add_argument("--json", required=True, help="JSON 清单输出路径")
    parser.add_argument("--report", required=True, help="Markdown 报告输出路径")
    parser.add_argument(
        "--force", action="store_true", help="明确允许原子替换已有双份清单"
    )
    arguments = parser.parse_args()
    root = getattr(arguments, "目标目录")
    try:
        inventory = inspect_sources(root)
        project_root = Path(root).expanduser().resolve()
        write_source_outputs(
            project_root,
            inventory,
            arguments.json,
            arguments.report,
            force=arguments.force,
        )
    except (OSError, RuntimeError, ValueError) as error:
        print("来源检查失败：{}".format(error), file=sys.stderr)
        return 1
    print("来源检查完成：{}".format(project_root))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
