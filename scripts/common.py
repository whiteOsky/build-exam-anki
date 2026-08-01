"""制卡流程共用的固定契约与文件读写工具。"""

import argparse
import csv
import json
import os
import re
import sys
import tempfile
import unicodedata
from pathlib import Path
from typing import Any, Dict, List, Optional, Union


PathLike = Union[str, Path]

CARD_FIELDS = [
    "Front",
    "Back",
    "Extra",
    "Mistake",
    "Trigger",
    "Importance",
    "Source",
    "Tags",
]

IMPORTANCE_VALUES = ["必背", "高频", "易错", "理解", "低频补充"]

COVERAGE_STATUSES = ["已制卡", "并入上级卡", "不适合制卡", "OCR存疑"]

MATH_DELIMITER_PATTERN = re.compile(r"\$\$|\$|\\\(|\\\)|\\\[|\\\]")

MATH_CLOSERS = {r"\(": r"\)", r"\[": r"\]"}

CURRENCY_AMOUNT_PATTERN = re.compile(r"\d+(?:,\d{3})*(?:\.\d+)?")
CURRENCY_UNIT_PATTERN = re.compile(r"(?:美元|美金|元)")

DIRECT_AD_LINE_PATTERN = re.compile(
    r"(?:扫码关注|关注公众号|微信公众号|加微信|添加微信|"
    r"领取资料|购买课程|付费课程|内部资料.*联系)"
)
QR_MARKER_PATTERN = re.compile(r"(?:扫码|二维码|QR\s*码)", re.IGNORECASE)
QR_GUIDANCE_ACTION_PATTERN = re.compile(
    r"(?:扫描|扫码|长按|关注|添加|加入|获取|领取|下载|"
    r"查看|观看|进入|联系|购买|见(?:下|上|左|右)?图)"
)
QR_PROMOTION_TARGET_PATTERN = re.compile(
    r"(?:公众号|微信|资料|视频讲解|讲解视频|"
    r"配套(?:课程|题库|答案|解析|资料)|参考答案|习题答案|"
    r"答案|题库|网盘|客服|详情|下图|交流群|学习群)"
)

CHINESE_PUNCTUATION_TRANSLATION = str.maketrans(
    {
        "。": ".",
        "、": ",",
        "【": "[",
        "】": "]",
        "《": "<",
        "》": ">",
        "“": '"',
        "”": '"',
        "‘": "'",
        "’": "'",
        "「": '"',
        "」": '"',
        "『": '"',
        "』": '"',
    }
)


class ChineseArgumentParser(argparse.ArgumentParser):
    """统一公开脚本的中文帮助与参数错误。"""

    def __init__(self, *args, **kwargs) -> None:
        kwargs["add_help"] = False
        super().__init__(*args, **kwargs)
        self._positionals.title = "位置参数"
        self._optionals.title = "选项"
        self.add_argument(
            "-h",
            "--help",
            action="help",
            help="显示帮助信息并退出",
        )

    def format_usage(self) -> str:
        return super().format_usage().replace("usage:", "用法：", 1)

    def format_help(self) -> str:
        return (
            super()
            .format_help()
            .replace("usage:", "用法：", 1)
            .replace("\n位置参数:\n", "\n位置参数：\n")
            .replace("\n选项:\n", "\n选项：\n")
        )

    def error(self, message: str) -> None:
        self.print_usage(sys.stderr)
        self.exit(2, "参数错误：请检查命令参数，并使用 --help 查看格式。\n")


def _is_escaped(text: str, position: int) -> bool:
    slash_count = 0
    position -= 1
    while position >= 0 and text[position] == "\\":
        slash_count += 1
        position -= 1
    return slash_count % 2 == 1


def _is_currency_dollar(text: str, position: int) -> bool:
    if position + 1 >= len(text) or not text[position + 1].isdigit():
        return False
    amount = CURRENCY_AMOUNT_PATTERN.match(text, position + 1)
    if amount is None:
        return False
    following = text[amount.end():]
    if CURRENCY_UNIT_PATTERN.match(following):
        return True

    preceding = text[position - 1] if position > 0 else ""
    if preceding and not (
        preceding.isspace() or unicodedata.category(preceding).startswith("P")
    ):
        return False
    if not following:
        return True

    boundary = following[0]
    if boundary.isspace():
        next_character = following.lstrip()[:1]
        return next_character not in r"+-*/^_=<>\$"
    if boundary not in "，。！？；、：,.!?:;）)]}":
        return False

    remainder = following[1:]
    if not remainder:
        return True
    next_character = remainder[:1]
    if next_character.isdigit() or next_character in r"+-*/^_=<>\$.,":
        return False
    if boundary in ",.!?:;" and not next_character.isspace():
        return False
    return True


def _is_cjk_character(character: str) -> bool:
    codepoint = ord(character)
    return (
        0x3400 <= codepoint <= 0x4DBF
        or 0x4E00 <= codepoint <= 0x9FFF
        or 0xF900 <= codepoint <= 0xFAFF
        or 0x20000 <= codepoint <= 0x2FA1F
        or codepoint == 0x3007
    )


def _math_ranges(text: str) -> List[tuple]:
    ranges = []
    opened = None
    expected_closer = None
    start = None

    for match in MATH_DELIMITER_PATTERN.finditer(text):
        token = match.group(0)
        if _is_escaped(text, match.start()):
            continue
        if token == "$" and _is_currency_dollar(text, match.start()):
            continue

        if opened is None:
            if token in MATH_CLOSERS:
                opened = token
                expected_closer = MATH_CLOSERS[token]
                start = match.start()
            elif token in {"$", "$$"}:
                opened = token
                expected_closer = token
                start = match.start()
            continue

        if token == expected_closer:
            ranges.append((start, match.end()))
            opened = None
            expected_closer = None
            start = None

    if start is not None:
        ranges.append((start, len(text)))
    return ranges


def normalize_front(text: str) -> str:
    """规范化 Front，同时保留公式和拉丁词内部的空格语义。"""
    normalized = unicodedata.normalize("NFKC", str(text))
    normalized = normalized.translate(CHINESE_PUNCTUATION_TRANSLATION)
    normalized = re.sub(r"\s+", " ", normalized).strip()
    math_ranges = _math_ranges(normalized)

    characters = []
    range_index = 0
    for index, character in enumerate(normalized):
        while (
            range_index < len(math_ranges)
            and index >= math_ranges[range_index][1]
        ):
            range_index += 1
        in_math = (
            range_index < len(math_ranges)
            and math_ranges[range_index][0] <= index < math_ranges[range_index][1]
        )
        if (
            character == " "
            and not in_math
            and index > 0
            and index + 1 < len(normalized)
            and _is_cjk_character(normalized[index - 1])
            and _is_cjk_character(normalized[index + 1])
        ):
            continue
        characters.append(character if in_math else character.lower())
    return "".join(characters)


def to_anki_mathjax(text: str) -> str:
    """把未转义的 Markdown 数学定界符转换为 Anki MathJax。"""
    value = str(text)
    pieces = []
    last_position = 0
    opened = None

    for match in MATH_DELIMITER_PATTERN.finditer(value):
        token = match.group(0)
        if token not in {"$", "$$"}:
            continue
        if _is_escaped(value, match.start()):
            continue
        if token == "$" and _is_currency_dollar(value, match.start()):
            continue

        pieces.append(value[last_position:match.start()])
        if opened is None:
            pieces.append(r"\[" if token == "$$" else r"\(")
            opened = token
        else:
            pieces.append(r"\]" if token == "$$" else r"\)")
            opened = None
        last_position = match.end()

    pieces.append(value[last_position:])
    return "".join(pieces)


def is_promotional_ad_line(text: str) -> bool:
    """判断一行是否为明确广告或二维码导流。"""
    if DIRECT_AD_LINE_PATTERN.search(text):
        return True
    return bool(
        QR_MARKER_PATTERN.search(text)
        and QR_GUIDANCE_ACTION_PATTERN.search(text)
        and QR_PROMOTION_TARGET_PATTERN.search(text)
    )


def mask_currency_dollars(text: str) -> str:
    """屏蔽货币金额中的美元符号，避免按数学定界符处理。"""
    characters = list(text)
    for match in MATH_DELIMITER_PATTERN.finditer(text):
        if match.group(0) == "$" and _is_currency_dollar(text, match.start()):
            characters[match.start()] = "¤"
    return "".join(characters)


def find_math_delimiter_error(text: str) -> Optional[str]:
    """按出现顺序检查 Markdown 与 Anki 数学定界符。"""
    opened = None
    expected_closer = None

    for match in MATH_DELIMITER_PATTERN.finditer(text):
        token = match.group(0)
        if _is_escaped(text, match.start()):
            continue
        if token == "$" and _is_currency_dollar(text, match.start()):
            continue

        if token in MATH_CLOSERS:
            if opened is not None:
                return "数学定界符嵌套错误：{} 内出现 {}".format(opened, token)
            opened = token
            expected_closer = MATH_CLOSERS[token]
            continue

        if token in MATH_CLOSERS.values():
            if opened is None:
                return "数学定界符顺序错误：{} 缺少前置定界符".format(token)
            if token != expected_closer:
                return "数学定界符顺序错误：{} 应由 {} 结束".format(
                    opened, expected_closer
                )
            opened = None
            expected_closer = None
            continue

        if opened is None:
            opened = token
            expected_closer = token
        elif opened == token:
            opened = None
            expected_closer = None
        else:
            return "数学定界符嵌套错误：{} 内出现 {}".format(opened, token)

    if opened is not None:
        return "数学定界符不配对：{} 未闭合".format(opened)
    return None


def ensure_safe_output_path(root: PathLike, path: PathLike) -> Path:
    """确认输出路径位于根目录内，且路径组成中没有符号链接。"""
    project_root = Path(root).expanduser().resolve()
    if not project_root.is_dir():
        raise ValueError("项目根目录不存在：{}".format(project_root))

    candidate = Path(path).expanduser()
    if not candidate.is_absolute():
        candidate = Path.cwd() / candidate
    candidate = Path(os.path.abspath(str(candidate)))
    resolved = candidate.resolve(strict=False)
    try:
        resolved.relative_to(project_root)
    except ValueError:
        raise ValueError(
            "输出路径包含符号链接或解析后超出项目根目录：{}".format(
                resolved
            )
        )
    return candidate


def fsync_directory(path: PathLike) -> None:
    """同步目录项，确保原子替换在文件系统中持久化。"""
    descriptor = os.open(str(path), os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def atomic_write_text(
    path: PathLike,
    text: str,
    root: Optional[PathLike] = None,
    overwrite: bool = True,
) -> Path:
    """在目标同目录写临时文件并原子替换目标。"""
    destination = Path(path).expanduser()
    if not destination.is_absolute():
        destination = Path.cwd() / destination
    destination = Path(os.path.abspath(str(destination)))
    if root is not None:
        destination = ensure_safe_output_path(root, destination)
    if destination.exists() and not overwrite:
        raise FileExistsError("输出文件已存在：{}".format(destination))

    destination.parent.mkdir(parents=True, exist_ok=True)
    if root is not None:
        destination = ensure_safe_output_path(root, destination)

    temporary_path = None
    try:
        descriptor, temporary_name = tempfile.mkstemp(
            dir=str(destination.parent),
            prefix=".{}.tmp-".format(destination.name),
            text=True,
        )
        temporary_path = Path(temporary_name)
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(str(temporary_path), str(destination))
        temporary_path = None
        fsync_directory(destination.parent)
        return destination
    finally:
        if temporary_path is not None:
            try:
                temporary_path.unlink()
            except FileNotFoundError:
                pass


def read_tsv_strict(path: PathLike) -> List[Dict[str, str]]:
    """按固定八字段读取 TSV，表头或列数不符时立即失败。"""
    source = Path(path)
    try:
        with source.open("r", encoding="utf-8-sig", newline="") as handle:
            reader = csv.reader(handle, delimiter="\t", strict=True)
            try:
                header = next(reader)
            except StopIteration:
                raise ValueError("TSV 文件为空，缺少表头")

            if header != CARD_FIELDS:
                raise ValueError(
                    "TSV 表头必须严格为：" + "、".join(CARD_FIELDS)
                )

            rows = []
            for line_number, values in enumerate(reader, start=2):
                if len(values) != len(CARD_FIELDS):
                    raise ValueError(
                        "TSV 第{}行列数为{}，必须为8列".format(
                            line_number, len(values)
                        )
                    )
                rows.append(dict(zip(CARD_FIELDS, values)))
            return rows
    except csv.Error:
        raise ValueError("TSV 格式错误：内容无法按制表符格式解析")
    except UnicodeError:
        raise ValueError("TSV 编码错误：文件必须使用 UTF-8 编码")
    except OSError:
        raise ValueError("TSV 读取失败：无法读取指定文件")


def read_json(path: PathLike) -> Any:
    """读取 UTF-8 JSON 文件。"""
    try:
        with Path(path).open("r", encoding="utf-8") as handle:
            return json.load(handle)
    except json.JSONDecodeError as error:
        raise ValueError(
            "JSON 格式错误：第{}行第{}列".format(error.lineno, error.colno)
        )
    except UnicodeError:
        raise ValueError("JSON 编码错误：文件必须使用 UTF-8 编码")
    except OSError:
        raise ValueError("JSON 读取失败：无法读取指定文件")


def write_json(
    path: PathLike, data: Any, root: Optional[PathLike] = None
) -> None:
    """以可读的 UTF-8 格式写入 JSON 文件。"""
    try:
        content = json.dumps(data, ensure_ascii=False, indent=2) + "\n"
    except (TypeError, ValueError):
        raise ValueError("JSON 写入失败：数据无法序列化")
    atomic_write_text(path, content, root=root)
