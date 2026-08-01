"""保守清理 OCR 文本中的明确噪声。"""

import json
import os
import re
import sys
import tempfile
from html.parser import HTMLParser
from pathlib import Path
from typing import Dict, Tuple, Union

try:
    from .common import (
        ChineseArgumentParser,
        fsync_directory,
        is_promotional_ad_line,
    )
except ImportError:
    from common import ChineseArgumentParser, fsync_directory, is_promotional_ad_line


PathLike = Union[str, Path]

MARKDOWN_IMAGE_PATTERN = re.compile(r"!\[[^\]]*\]\([^)]*\)")
URL_PATTERN = re.compile(
    r"(?:https?://|www\.)[A-Za-z0-9\-._~:/?#\[\]@!$&'()*+,;=%]+",
    re.IGNORECASE,
)
REMOVED_HTML_TAGS = {
    "img",
    "table",
    "thead",
    "tbody",
    "tfoot",
    "tr",
    "td",
    "th",
    "div",
}


class _OcrHtmlCleaner(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=False)
        self.parts = []
        self.removed_count = 0

    def handle_starttag(self, tag, attrs) -> None:
        lowered = tag.lower()
        if lowered in REMOVED_HTML_TAGS:
            self.removed_count += 1
            if lowered in {"div", "tr"}:
                self.parts.append("\n")
            return
        self.parts.append(self.get_starttag_text())

    def handle_startendtag(self, tag, attrs) -> None:
        if tag.lower() in REMOVED_HTML_TAGS:
            self.removed_count += 1
            return
        self.parts.append(self.get_starttag_text())

    def handle_endtag(self, tag) -> None:
        lowered = tag.lower()
        if lowered in REMOVED_HTML_TAGS:
            self.removed_count += 1
            if lowered in {"td", "th"}:
                self.parts.append("\t")
            elif lowered in {"div", "tr"}:
                self.parts.append("\n")
            return
        self.parts.append("</{}>".format(tag))

    def handle_data(self, data) -> None:
        self.parts.append(data)

    def handle_entityref(self, name) -> None:
        self.parts.append("&{};".format(name))

    def handle_charref(self, name) -> None:
        self.parts.append("&#{};".format(name))

    def handle_comment(self, data) -> None:
        self.parts.append("<!--{}-->".format(data))

    def handle_decl(self, decl) -> None:
        self.parts.append("<!{}>".format(decl))

    def result(self) -> str:
        return "".join(self.parts)


def _strip_target_html(text: str) -> Tuple[str, int]:
    target_tags = "|".join(sorted(REMOVED_HTML_TAGS))
    if not re.search(r"</?(?:{})\b".format(target_tags), text, re.IGNORECASE):
        return text, 0
    parser = _OcrHtmlCleaner()
    parser.feed(text)
    parser.close()
    return parser.result(), parser.removed_count


def _remove_url(match) -> str:
    candidate = match.group(0)
    suffix = ""
    while candidate and candidate[-1] in ".,;:!?":
        suffix = candidate[-1] + suffix
        candidate = candidate[:-1]
    for closing, opening in ((")", "("), ("]", "["), ("}", "{")):
        while (
            candidate.endswith(closing)
            and candidate.count(closing) > candidate.count(opening)
        ):
            suffix = closing + suffix
            candidate = candidate[:-1]
    return suffix


def clean_ocr(text: str) -> Tuple[str, Dict[str, int]]:
    """删除明确噪声，同时保留公式、题目、解析和提示性正文。"""
    html_cleaned, removed_html_count = _strip_target_html(text)
    record = {
        "删除HTML标签数": removed_html_count,
        "删除Markdown图片数": len(MARKDOWN_IMAGE_PATTERN.findall(text)),
        "删除链接数": len(URL_PATTERN.findall(text)),
        "删除广告水印行数": 0,
        "合并多余空行处数": 0,
    }

    cleaned = MARKDOWN_IMAGE_PATTERN.sub("", html_cleaned)
    cleaned = URL_PATTERN.sub(_remove_url, cleaned)

    kept_lines = []
    for line in cleaned.splitlines():
        if is_promotional_ad_line(line):
            record["删除广告水印行数"] += 1
            continue
        kept_lines.append(line.rstrip())
    cleaned = "\n".join(kept_lines)

    repeated_blank_lines = re.findall(r"\n[ \t]*\n(?:[ \t]*\n)+", cleaned)
    record["合并多余空行处数"] = len(repeated_blank_lines)
    cleaned = re.sub(r"\n[ \t]*\n(?:[ \t]*\n)+", "\n\n", cleaned)
    return cleaned.strip() + ("\n" if cleaned.strip() else ""), record


def _paths_are_same(first: Path, second: Path) -> bool:
    if first.resolve(strict=False) == second.resolve(strict=False):
        return True
    try:
        return os.path.samefile(str(first), str(second))
    except FileNotFoundError:
        return False


def _validate_output_paths(
    source: Path,
    destination: Path,
    report: Path,
    force: bool,
) -> None:
    pairs = [
        (source, destination),
        (source, report),
        (destination, report),
    ]
    if any(_paths_are_same(first, second) for first, second in pairs):
        raise ValueError(
            "来源、输出和清洗记录必须使用不同路径，"
            "不能指向同一文件"
        )
    if not force:
        for path in (destination, report):
            if path.exists():
                raise FileExistsError("输出文件已存在：{}".format(path))


def _stage_text(path: Path, content: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        dir=str(path.parent),
        prefix=".{}.tmp-".format(path.name),
        text=True,
    )
    temporary_path = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
    except BaseException:
        try:
            temporary_path.unlink()
        except FileNotFoundError:
            pass
        raise
    return temporary_path


def _reserve_backup_path(path: Path) -> Path:
    descriptor, backup_name = tempfile.mkstemp(
        dir=str(path.parent),
        prefix=".{}.bak-".format(path.name),
    )
    os.close(descriptor)
    backup_path = Path(backup_name)
    backup_path.unlink()
    return backup_path


def _remove_if_present(path: Path) -> None:
    try:
        path.unlink()
    except FileNotFoundError:
        pass


def _commit_pair(
    destination: Path,
    staged_destination: Path,
    report: Path,
    staged_report: Path,
    force: bool,
) -> None:
    targets = [
        (destination, staged_destination),
        (report, staged_report),
    ]
    backups = {}
    committed = []
    try:
        if not force:
            for target, _ in targets:
                if target.exists():
                    raise FileExistsError("输出文件已存在：{}".format(target))
        else:
            for target, _ in targets:
                if target.exists():
                    backup = _reserve_backup_path(target)
                    os.replace(str(target), str(backup))
                    backups[target] = backup
        for target, staged in targets:
            if force:
                os.replace(str(staged), str(target))
            else:
                try:
                    os.link(str(staged), str(target))
                except FileExistsError:
                    raise FileExistsError(
                        "输出文件已存在：{}".format(target)
                    ) from None
            committed.append(target)
            if not force:
                staged.unlink()
        for parent in {target.parent for target, _ in targets}:
            fsync_directory(parent)
    except BaseException:
        for target in committed:
            _remove_if_present(target)
        for target, backup in backups.items():
            if backup.exists():
                os.replace(str(backup), str(target))
        for parent in {target.parent for target, _ in targets}:
            fsync_directory(parent)
        raise
    finally:
        for _, staged in targets:
            _remove_if_present(staged)
        for backup in backups.values():
            _remove_if_present(backup)


def _format_report(record: Dict[str, int]) -> str:
    lines = [
        "# OCR 清洗记录",
        "",
        "| 项目 | 数量 |",
        "| --- | ---: |",
    ]
    for key, value in record.items():
        lines.append("| {} | {} |".format(key, value))
    return "\n".join(lines) + "\n"


def clean_ocr_file(
    source: PathLike,
    destination: PathLike,
    record_path: PathLike,
    force: bool = False,
) -> Dict[str, int]:
    """原子写出清洗正文与报告，默认不覆盖已有文件。"""
    source_path = Path(source).expanduser().resolve()
    destination_path = Path(destination).expanduser().absolute()
    log_path = Path(record_path).expanduser().absolute()
    _validate_output_paths(
        source_path,
        destination_path,
        log_path,
        force,
    )
    text = source_path.read_text(encoding="utf-8")
    cleaned, record = clean_ocr(text)
    staged_destination = _stage_text(destination_path, cleaned)
    try:
        staged_report = _stage_text(log_path, _format_report(record))
    except BaseException:
        _remove_if_present(staged_destination)
        raise
    _validate_output_paths(
        source_path,
        destination_path,
        log_path,
        force,
    )
    _commit_pair(
        destination_path,
        staged_destination,
        log_path,
        staged_report,
        force,
    )
    return record


def main() -> int:
    parser = ChineseArgumentParser(description="保守清理 OCR 文本")
    parser.add_argument("输入", help="原始 OCR 文本路径")
    parser.add_argument("--output", required=True, help="清洗后文本路径")
    parser.add_argument("--report", required=True, help="清洗记录路径")
    parser.add_argument("--force", action="store_true", help="允许覆盖已有输出")
    arguments = parser.parse_args()
    try:
        record = clean_ocr_file(
            getattr(arguments, "输入"),
            arguments.output,
            arguments.report,
            force=arguments.force,
        )
    except (OSError, UnicodeError, ValueError) as error:
        print("OCR 清洗失败：{}".format(error), file=sys.stderr)
        return 1
    print(json.dumps(record, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
