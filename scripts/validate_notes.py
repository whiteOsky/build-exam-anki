"""校验考试笔记的结构与残留噪声。"""

import json
import re
import sys
from html.parser import HTMLParser
from pathlib import Path
from typing import Dict, List, Optional, Union
from urllib.parse import unquote, urlsplit

try:
    from .common import (
        ChineseArgumentParser,
        find_math_delimiter_error,
        is_promotional_ad_line,
        mask_currency_dollars,
    )
except ImportError:
    from common import (
        ChineseArgumentParser,
        find_math_delimiter_error,
        is_promotional_ad_line,
        mask_currency_dollars,
    )


PathLike = Union[str, Path]


HEADING_PATTERN = re.compile(r"^(#{1,6})\s+(.+?)\s*$")
HEADING_ONLY_LINE_PATTERN = re.compile(r"^\s*#{1,6}(?:\s+.*)?\s*$")
PURE_NUMBER_HEADING_PATTERN = re.compile(
    r"^(?:(?:\d+|[一二三四五六七八九十百]+)[.、．)]?|"
    r"[（(]\s*(?:\d+|[一二三四五六七八九十百]+)\s*[）)])$"
)
IMAGE_PLACEHOLDER_PATTERN = re.compile(
    r"(?:【\s*(?:图片|图)\s*】|(?<!!)\[\s*(?:图片|图)\s*\])",
    re.IGNORECASE,
)
LATEX_PATTERN = re.compile(
    r"(?:\\[A-Za-z]+\b|[A-Za-z0-9)]\s*[_^]\s*(?:\{[^}]*\}|[A-Za-z0-9]))"
)
MARKDOWN_ESCAPABLE = frozenset(
    r"""!"#$%&'()*+,-./:;<=>?@[\]^_`{|}~"""
)


class _HtmlImageParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.images = []

    def _record_image(self, tag, attrs) -> None:
        if tag.lower() != "img":
            return
        source = next(
            (
                value
                for name, value in attrs
                if name.lower() == "src"
            ),
            None,
        )
        self.images.append((self.getpos()[0], source))

    def handle_starttag(self, tag, attrs) -> None:
        self._record_image(tag, attrs)

    def handle_startendtag(self, tag, attrs) -> None:
        self._record_image(tag, attrs)


def _delimiter_errors(text: str) -> List[str]:
    error = find_math_delimiter_error(text)
    return [error] if error else []


def _mask_math(text: str) -> str:
    masked = mask_currency_dollars(text)
    masked = re.sub(r"\$\$.*?\$\$", " ", masked, flags=re.DOTALL)
    masked = re.sub(r"(?<!\\)\$.*?(?<!\\)\$", " ", masked, flags=re.DOTALL)
    masked = re.sub(r"\\\[.*?\\\]", " ", masked, flags=re.DOTALL)
    masked = re.sub(r"\\\(.*?\\\)", " ", masked, flags=re.DOTALL)
    return masked


def _mask_inline_code(line: str) -> str:
    pattern = re.compile(r"(`+)(.*?)\1")
    return pattern.sub(lambda match: " " * len(match.group(0)), line)


def _mask_code(text: str) -> str:
    masked_lines = []
    fence_character = None
    fence_length = 0
    for line in text.splitlines():
        if fence_character is not None:
            closing = r"^\s*{}{{{},}}\s*$".format(
                re.escape(fence_character),
                fence_length,
            )
            masked_lines.append(" " * len(line))
            if re.match(closing, line):
                fence_character = None
                fence_length = 0
            continue

        opening = re.match(r"^\s*(`{3,}|~{3,})", line)
        if opening:
            marker = opening.group(1)
            fence_character = marker[0]
            fence_length = len(marker)
            masked_lines.append(" " * len(line))
            continue
        masked_lines.append(_mask_inline_code(line))
    return "\n".join(masked_lines)


def _unescape_markdown_target(target: str) -> str:
    result = []
    index = 0
    while index < len(target):
        character = target[index]
        if (
            character == "\\"
            and index + 1 < len(target)
            and target[index + 1] in MARKDOWN_ESCAPABLE
        ):
            result.append(target[index + 1])
            index += 2
            continue
        result.append(character)
        index += 1
    return "".join(result)


def _find_closing_bracket(line: str, start: int) -> Optional[int]:
    depth = 1
    index = start
    while index < len(line):
        if line[index] == "\\" and index + 1 < len(line):
            index += 2
            continue
        if line[index] == "[":
            depth += 1
        elif line[index] == "]":
            depth -= 1
            if depth == 0:
                return index
        index += 1
    return None


def _find_closing_angle(line: str, start: int) -> Optional[int]:
    index = start
    while index < len(line):
        if line[index] == "\\" and index + 1 < len(line):
            index += 2
            continue
        if line[index] == ">":
            return index
        index += 1
    return None


def _iter_markdown_image_targets(line: str):
    search_from = 0
    while True:
        image_start = line.find("![", search_from)
        if image_start < 0:
            return
        label_end = _find_closing_bracket(line, image_start + 2)
        if label_end is None or label_end + 1 >= len(line):
            search_from = image_start + 2
            continue
        if line[label_end + 1] != "(":
            search_from = label_end + 1
            continue

        index = label_end + 2
        while index < len(line) and line[index].isspace():
            index += 1
        if index < len(line) and line[index] == "<":
            target_start = index + 1
            target_end = _find_closing_angle(line, target_start)
            if target_end is None:
                search_from = image_start + 2
                continue
            index = target_end + 1
        else:
            target_start = index
            target_end = None

        depth = 1
        while index < len(line):
            character = line[index]
            if character == "\\" and index + 1 < len(line):
                index += 2
                continue
            if character == "(":
                depth += 1
            elif character == ")":
                depth -= 1
                if depth == 0:
                    if target_end is None:
                        target_end = index
                    yield line[target_start:target_end]
                    search_from = index + 1
                    break
            elif character.isspace() and depth == 1 and target_end is None:
                target_end = index
            index += 1
        else:
            search_from = image_start + 2


def _validate_image_target(
    raw_target: str,
    line_number: int,
    note_path: Optional[Path],
    errors: List[str],
    warnings: List[str],
) -> None:
    target = _unescape_markdown_target(raw_target.strip())
    parsed = urlsplit(target)
    if parsed.scheme or target.startswith("//"):
        warnings.append(
            "第{}行图片使用远程 URL，无法验证图片：{}".format(
                line_number,
                target,
            )
        )
        return
    if note_path is None:
        warnings.append(
            (
                "第{}行存在本地图片，未提供笔记路径，"
                "无法验证图片"
            ).format(line_number)
        )
        return

    local_target = unquote(parsed.path)
    image_path = Path(local_target)
    if not image_path.is_absolute():
        image_path = note_path.parent / image_path
    if not local_target or not image_path.exists():
        errors.append(
            "第{}行发现失效图片引用：{}".format(
                line_number,
                image_path,
            )
        )


def _validate_markdown_images(
    line: str,
    line_number: int,
    note_path: Optional[Path],
    errors: List[str],
    warnings: List[str],
) -> None:
    for target in _iter_markdown_image_targets(line):
        _validate_image_target(
            target,
            line_number,
            note_path,
            errors,
            warnings,
        )


def _validate_html_images(
    text: str,
    note_path: Optional[Path],
    errors: List[str],
    warnings: List[str],
) -> None:
    parser = _HtmlImageParser()
    parser.feed(text)
    parser.close()
    for line_number, source in parser.images:
        if not source:
            errors.append(
                "第{}行发现缺少 src 的 HTML 图片".format(line_number)
            )
            continue
        _validate_image_target(
            source,
            line_number,
            note_path,
            errors,
            warnings,
        )


def validate_note(
    text: str,
    note_path: Optional[PathLike] = None,
) -> Dict[str, object]:
    """校验一篇 Markdown 笔记并返回汉语结果。"""
    errors = []
    warnings = []
    if not any(
        line.strip() and not HEADING_ONLY_LINE_PATTERN.fullmatch(line)
        for line in text.splitlines()
    ):
        errors.append("笔记为空或无有效知识内容")
    masked_text = _mask_code(text)
    lines = masked_text.splitlines()
    resolved_note_path = (
        Path(note_path).expanduser().resolve()
        if note_path is not None
        else None
    )
    _validate_html_images(
        masked_text,
        resolved_note_path,
        errors,
        warnings,
    )

    previous_heading_level = 0
    heading_count = 0
    for line_number, line in enumerate(lines, start=1):
        if is_promotional_ad_line(line):
            errors.append(
                "第{}行发现明确广告或二维码导流".format(line_number)
            )
        _validate_markdown_images(
            line,
            line_number,
            resolved_note_path,
            errors,
            warnings,
        )
        if IMAGE_PLACEHOLDER_PATTERN.search(line):
            errors.append("第{}行发现失效图片引用".format(line_number))

        heading = HEADING_PATTERN.match(line)
        if not heading:
            continue
        level = len(heading.group(1))
        title = heading.group(2).strip()
        if heading_count == 0 and level > 1:
            errors.append(
                (
                    "第{}行首个标题层级异常：必须从一级标题开始"
                ).format(line_number)
            )
        if previous_heading_level and level > previous_heading_level + 1:
            errors.append(
                "第{}行标题层级跳跃：从{}级跳到{}级".format(
                    line_number, previous_heading_level, level
                )
            )
        if line_number > 1 and lines[line_number - 2].strip():
            errors.append("第{}行标题前缺少空行".format(line_number))
        if PURE_NUMBER_HEADING_PATTERN.fullmatch(title):
            errors.append("第{}行存在异常纯序号标题".format(line_number))
        previous_heading_level = level
        heading_count += 1

    errors.extend(_delimiter_errors(masked_text))

    outside_math = _mask_math(masked_text)
    for line_number, line in enumerate(outside_math.splitlines(), start=1):
        if LATEX_PATTERN.search(line):
            errors.append(
                "第{}行发现数学环境外裸露 LaTeX".format(line_number)
            )

    return {"通过": not errors, "错误": errors, "警告": warnings}


def main() -> int:
    parser = ChineseArgumentParser(description="校验考试笔记")
    parser.add_argument("笔记", help="待校验的 Markdown 笔记路径")
    arguments = parser.parse_args()
    note_path = Path(getattr(arguments, "笔记")).expanduser().resolve()
    try:
        text = note_path.read_text(encoding="utf-8")
    except UnicodeError:
        print(
            "笔记读取失败：{} 必须使用 UTF-8 编码".format(note_path),
            file=sys.stderr,
        )
        return 1
    except OSError as error:
        print(
            "笔记读取失败：{}：{}".format(note_path, error),
            file=sys.stderr,
        )
        return 1

    result = validate_note(text, note_path=note_path)
    if result["警告"]:
        print("笔记校验警告：共{}项".format(len(result["警告"])))
        for index, warning in enumerate(result["警告"], start=1):
            print("{}. {}".format(index, warning))
    if result["通过"]:
        print("笔记校验通过")
        return 0
    print("笔记校验未通过：{}".format(note_path), file=sys.stderr)
    print(
        json.dumps(result, ensure_ascii=False, indent=2),
        file=sys.stderr,
    )
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
