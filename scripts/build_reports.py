"""校验逐节覆盖状态并生成统计、背诵索引和 OCR 存疑报告。"""

import csv
import os
import re
import stat
import sys
import tempfile
from collections import Counter
from pathlib import Path
from typing import Dict, List, Mapping, Sequence, Union

try:
    from .build_apkg import stable_guid
    from .common import (
        COVERAGE_STATUSES,
        IMPORTANCE_VALUES,
        ChineseArgumentParser,
        atomic_write_text,
        ensure_safe_output_path,
        fsync_directory,
        read_tsv_strict,
    )
    from .validate_cards import validate_rows
except ImportError:
    from build_apkg import stable_guid
    from common import (
        COVERAGE_STATUSES,
        IMPORTANCE_VALUES,
        ChineseArgumentParser,
        atomic_write_text,
        ensure_safe_output_path,
        fsync_directory,
        read_tsv_strict,
    )
    from validate_cards import validate_rows


PathLike = Union[str, Path]
COVERAGE_FIELDS = [
    "SectionID",
    "Title",
    "Status",
    "CardIDs",
    "Reason",
    "Source",
]
SECTION_FIELDS = ["SectionID", "Title", "Source"]
SECTION_MANIFEST = Path("build/小节清单.tsv")
REPORT_NAMES = ["覆盖校验.md", "卡片统计.md", "背诵索引.md", "OCR存疑.md"]
CARD_ID_SEPARATOR = re.compile(r"[,，、;；|/\s]+")


def read_coverage_tsv(path: PathLike) -> List[Dict[str, str]]:
    """严格读取固定六字段覆盖表。"""
    source = Path(path)
    try:
        with source.open("r", encoding="utf-8-sig", newline="") as handle:
            reader = csv.reader(handle, delimiter="\t", strict=True)
            try:
                header = next(reader)
            except StopIteration:
                raise ValueError("覆盖表为空，缺少表头")
            if header != COVERAGE_FIELDS:
                raise ValueError(
                    "覆盖表表头必须严格为：" + "、".join(COVERAGE_FIELDS)
                )

            rows = []
            for line_number, values in enumerate(reader, start=2):
                if len(values) != len(COVERAGE_FIELDS):
                    raise ValueError(
                        "覆盖表第{}行列数为{}，必须为6列".format(
                            line_number, len(values)
                        )
                    )
                rows.append(dict(zip(COVERAGE_FIELDS, values)))
            if not rows:
                raise ValueError("覆盖表不得为空，至少一条覆盖记录是必需的")
            return rows
    except csv.Error:
        raise ValueError("覆盖表格式错误：内容无法按制表符格式解析")
    except UnicodeError:
        raise ValueError("覆盖表编码错误：文件必须使用 UTF-8 编码")
    except OSError:
        raise ValueError("覆盖表读取失败：无法读取指定文件")


def read_section_manifest(path: PathLike) -> List[Dict[str, str]]:
    """严格读取 build/小节清单.tsv 的固定三字段来源清单。"""
    source = Path(path)
    try:
        with source.open("r", encoding="utf-8-sig", newline="") as handle:
            reader = csv.reader(handle, delimiter="\t", strict=True)
            try:
                header = next(reader)
            except StopIteration:
                raise ValueError("来源小节清单为空，缺少表头")
            if header != SECTION_FIELDS:
                raise ValueError(
                    "来源小节清单表头必须严格为：" + "、".join(SECTION_FIELDS)
                )

            rows = []
            seen = {}
            for line_number, values in enumerate(reader, start=2):
                if len(values) != len(SECTION_FIELDS):
                    raise ValueError(
                        "来源小节清单第{}行列数为{}，必须为3列".format(
                            line_number, len(values)
                        )
                    )
                row = dict(zip(SECTION_FIELDS, values))
                for field in SECTION_FIELDS:
                    if not row[field].strip():
                        raise ValueError(
                            "来源小节清单第{}行 {} 为空".format(
                                line_number, field
                            )
                        )
                section_id = row["SectionID"].strip()
                if section_id in seen:
                    raise ValueError(
                        "来源小节清单第{}行与第{}行 SectionID 重复：{}".format(
                            line_number, seen[section_id], section_id
                        )
                    )
                seen[section_id] = line_number
                rows.append(row)
            if not rows:
                raise ValueError("来源小节清单必须至少包含一条来源记录")
            return rows
    except csv.Error:
        raise ValueError("来源小节清单格式错误：内容无法按制表符格式解析")
    except UnicodeError:
        raise ValueError("来源小节清单编码错误：文件必须使用 UTF-8 编码")
    except OSError:
        raise ValueError("来源小节清单读取失败：无法读取指定文件")


def validate_coverage_rows(
    rows: Sequence[Mapping[str, str]],
) -> Dict[str, object]:
    """校验字段、稳定小节身份和四种覆盖状态的条件字段。"""
    errors = []
    seen_section_ids = {}
    status_counts = Counter()

    for row_number, row in enumerate(rows, start=1):
        if list(row.keys()) != COVERAGE_FIELDS:
            errors.append(
                "第{}条覆盖记录字段顺序错误，必须为：{}".format(
                    row_number, "、".join(COVERAGE_FIELDS)
                )
            )

        for field in ["SectionID", "Title", "Status", "Source"]:
            if not str(row.get(field, "")).strip():
                errors.append("第{}条覆盖记录 {} 为空".format(row_number, field))

        section_id = str(row.get("SectionID", "")).strip()
        if section_id:
            if section_id in seen_section_ids:
                errors.append(
                    "第{}条覆盖记录与第{}条 SectionID 重复：{}".format(
                        row_number, seen_section_ids[section_id], section_id
                    )
                )
            else:
                seen_section_ids[section_id] = row_number

        status = str(row.get("Status", ""))
        if status not in COVERAGE_STATUSES:
            if status:
                errors.append(
                    "第{}条覆盖记录 Status 非法：{}".format(
                        row_number, status
                    )
                )
            continue
        status_counts[status] += 1

        if status in ["已制卡", "并入上级卡"]:
            if not str(row.get("CardIDs", "")).strip():
                errors.append(
                    "第{}条覆盖记录状态为{}，CardIDs 不得为空".format(
                        row_number, status
                    )
                )
        if status in ["不适合制卡", "OCR存疑"]:
            if not str(row.get("Reason", "")).strip():
                errors.append(
                    "第{}条覆盖记录状态为{}，Reason 不得为空".format(
                        row_number, status
                    )
                )

    return {
        "通过": not errors,
        "小节数": len(rows),
        "错误": errors,
        "状态统计": {
            status: status_counts.get(status, 0)
            for status in COVERAGE_STATUSES
        },
    }


def _escape_markdown_cell(value: object) -> str:
    text = str(value).replace("\r\n", "\n").replace("\r", "\n")
    return text.replace("\\", "\\\\").replace("|", r"\|").replace("\n", "<br>")


def _markdown_table(fields: Sequence[str], rows: Sequence[Mapping[str, object]]) -> str:
    lines = [
        "| {} |".format(" | ".join(fields)),
        "| {} |".format(" | ".join(["---"] * len(fields))),
    ]
    for row in rows:
        lines.append(
            "| {} |".format(
                " | ".join(
                    _escape_markdown_cell(row.get(field, ""))
                    for field in fields
                )
            )
        )
    return "\n".join(lines)


def _build_coverage_report(
    rows: Sequence[Mapping[str, str]], validation: Mapping[str, object]
) -> str:
    lines = [
        "# 覆盖校验",
        "",
        "已按来源小节清单逐节核对，SectionID 集合完全一致，Title/Source 对应。",
        "已制卡和并入上级卡的 CardIDs 均对应项目内真实卡片。",
        "",
        "- 小节总数：{}".format(validation["小节数"]),
    ]
    counts = validation["状态统计"]
    for status in COVERAGE_STATUSES:
        lines.append("- {}：{}".format(status, counts[status]))
    lines.extend(["", _markdown_table(COVERAGE_FIELDS, rows), ""])
    return "\n".join(lines)


def _build_statistics_report(
    chapter_rows: Mapping[str, Sequence[Mapping[str, str]]]
) -> str:
    fields = ["章节", "卡片数"] + list(IMPORTANCE_VALUES)
    table_rows = []
    totals = Counter()
    for chapter, rows in chapter_rows.items():
        counts = Counter(row["Importance"] for row in rows)
        item = {"章节": chapter, "卡片数": len(rows)}
        for importance in IMPORTANCE_VALUES:
            item[importance] = counts.get(importance, 0)
            totals[importance] += counts.get(importance, 0)
        table_rows.append(item)
        totals["卡片数"] += len(rows)

    total_row = {"章节": "合计", "卡片数": totals["卡片数"]}
    for importance in IMPORTANCE_VALUES:
        total_row[importance] = totals[importance]
    table_rows.append(total_row)
    return "\n".join(
        [
            "# 卡片统计",
            "",
            "卡片总数：{}".format(totals["卡片数"]),
            "",
            _markdown_table(fields, table_rows),
            "",
        ]
    )


def _build_memorization_index(
    chapter_rows: Mapping[str, Sequence[Mapping[str, str]]]
) -> str:
    grouped = {importance: [] for importance in IMPORTANCE_VALUES}
    for chapter, rows in chapter_rows.items():
        for row in rows:
            grouped[row["Importance"]].append(
                {
                    "卡片编号": stable_guid(row["Source"], row["Front"]),
                    "章节": chapter,
                    "Front": row["Front"],
                    "Trigger": row["Trigger"],
                    "Source": row["Source"],
                }
            )

    fields = ["卡片编号", "章节", "Front", "Trigger", "Source"]
    lines = ["# 背诵索引", ""]
    for importance in IMPORTANCE_VALUES:
        lines.extend(["## {}".format(importance), ""])
        if grouped[importance]:
            lines.extend([_markdown_table(fields, grouped[importance]), ""])
        else:
            lines.extend(["暂无卡片。", ""])
    return "\n".join(lines)


def _build_ocr_report(rows: Sequence[Mapping[str, str]]) -> str:
    uncertain = [row for row in rows if row.get("Status") == "OCR存疑"]
    lines = ["# OCR 存疑", "", "存疑小节数：{}".format(len(uncertain)), ""]
    if uncertain:
        lines.extend([_markdown_table(COVERAGE_FIELDS, uncertain), ""])
    else:
        lines.extend(["当前没有 OCR 存疑项。", ""])
    return "\n".join(lines)


def _read_chapter_cards(project_root: Path) -> Dict[str, List[Dict[str, str]]]:
    cards_directory = project_root / "cards"
    if not cards_directory.exists():
        return {}
    if not cards_directory.is_dir():
        raise ValueError("卡片目录不是有效目录：{}".format(cards_directory))

    chapters = {}
    for path in sorted(cards_directory.glob("*.tsv"), key=lambda item: item.name):
        rows = read_tsv_strict(path)
        validation = validate_rows(rows)
        if not validation["通过"] or validation["警告"]:
            details = list(validation["错误"])
            details.extend(
                "卡片校验警告（硬门禁）：{}".format(message)
                for message in validation["警告"]
            )
            raise ValueError(
                "卡片文件校验失败：{}；{}".format(
                    path, "；".join(details)
                )
            )
        chapters[path.stem] = rows
    return chapters


def _split_card_ids(value: object) -> List[str]:
    return [item for item in CARD_ID_SEPARATOR.split(str(value).strip()) if item]


def _validate_manifest_identity(
    coverage_rows: Sequence[Mapping[str, str]],
    manifest_rows: Sequence[Mapping[str, str]],
) -> List[str]:
    errors = []
    coverage_by_id = {
        str(row["SectionID"]).strip(): row for row in coverage_rows
    }
    manifest_by_id = {
        str(row["SectionID"]).strip(): row for row in manifest_rows
    }
    coverage_ids = set(coverage_by_id)
    manifest_ids = set(manifest_by_id)
    if coverage_ids != manifest_ids:
        details = []
        missing = sorted(manifest_ids - coverage_ids)
        extra = sorted(coverage_ids - manifest_ids)
        if missing:
            details.append("覆盖表缺少 {}".format("、".join(missing)))
        if extra:
            details.append("覆盖表多出 {}".format("、".join(extra)))
        errors.append(
            "覆盖表 SectionID 集合与来源小节清单不一致：{}".format(
                "；".join(details)
            )
        )

    for section_id in sorted(coverage_ids & manifest_ids):
        coverage_row = coverage_by_id[section_id]
        manifest_row = manifest_by_id[section_id]
        for field in ["Title", "Source"]:
            if str(coverage_row[field]).strip() != str(manifest_row[field]).strip():
                errors.append(
                    "SectionID {} 的 {} 不一致：覆盖表为“{}”，来源小节清单为“{}”".format(
                        section_id,
                        field,
                        str(coverage_row[field]).strip(),
                        str(manifest_row[field]).strip(),
                    )
                )
    return errors


def _validate_card_mappings(
    coverage_rows: Sequence[Mapping[str, str]],
    chapter_rows: Mapping[str, Sequence[Mapping[str, str]]],
) -> List[str]:
    errors = []
    card_locations = {}
    for chapter, rows in chapter_rows.items():
        for row_number, row in enumerate(rows, start=2):
            identifier = stable_guid(row["Source"], row["Front"])
            card_locations.setdefault(identifier, []).append(
                "{}第{}行".format(chapter, row_number)
            )

    for identifier, locations in sorted(card_locations.items()):
        if len(locations) > 1:
            errors.append(
                "卡片稳定编号重复：{}（{}）".format(
                    identifier, "、".join(locations)
                )
            )

    for row_number, row in enumerate(coverage_rows, start=1):
        if row.get("Status") not in ["已制卡", "并入上级卡"]:
            continue
        identifiers = _split_card_ids(row.get("CardIDs", ""))
        if not identifiers:
            errors.append(
                "第{}条覆盖记录 CardIDs 映射为空".format(row_number)
            )
            continue
        counts = Counter(identifiers)
        repeated = sorted(
            identifier for identifier, count in counts.items() if count > 1
        )
        if repeated:
            errors.append(
                "第{}条覆盖记录 CardIDs 存在重复编号：{}".format(
                    row_number, "、".join(repeated)
                )
            )
        missing = sorted(set(identifiers) - set(card_locations))
        if missing:
            errors.append(
                "第{}条覆盖记录 CardIDs 在项目 cards/*.tsv 中不存在：{}".format(
                    row_number, "、".join(missing)
                )
            )
    return errors


def validate_project_coverage(
    project: PathLike, coverage: PathLike
) -> Dict[str, object]:
    """复用同一规则验证覆盖表、来源清单和真实卡片映射。"""
    project_root = Path(project).expanduser().resolve()
    if not project_root.is_dir():
        raise ValueError("项目根目录不存在：{}".format(project_root))

    coverage_path = Path(coverage).expanduser()
    if not coverage_path.is_absolute():
        coverage_path = project_root / coverage_path
    manifest_path = project_root / SECTION_MANIFEST
    if not manifest_path.is_file():
        raise ValueError(
            "缺少来源小节清单 build/小节清单.tsv，无法证明逐节完整"
        )

    coverage_rows = read_coverage_tsv(coverage_path)
    validation = validate_coverage_rows(coverage_rows)
    errors = list(validation["错误"])
    manifest_rows = read_section_manifest(manifest_path)
    chapter_rows = _read_chapter_cards(project_root)
    errors.extend(_validate_manifest_identity(coverage_rows, manifest_rows))
    errors.extend(_validate_card_mappings(coverage_rows, chapter_rows))
    validation = dict(validation)
    validation["错误"] = errors
    validation["通过"] = not errors
    return {
        "通过": not errors,
        "错误": errors,
        "覆盖行": coverage_rows,
        "来源行": manifest_rows,
        "章节卡片": chapter_rows,
        "覆盖表": coverage_path,
        "来源清单": manifest_path,
        "校验": validation,
    }


def _validate_reports_directory(project_root: Path, reports_directory: Path) -> None:
    ensure_safe_output_path(project_root, reports_directory)
    if reports_directory.is_symlink():
        raise ValueError("报告目录不得为符号链接：{}".format(reports_directory))
    if reports_directory.exists() and not reports_directory.is_dir():
        raise ValueError("报告目录不是有效目录：{}".format(reports_directory))


def _validate_report_destination(destination: Path, coverage_path: Path) -> None:
    if destination.is_symlink():
        raise ValueError("报告输出不得为符号链接：{}".format(destination))
    if os.path.lexists(str(destination)):
        metadata = destination.stat()
        if not stat.S_ISREG(metadata.st_mode):
            raise ValueError("报告输出必须为普通文件：{}".format(destination))
        if metadata.st_nlink != 1:
            raise ValueError("报告输出不得为硬链接：{}".format(destination))

    resolved_coverage = coverage_path.resolve()
    same_path = destination.resolve(strict=False) == resolved_coverage
    same_file = destination.exists() and destination.samefile(coverage_path)
    if same_path or same_file:
        raise ValueError("报告输出不得覆盖覆盖表输入：{}".format(coverage_path))


def _stage_reports(
    project_root: Path,
    temporary: Path,
    contents: Mapping[str, str],
) -> Dict[str, Path]:
    staged = {}
    for name in REPORT_NAMES:
        path = temporary / name
        atomic_write_text(path, contents[name], root=project_root)
        staged[name] = path

    for name in REPORT_NAMES:
        path = staged[name]
        if path.is_symlink() or not path.is_file() or path.stat().st_nlink != 1:
            raise ValueError("临时报告不是独立普通文件：{}".format(name))
        try:
            actual = path.read_text(encoding="utf-8")
        except (OSError, UnicodeError):
            raise ValueError("临时报告验证失败：{} 无法按 UTF-8 读取".format(name))
        if not actual.strip() or actual != contents[name]:
            raise ValueError("临时报告验证失败：{} 内容不完整".format(name))
    return staged


def _commit_reports(
    reports_directory: Path,
    destinations: Mapping[str, Path],
    staged: Mapping[str, Path],
    temporary: Path,
    coverage_path: Path,
) -> None:
    backups = {}
    promoted = []
    try:
        for name in REPORT_NAMES:
            destination = destinations[name]
            _validate_report_destination(destination, coverage_path)
            if destination.exists():
                backup = temporary / ".old-{}".format(name)
                os.replace(str(destination), str(backup))
                backups[name] = backup

        for name in REPORT_NAMES:
            destination = destinations[name]
            _validate_report_destination(destination, coverage_path)
            os.replace(str(staged[name]), str(destination))
            promoted.append(name)
        fsync_directory(reports_directory)
    except BaseException as error:
        rollback_errors = []
        for name in reversed(REPORT_NAMES):
            destination = destinations[name]
            backup = backups.get(name)
            try:
                if backup is not None and backup.exists():
                    os.replace(str(backup), str(destination))
                elif name in promoted and os.path.lexists(str(destination)):
                    destination.unlink()
            except OSError as rollback_error:
                rollback_errors.append("{}：{}".format(name, rollback_error))
        try:
            fsync_directory(reports_directory)
        except OSError as rollback_error:
            rollback_errors.append("目录同步：{}".format(rollback_error))
        if rollback_errors:
            raise RuntimeError(
                "报告事务失败且回滚不完整：{}；原始错误：{}".format(
                    "；".join(rollback_errors), error
                )
            )
        raise


def build_reports(project: PathLike, coverage: PathLike) -> Dict[str, object]:
    """完成全部输入校验后，原子写入四份固定报告。"""
    project_root = Path(project).expanduser().resolve()
    if not project_root.is_dir():
        raise ValueError("项目根目录不存在：{}".format(project_root))

    coverage_path = Path(coverage).expanduser()
    if not coverage_path.is_absolute():
        coverage_path = project_root / coverage_path
    coverage_rows = read_coverage_tsv(coverage_path)
    coverage_validation = validate_coverage_rows(coverage_rows)
    if not coverage_validation["通过"]:
        raise ValueError(
            "覆盖表校验失败，共{}项错误：{}".format(
                len(coverage_validation["错误"]),
                "；".join(coverage_validation["错误"]),
            )
        )

    reports_directory = project_root / "reports"
    _validate_reports_directory(project_root, reports_directory)
    destinations = {
        name: ensure_safe_output_path(project_root, reports_directory / name)
        for name in REPORT_NAMES
    }
    for destination in destinations.values():
        _validate_report_destination(destination, coverage_path)

    proof = validate_project_coverage(project_root, coverage_path)
    if not proof["通过"]:
        raise ValueError(
            "覆盖表校验失败，共{}项错误：{}".format(
                len(proof["错误"]), "；".join(proof["错误"])
            )
        )
    coverage_rows = proof["覆盖行"]
    coverage_validation = proof["校验"]
    chapter_rows = proof["章节卡片"]
    contents = {
        "覆盖校验.md": _build_coverage_report(
            coverage_rows, coverage_validation
        ),
        "卡片统计.md": _build_statistics_report(chapter_rows),
        "背诵索引.md": _build_memorization_index(chapter_rows),
        "OCR存疑.md": _build_ocr_report(coverage_rows),
    }

    reports_directory.mkdir(parents=True, exist_ok=True)
    _validate_reports_directory(project_root, reports_directory)
    for destination in destinations.values():
        _validate_report_destination(destination, coverage_path)

    with tempfile.TemporaryDirectory(
        prefix=".reports-transaction-", dir=str(reports_directory)
    ) as temporary_name:
        temporary = Path(temporary_name)
        staged = _stage_reports(project_root, temporary, contents)
        _commit_reports(
            reports_directory,
            destinations,
            staged,
            temporary,
            coverage_path,
        )

    return {
        "报告": [destinations[name] for name in REPORT_NAMES],
        "小节数": len(coverage_rows),
        "卡片数": sum(len(rows) for rows in chapter_rows.values()),
    }


def main() -> int:
    parser = ChineseArgumentParser(description="生成覆盖、统计和背诵报告")
    parser.add_argument(
        "--project", required=True, metavar="项目目录", help="课程项目根目录"
    )
    parser.add_argument(
        "--coverage", required=True, metavar="覆盖表", help="固定六字段覆盖表"
    )
    arguments = parser.parse_args()

    try:
        result = build_reports(arguments.project, arguments.coverage)
    except (OSError, ValueError) as error:
        print("报告生成失败：{}".format(error), file=sys.stderr)
        return 1
    print(
        "已生成4份报告，共{}个小节、{}张卡片".format(
            result["小节数"], result["卡片数"]
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
