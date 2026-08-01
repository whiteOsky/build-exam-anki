"""校验考试卡片 TSV 的固定字段与内容质量。"""

import json
import re
import sys
from pathlib import Path
from typing import Dict, Mapping, Sequence, Union

try:
    from .common import (
        CARD_FIELDS,
        ChineseArgumentParser,
        IMPORTANCE_VALUES,
        find_math_delimiter_error,
        normalize_front,
        read_tsv_strict,
    )
except ImportError:
    from common import (
        CARD_FIELDS,
        ChineseArgumentParser,
        IMPORTANCE_VALUES,
        find_math_delimiter_error,
        normalize_front,
        read_tsv_strict,
    )


PathLike = Union[str, Path]

GENERIC_FRONT_PATTERNS = [
    re.compile(r"第\s*(?:[Nn]|\d+|[一二三四五六七八九十百]+)\s*个知识点"),
    re.compile(r"本节.*是什么"),
    re.compile(r"请回忆上述内容"),
    re.compile(r"请(?:简述|概述|说明)(?:本节|本章|上述)内容"),
    re.compile(r"这是什么"),
]


def _mathjax_is_balanced(text: str) -> bool:
    return find_math_delimiter_error(text) is None


def validate_rows(rows: Sequence[Mapping[str, str]]) -> Dict[str, object]:
    """校验卡片行，内容错误阻止通过，泛化问题只产生警告。"""
    errors = []
    warnings = []
    seen_fronts = {}

    if not rows:
        errors.append("卡片 TSV 至少包含1张卡片")

    for row_number, row in enumerate(rows, start=1):
        if list(row.keys()) != CARD_FIELDS:
            errors.append(
                "第{}张卡片字段顺序错误，必须为：{}".format(
                    row_number, "、".join(CARD_FIELDS)
                )
            )

        for field in CARD_FIELDS:
            value = row.get(field, "")
            if not str(value).strip():
                errors.append("第{}张卡片 {} 为空".format(row_number, field))

        front = str(row.get("Front", ""))
        normalized_front = normalize_front(front)
        if normalized_front:
            if normalized_front in seen_fronts:
                errors.append(
                    "第{}张卡片与第{}张卡片 Front 重复".format(
                        row_number, seen_fronts[normalized_front]
                    )
                )
            else:
                seen_fronts[normalized_front] = row_number

        importance = str(row.get("Importance", "")).strip()
        if importance and importance not in IMPORTANCE_VALUES:
            errors.append(
                "第{}张卡片 Importance 非法：{}".format(row_number, importance)
            )

        tags = str(row.get("Tags", "")).strip()
        if tags and ("::" not in tags or any(not part.strip() for part in tags.split("::"))):
            errors.append("第{}张卡片 Tags 缺少 :: 层级".format(row_number))

        source = str(row.get("Source", "")).strip()
        if not source and not any(
            message == "第{}张卡片 Source 为空".format(row_number)
            for message in errors
        ):
            errors.append("第{}张卡片 Source 为空".format(row_number))

        for field in CARD_FIELDS:
            value = str(row.get(field, ""))
            if value and not _mathjax_is_balanced(value):
                errors.append(
                    "第{}张卡片 {} 的 MathJax 定界符不配对".format(
                        row_number, field
                    )
                )

        if any(pattern.search(front) for pattern in GENERIC_FRONT_PATTERNS):
            warnings.append("第{}张卡片问题过于泛化：{}".format(row_number, front))

    return {
        "通过": not errors,
        "卡片数": len(rows),
        "错误": errors,
        "警告": warnings,
    }


def validate_tsv(path: PathLike) -> Dict[str, object]:
    """严格读取并校验一个 TSV 文件。"""
    try:
        rows = read_tsv_strict(path)
    except (OSError, UnicodeError, ValueError) as error:
        return {"通过": False, "卡片数": 0, "错误": [str(error)], "警告": []}
    return validate_rows(rows)


def main() -> int:
    parser = ChineseArgumentParser(description="校验考试卡片 TSV")
    parser.add_argument("卡片", help="待校验的 TSV 文件路径")
    arguments = parser.parse_args()
    card_path = Path(getattr(arguments, "卡片")).expanduser().resolve()
    result = validate_tsv(card_path)
    serialized = json.dumps(result, ensure_ascii=False, indent=2)
    if result["通过"]:
        print(serialized)
        return 0
    print("卡片校验未通过：{}".format(card_path), file=sys.stderr)
    print(serialized, file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
