"""统一审计卡片、报告、DOCX 和 PDF 输出。"""

import csv
import hashlib
import json
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path
from typing import Callable, Dict, List, Mapping, Optional, Sequence, Tuple, Union

try:
    from .build_apkg import MODEL_KEY, MODEL_NAME, stable_guid, stable_id
    from .build_reports import validate_project_coverage
    from .build_print import assemble_markdown
    from .common import (
        CARD_FIELDS,
        ChineseArgumentParser,
        atomic_write_text,
        ensure_safe_output_path,
        read_json,
        read_tsv_strict,
        to_anki_mathjax,
    )
    from .validate_cards import validate_tsv
    from .validate_notes import validate_note
except ImportError:
    from build_apkg import MODEL_KEY, MODEL_NAME, stable_guid, stable_id
    from build_reports import validate_project_coverage
    from build_print import assemble_markdown
    from common import (
        CARD_FIELDS,
        ChineseArgumentParser,
        atomic_write_text,
        ensure_safe_output_path,
        read_json,
        read_tsv_strict,
        to_anki_mathjax,
    )
    from validate_cards import validate_tsv
    from validate_notes import validate_note


PathLike = Union[str, Path]
Runner = Callable[..., subprocess.CompletedProcess]

POPPLER_TOOLS = ("pdfinfo", "pdftotext", "pdftoppm")
A4_POINTS = (595.276, 841.89)
NEAR_BLANK_INK_RATIO = 0.0005
COLLECTION_NAMES = ("collection.anki21", "collection.anki2")
REQUIRED_ANKI_TABLES = {"col", "notes", "cards", "revlog", "graves"}
VISUAL_REVIEW_FIELDS = ["PDF_SHA256", "页码", "状态", "问题"]
VISUAL_REVIEW_FILES = (
    Path("reports/打印视觉复核.tsv"),
    Path("reports/打印视觉复核.md"),
)

WIDTH_ATTRIBUTE_PATTERN = re.compile(r"\{[^{}\n]*\bwidth\s*=", re.IGNORECASE)
BARE_LATEX_PATTERN = re.compile(
    r"\\(?:begin|d?frac|end|int|left|lim|prod|right|sqrt|sum|text)\b"
    r"|[_^]\{"
    r"|(?<!\\)\$(?:[^$\n]|\\.)+(?<!\\)\$"
)


def _project_root(project: PathLike) -> Path:
    root = Path(project).expanduser().resolve()
    if not root.is_dir():
        raise ValueError("项目根目录不存在：{}".format(root))
    return root


def _prepare_project_directory(project: Path, relative: str) -> Path:
    directory = project / relative
    ensure_safe_output_path(project, directory)
    directory.mkdir(parents=True, exist_ok=True)
    ensure_safe_output_path(project, directory)
    return directory


def _add_check(
    checks: List[Dict[str, object]], name: str, passed: bool, detail: str
) -> None:
    checks.append({"项目": name, "通过": bool(passed), "说明": detail})


def _detect_pdf_tools(
    tool_paths: Optional[Mapping[str, str]],
) -> Tuple[Dict[str, str], List[str]]:
    if tool_paths is None:
        tools = {name: shutil.which(name) or "" for name in POPPLER_TOOLS}
    else:
        tools = {name: tool_paths.get(name, "") for name in POPPLER_TOOLS}
    missing = [name for name in POPPLER_TOOLS if not tools[name]]
    return tools, missing


def _coverage_path_from_config(project: Path) -> Path:
    candidates = [project / "build/project.json", project / "project.json"]
    config_path = next((path for path in candidates if path.is_file()), None)
    configured = None
    if config_path is not None:
        payload = read_json(config_path)
        if not isinstance(payload, dict):
            raise ValueError("项目配置必须是 JSON 对象")
        nested = payload.get("config")
        config = nested if isinstance(nested, dict) else payload
        configured = config.get("覆盖表")
        if configured is None:
            configured = config.get("覆盖表路径")
        if configured is None and config is not payload:
            configured = payload.get("覆盖表", payload.get("覆盖表路径"))

    relative = str(configured).strip() if configured is not None else ""
    requested = Path(relative or "reports/覆盖表.tsv").expanduser()
    if not requested.is_absolute():
        requested = project / requested
    return ensure_safe_output_path(project, requested)


def _audit_coverage(project: Path, checks: List[Dict[str, object]]) -> None:
    try:
        coverage = _coverage_path_from_config(project)
        proof = validate_project_coverage(project, coverage)
    except (OSError, UnicodeError, ValueError) as error:
        _add_check(checks, "逐节覆盖与卡片映射", False, str(error))
        return

    _add_check(
        checks,
        "逐节覆盖与卡片映射",
        bool(proof["通过"]),
        "已复用报告校验规则检查 {}，共{}个来源小节，CardIDs 均对应真实卡片。".format(
            Path(proof["覆盖表"]).name,
            len(proof["覆盖行"]),
        )
        if proof["通过"]
        else "覆盖校验失败：{}".format("；".join(proof["错误"])),
    )


def _audit_notes(project: Path, checks: List[Dict[str, object]]) -> None:
    notes_directory = project / "notes"
    files = sorted(notes_directory.glob("*.md"), key=lambda path: path.name.casefold())
    if not files:
        _add_check(
            checks,
            "笔记校验",
            False,
            "notes 目录中没有 Markdown 笔记。",
        )
        return

    problems = []
    for path in files:
        try:
            content = path.read_text(encoding="utf-8")
            result = validate_note(content, note_path=path)
        except (OSError, UnicodeError, ValueError) as error:
            problems.append("{}：读取或校验失败：{}".format(path.name, error))
            continue
        if result["错误"]:
            problems.append(
                "{}：错误：{}".format(path.name, "；".join(result["错误"]))
            )
        if result["警告"]:
            problems.append(
                "{}：警告：{}".format(path.name, "；".join(result["警告"]))
            )

    _add_check(
        checks,
        "笔记校验",
        not problems,
        "已复用 validate_notes 检查{}份笔记，无错误或警告。".format(len(files))
        if not problems
        else "存在笔记错误或警告：{}".format("；".join(problems)),
    )


def _run_tool(
    command: List[str], label: str, runner: Runner
) -> subprocess.CompletedProcess:
    try:
        completed = runner(command, check=False, capture_output=True, text=True)
    except OSError as error:
        raise RuntimeError("{}失败：{}".format(label, error))
    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout or "未提供错误详情").strip()
        raise RuntimeError("{}失败：{}".format(label, detail))
    return completed


def _audit_tsv(project: Path, checks: List[Dict[str, object]]) -> Tuple[int, int]:
    files = sorted((project / "cards").glob("*.tsv"), key=lambda path: path.name.casefold())
    if not files:
        _add_check(checks, "TSV 八字段", False, "cards 目录中没有 TSV 文件。")
        return 0, 0

    card_count = 0
    errors = []
    for path in files:
        result = validate_tsv(path)
        if result["通过"] and not result["警告"]:
            card_count += int(result["卡片数"])
        else:
            details = list(result["错误"])
            details.extend(
                "卡片校验警告（硬门禁）：{}".format(message)
                for message in result["警告"]
            )
            errors.append("{}：{}".format(path.name, "；".join(details)))
    _add_check(
        checks,
        "TSV 八字段",
        not errors,
        "已检查{}份，共{}张卡片。".format(len(files), card_count)
        if not errors
        else "存在不合格 TSV：{}".format("；".join(errors)),
    )
    return len(files), card_count


def _read_metadata_object(value: object, label: str) -> Dict[str, object]:
    try:
        payload = json.loads(value)
    except (TypeError, ValueError):
        raise ValueError("collection 的 {} 元数据不是可读 JSON".format(label))
    if not isinstance(payload, dict) or not payload:
        raise ValueError("collection 的 {} 元数据必须为非空对象".format(label))
    return payload


def _require_table_columns(
    connection: sqlite3.Connection,
    table: str,
    required: Sequence[str],
) -> None:
    columns = {
        str(row[1]) for row in connection.execute("PRAGMA table_info({})".format(table))
    }
    missing = sorted(set(required) - columns)
    if missing:
        raise ValueError(
            "collection 的 {} 表缺少必要列：{}".format(table, "、".join(missing))
        )


def _sqlite_collection_count(
    data: bytes,
    temporary: Path,
    index: int,
    tsv_rows: Sequence[Mapping[str, str]],
) -> int:
    database = temporary / "collection-{}.sqlite3".format(index)
    database.write_bytes(data)
    connection = sqlite3.connect(
        "file:{}?mode=ro".format(database.as_posix()), uri=True
    )
    try:
        tables = {
            str(row[0])
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
        missing_tables = sorted(REQUIRED_ANKI_TABLES - tables)
        if missing_tables:
            raise ValueError(
                "collection 缺少必要 Anki 表：{}".format("、".join(missing_tables))
            )
        _require_table_columns(connection, "col", ["models", "decks"])
        _require_table_columns(
            connection, "notes", ["id", "guid", "mid", "flds", "tags"]
        )
        _require_table_columns(connection, "cards", ["id", "nid", "did"])

        metadata = connection.execute("SELECT models, decks FROM col").fetchone()
        if metadata is None:
            raise ValueError("collection 的 col 表没有 deck/model 元数据")
        models = _read_metadata_object(metadata[0], "model")
        decks = _read_metadata_object(metadata[1], "deck")

        notes = connection.execute(
            "SELECT id, guid, mid, flds, tags FROM notes"
        ).fetchall()
        cards = connection.execute("SELECT nid, did FROM cards").fetchall()
    finally:
        connection.close()

    expected_notes = {}
    for row in tsv_rows:
        guid = stable_guid(row["Source"], row["Front"])
        expected_notes[guid] = {
            "fields": [to_anki_mathjax(row[field]) for field in CARD_FIELDS],
            "tags": {row["Tags"], row["Importance"]},
        }
    expected_guids = set(expected_notes)
    if len(expected_guids) != len(tsv_rows):
        raise ValueError("同名 TSV 中存在重复 stable GUID")
    actual_guids = [str(row[1]) for row in notes]
    if len(set(actual_guids)) != len(actual_guids):
        raise ValueError("collection 的 notes.guid 存在重复 GUID")
    actual_guid_set = set(actual_guids)
    if actual_guid_set != expected_guids:
        missing = sorted(expected_guids - actual_guid_set)
        stale = sorted(actual_guid_set - expected_guids)
        details = []
        if missing:
            details.append("缺少 {}".format("、".join(missing)))
        if stale:
            details.append("陈旧或跨章 {}".format("、".join(stale)))
        raise ValueError("GUID 集合与同名 TSV 不一致：{}".format("；".join(details)))

    expected_model_id = stable_id("model", MODEL_KEY)
    model = models.get(str(expected_model_id))
    if not isinstance(model, dict):
        raise ValueError(
            "model id 必须等于 build_apkg 稳定 ID：{}".format(
                expected_model_id
            )
        )
    if (
        str(model.get("id")) != str(expected_model_id)
        or model.get("name") != MODEL_NAME
    ):
        raise ValueError("model id/name 与 build_apkg 常量不一致")
    field_items = model.get("flds")
    if not isinstance(field_items, list):
        raise ValueError("model 缺少可读字段元数据")
    field_names = [
        item.get("name") if isinstance(item, dict) else None
        for item in field_items
    ]
    if field_names != CARD_FIELDS:
        raise ValueError("model 字段顺序不是规定的八字段模型")

    note_ids = set()
    notes_by_guid = {str(row[1]): row for row in notes}
    for guid, expected in expected_notes.items():
        note_id, _, model_id, fields_text, tags_text = notes_by_guid[guid]
        note_ids.add(int(note_id))
        if int(model_id) != expected_model_id:
            raise ValueError(
                "note {} 的 model id 与 build_apkg 稳定 ID 不一致".format(
                    guid
                )
            )
        fields = str(fields_text).split("\x1f")
        if len(fields) != len(CARD_FIELDS):
            raise ValueError(
                "note {} 字段数为{}，必须为8".format(guid, len(fields))
            )
        for field_name, actual, wanted in zip(
            CARD_FIELDS, fields, expected["fields"]
        ):
            if actual != wanted:
                raise ValueError(
                    "note {} 的 {} 与同名 TSV 打包预期不一致".format(
                        guid, field_name
                    )
                )
        if set(str(tags_text).split()) != expected["tags"]:
            raise ValueError(
                "note {} 的 Tags/Importance 与同名 TSV 不一致".format(guid)
            )

    card_note_ids = {int(row[0]) for row in cards}
    if len(cards) != len(notes) or card_note_ids != note_ids:
        missing_cards = sorted(note_ids - card_note_ids)
        orphan_cards = sorted(card_note_ids - note_ids)
        details = []
        if missing_cards:
            details.append("notes 缺少 cards：{}".format("、".join(map(str, missing_cards))))
        if orphan_cards:
            details.append("cards 引用了不存在的 notes：{}".format("、".join(map(str, orphan_cards))))
        raise ValueError("notes/cards 对应关系无效：{}".format("；".join(details)))

    used_deck_ids = {int(row[1]) for row in cards}
    if len(used_deck_ids) != 1:
        raise ValueError("cards 必须全部属于同一个当前 deck")
    deck_id = next(iter(used_deck_ids))
    deck = decks.get(str(deck_id))
    if not isinstance(deck, dict):
        raise ValueError("cards 使用的 deck {} 在元数据中不存在".format(deck_id))
    deck_name = str(deck.get("name", "")).strip()
    if not deck_name:
        raise ValueError("deck {} 名称为空".format(deck_id))
    expected_deck_id = stable_id("deck", deck_name)
    if deck.get("id") != deck_id or deck_id != expected_deck_id:
        raise ValueError("deck id/name 与 build_apkg 稳定 ID 规则不一致")
    if any(int(row[1]) != deck_id for row in cards):
        raise ValueError("cards 与当前 deck 的关联不一致")
    return len(notes)


def _audit_apkg(
    project: Path, temporary: Path, checks: List[Dict[str, object]]
) -> Tuple[int, Optional[int]]:
    cards_directory = project / "cards"
    files = sorted(cards_directory.glob("*.apkg"), key=lambda path: path.name.casefold())
    tsv_files = sorted(cards_directory.glob("*.tsv"), key=lambda path: path.name.casefold())
    if not files:
        _add_check(checks, "APKG 逐章配对与结构", False, "cards 目录中没有 APKG 文件。")
        return 0, None

    apkg_by_stem = {path.stem: path for path in files}
    tsv_by_stem = {path.stem: path for path in tsv_files}
    total = 0
    errors = []
    missing_apkg = sorted(set(tsv_by_stem) - set(apkg_by_stem))
    missing_tsv = sorted(set(apkg_by_stem) - set(tsv_by_stem))
    if missing_apkg:
        errors.append("缺少同名 APKG：{}".format("、".join(missing_apkg)))
    if missing_tsv:
        errors.append("APKG 找不到同名 TSV：{}".format("、".join(missing_tsv)))

    paired_stems = sorted(set(apkg_by_stem) & set(tsv_by_stem), key=str.casefold)
    for index, stem in enumerate(paired_stems, start=1):
        path = apkg_by_stem[stem]
        try:
            tsv_rows = read_tsv_strict(tsv_by_stem[stem])
            with zipfile.ZipFile(path) as archive:
                damaged = archive.testzip()
                if damaged is not None:
                    raise ValueError("压缩成员损坏：{}".format(damaged))
                names = set(archive.namelist())
                collection = next(
                    (name for name in COLLECTION_NAMES if name in names), None
                )
                if collection is None:
                    raise ValueError("缺少 collection.anki2 或 collection.anki21")
                total += _sqlite_collection_count(
                    archive.read(collection), temporary, index, tsv_rows
                )
        except (OSError, sqlite3.DatabaseError, ValueError, zipfile.BadZipFile) as error:
            if isinstance(error, zipfile.BadZipFile):
                detail = "APKG 无法解压"
            elif not zipfile.is_zipfile(path):
                detail = "APKG 无法解压"
            else:
                detail = str(error)
            errors.append("{}：{}".format(path.name, detail))

    _add_check(
        checks,
        "APKG 逐章配对与结构",
        not errors,
        "已逐章检查{}对同名 TSV/APKG，collection 共{}张卡片，schema、八字段、GUID、deck/model 均一致。".format(
            len(paired_stems), total
        )
        if not errors
        else "存在不合格 APKG 或逐章配对：{}".format("；".join(errors)),
    )
    return len(files), total if not errors else None


def _expected_print_structure(project: Path) -> Tuple[int, int, int, Optional[str]]:
    try:
        _, details = assemble_markdown(project, "输出审计")
        return (
            int(details["章节数"]),
            int(details["预期图片数"]),
            int(details["预期公式数"]),
            None,
        )
    except (OSError, UnicodeError, ValueError) as error:
        return 0, 0, 0, str(error)


def _audit_docx(
    project: Path,
    expected_images: int,
    expected_formulas: int,
    checks: List[Dict[str, object]],
) -> None:
    files = sorted((project / "print").glob("*.docx"), key=lambda path: path.name.casefold())
    if len(files) != 1:
        _add_check(
            checks,
            "DOCX 结构",
            False,
            "print 目录应恰好有1份 DOCX，实际为{}份。".format(len(files)),
        )
        return

    path = files[0]
    try:
        with zipfile.ZipFile(path) as archive:
            damaged = archive.testzip()
            if damaged is not None:
                raise ValueError("DOCX 压缩成员损坏：{}".format(damaged))
            names = set(archive.namelist())
            required = {"[Content_Types].xml", "_rels/.rels", "word/document.xml"}
            missing = sorted(required - names)
            if missing:
                raise ValueError("DOCX 缺少 {}".format("、".join(missing)))
            document = archive.read("word/document.xml")
            images = sum(name.startswith("word/media/") for name in names)
            formulas = len(
                re.findall(rb"<(?:[A-Za-z_][\w.-]*:)?oMath(?:\s|>)", document)
            )
    except (OSError, ValueError, zipfile.BadZipFile) as error:
        _add_check(checks, "DOCX 结构", False, str(error))
        return

    errors = []
    if images < expected_images:
        errors.append(
            "预期图片至少{}张，DOCX 中只有{}张".format(expected_images, images)
        )
    if formulas < expected_formulas:
        errors.append(
            "预期公式对象至少{}个，DOCX 中只有{}个".format(
                expected_formulas, formulas
            )
        )
    _add_check(
        checks,
        "DOCX 结构",
        not errors,
        "ZIP 完整，含{}张图片和{}个公式对象。".format(images, formulas)
        if not errors
        else "；".join(errors),
    )


def _parse_pdfinfo(output: str) -> Tuple[Optional[int], Optional[Tuple[float, float]]]:
    page_match = re.search(r"^Pages:\s*(\d+)\s*$", output, re.MULTILINE)
    size_match = re.search(
        r"^Page(?:\s+\d+)? size:\s*([0-9.]+)\s+x\s+([0-9.]+)\s+pts",
        output,
        re.MULTILINE | re.IGNORECASE,
    )
    pages = int(page_match.group(1)) if page_match else None
    size = (
        (float(size_match.group(1)), float(size_match.group(2)))
        if size_match
        else None
    )
    return pages, size


def _is_a4(size: Tuple[float, float]) -> bool:
    width, height = size
    expected_width, expected_height = A4_POINTS
    portrait = abs(width - expected_width) <= 3 and abs(height - expected_height) <= 3
    landscape = abs(width - expected_height) <= 3 and abs(height - expected_width) <= 3
    return portrait or landscape


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    try:
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
    except OSError:
        raise ValueError("PDF SHA-256 计算失败：{}".format(path.name))
    return digest.hexdigest()


def _read_pgm_ink_ratio(path: Path) -> float:
    with path.open("rb") as handle:
        magic = handle.readline().strip()
        if magic != b"P5":
            raise ValueError("渲染页不是 PGM P5 格式：{}".format(path.name))

        tokens = []
        while len(tokens) < 3:
            line = handle.readline()
            if not line:
                raise ValueError("PGM 文件头不完整：{}".format(path.name))
            line = line.split(b"#", 1)[0]
            tokens.extend(line.split())
        width, height, maximum = (int(token) for token in tokens[:3])
        if width <= 0 or height <= 0 or maximum <= 0 or maximum > 255:
            raise ValueError("PGM 参数非法：{}".format(path.name))
        pixels = handle.read(width * height)
        if len(pixels) != width * height:
            raise ValueError("PGM 像素数据不完整：{}".format(path.name))
    threshold = int(maximum * 0.96)
    ink = sum(pixel < threshold for pixel in pixels)
    return ink / float(width * height)


def _audit_pdf(
    project: Path,
    expected_chapters: int,
    temporary: Path,
    tools: Mapping[str, str],
    missing_tools: Sequence[str],
    runner: Runner,
    checks: List[Dict[str, object]],
) -> Optional[Tuple[int, str]]:
    files = sorted((project / "print").glob("*.pdf"), key=lambda path: path.name.casefold())
    if len(files) != 1:
        _add_check(
            checks,
            "PDF 文件",
            False,
            "print 目录应恰好有1份 PDF，实际为{}份。".format(len(files)),
        )
        return None
    pdf = files[0]
    try:
        pdf_sha256 = _sha256_file(pdf)
        signature = pdf.read_bytes()[:5]
    except (OSError, ValueError) as error:
        _add_check(checks, "PDF 文件", False, "PDF 读取失败：{}".format(error))
        return None
    if signature != b"%PDF-":
        _add_check(checks, "PDF 文件", False, "文件缺少 PDF 签名。")
        return None
    _add_check(checks, "PDF 文件", True, "已找到有效 PDF 签名。")

    if missing_tools:
        _add_check(
            checks,
            "PDF 外部工具",
            False,
            "缺少 {}。请安装 Poppler 后重试：brew install poppler".format(
                "、".join(missing_tools)
            ),
        )
        return None
    _add_check(checks, "PDF 外部工具", True, "pdfinfo、pdftotext、pdftoppm 均可用。")

    try:
        general = _run_tool([tools["pdfinfo"], str(pdf)], "pdfinfo", runner)
        page_count, _ = _parse_pdfinfo(general.stdout)
        if page_count is None or page_count < 1:
            raise ValueError("pdfinfo 未返回有效页数")
        sizes = []
        for page in range(1, page_count + 1):
            output = _run_tool(
                [
                    tools["pdfinfo"],
                    "-f",
                    str(page),
                    "-l",
                    str(page),
                    str(pdf),
                ],
                "pdfinfo 第{}页".format(page),
                runner,
            )
            _, size = _parse_pdfinfo(output.stdout)
            if size is None:
                raise ValueError("pdfinfo 未返回第{}页尺寸".format(page))
            sizes.append(size)
        bad_pages = [index + 1 for index, size in enumerate(sizes) if not _is_a4(size)]
        reasonable = page_count > expected_chapters if expected_chapters else page_count >= 1
        errors = []
        if bad_pages:
            errors.append("非 A4 页面：{}".format("、".join(map(str, bad_pages))))
        if not reasonable:
            errors.append(
                "PDF 页数{}不大于纳入章节数{}，页数不合理".format(
                    page_count, expected_chapters
                )
            )
        _add_check(
            checks,
            "PDF A4与页数",
            not errors,
            "共{}页，全部为 A4。".format(page_count)
            if not errors
            else "；".join(errors),
        )

        extracted = _run_tool(
            [tools["pdftotext"], "-layout", str(pdf), "-"],
            "pdftotext",
            runner,
        ).stdout
        text_errors = []
        if WIDTH_ATTRIBUTE_PATTERN.search(extracted):
            text_errors.append("发现原样输出的 {width=...}")
        if BARE_LATEX_PATTERN.search(extracted):
            text_errors.append("发现已知裸露 LaTeX")
        _add_check(
            checks,
            "PDF 文本",
            not text_errors,
            "未发现尺寸属性或已知裸露 LaTeX。"
            if not text_errors
            else "；".join(text_errors),
        )

        prefix = temporary / "页面"
        _run_tool(
            [
                tools["pdftoppm"],
                "-gray",
                "-r",
                "72",
                str(pdf),
                str(prefix),
            ],
            "pdftoppm 全页渲染",
            runner,
        )
        rendered = sorted(
            temporary.glob("页面-*.pgm"),
            key=lambda path: int(re.search(r"-(\d+)\.pgm$", path.name).group(1)),
        )
        if len(rendered) != page_count:
            raise ValueError(
                "pdftoppm 应渲染{}页，实际得到{}页".format(
                    page_count, len(rendered)
                )
            )
        blank_pages = [
            index + 1
            for index, path in enumerate(rendered)
            if _read_pgm_ink_ratio(path) < NEAR_BLANK_INK_RATIO
        ]
        _add_check(
            checks,
            "PDF 全页渲染",
            not blank_pages,
            "已成功渲染全部{}页，近空白检测未发现异常。".format(page_count)
            if not blank_pages
            else "发现近空白页：{}。".format("、".join(map(str, blank_pages))),
        )
        if _sha256_file(pdf) != pdf_sha256:
            raise ValueError("PDF 在自动检查期间发生变化，SHA-256 已失效")
        return page_count, pdf_sha256
    except (OSError, RuntimeError, ValueError) as error:
        _add_check(checks, "PDF 全页渲染", False, str(error))
        return None


def _read_visual_review_tsv(path: Path) -> List[Dict[str, str]]:
    try:
        with path.open("r", encoding="utf-8-sig", newline="") as handle:
            reader = csv.reader(handle, delimiter="\t", strict=True)
            try:
                header = next(reader)
            except StopIteration:
                raise ValueError("打印视觉复核 TSV 为空，缺少表头")
            if header != VISUAL_REVIEW_FIELDS:
                raise ValueError(
                    "打印视觉复核 TSV 表头必须严格为："
                    + "、".join(VISUAL_REVIEW_FIELDS)
                )
            rows = []
            for line_number, values in enumerate(reader, start=2):
                if len(values) != len(VISUAL_REVIEW_FIELDS):
                    raise ValueError(
                        "打印视觉复核 TSV 第{}行列数为{}，必须为4列".format(
                            line_number, len(values)
                        )
                    )
                rows.append(dict(zip(VISUAL_REVIEW_FIELDS, values)))
            return rows
    except csv.Error:
        raise ValueError("打印视觉复核 TSV 无法按制表符格式解析")
    except UnicodeError:
        raise ValueError("打印视觉复核 TSV 必须使用 UTF-8 编码")
    except OSError:
        raise ValueError("打印视觉复核 TSV 读取失败")


def _markdown_cells(line: str) -> Optional[List[str]]:
    stripped = line.strip()
    if not stripped.startswith("|") or not stripped.endswith("|"):
        return None
    return [cell.strip() for cell in stripped[1:-1].split("|")]


def _read_visual_review_markdown(path: Path) -> List[Dict[str, str]]:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except UnicodeError:
        raise ValueError("打印视觉复核 Markdown 必须使用 UTF-8 编码")
    except OSError:
        raise ValueError("打印视觉复核 Markdown 读取失败")

    for index, line in enumerate(lines[:-1]):
        if _markdown_cells(line) != VISUAL_REVIEW_FIELDS:
            continue
        separator = _markdown_cells(lines[index + 1])
        if separator is None or len(separator) != len(VISUAL_REVIEW_FIELDS):
            continue
        if not all(re.fullmatch(r":?-{3,}:?", cell) for cell in separator):
            continue
        rows = []
        for row_line in lines[index + 2:]:
            cells = _markdown_cells(row_line)
            if cells is None:
                if rows:
                    break
                continue
            if len(cells) != len(VISUAL_REVIEW_FIELDS):
                raise ValueError("打印视觉复核 Markdown 表格必须为4列")
            rows.append(dict(zip(VISUAL_REVIEW_FIELDS, cells)))
        return rows
    raise ValueError(
        "打印视觉复核 Markdown 缺少“PDF_SHA256/页码/状态/问题”四列表格"
    )


def _validate_visual_review_rows(
    rows: Sequence[Mapping[str, str]], page_count: int, pdf_sha256: str
) -> List[str]:
    errors = []
    seen = {}
    for row_number, row in enumerate(rows, start=1):
        recorded_hash = str(row.get("PDF_SHA256", "")).strip().lower()
        if not re.fullmatch(r"[0-9a-f]{64}", recorded_hash):
            errors.append(
                "第{}条视觉复核记录 PDF_SHA256 不是64位十六进制".format(
                    row_number
                )
            )
        elif recorded_hash != pdf_sha256:
            errors.append(
                "第{}条视觉复核记录 SHA-256 与当前 PDF 不一致".format(
                    row_number
                )
            )
        page_text = str(row.get("页码", "")).strip()
        try:
            page = int(page_text)
        except ValueError:
            errors.append("第{}条视觉复核记录页码不是正整数".format(row_number))
            continue
        if page < 1:
            errors.append("第{}条视觉复核记录页码不是正整数".format(row_number))
            continue
        if page in seen:
            errors.append(
                "第{}条与第{}条视觉复核记录页码重复：{}".format(
                    row_number, seen[page], page
                )
            )
        else:
            seen[page] = row_number
        status = str(row.get("状态", "")).strip()
        if status != "已通过":
            errors.append("第{}页状态不是已通过：{}".format(page, status or "为空"))

    expected = set(range(1, page_count + 1))
    actual = set(seen)
    missing = sorted(expected - actual)
    extra = sorted(actual - expected)
    if missing:
        errors.append("缺少页码：{}".format("、".join(map(str, missing))))
    if extra:
        errors.append("存在超出 PDF 页数的页码：{}".format("、".join(map(str, extra))))
    if not rows:
        errors.append("打印视觉复核没有逐页记录")
    return errors


def _audit_visual_review(
    project: Path,
    page_count: Optional[int],
    pdf_sha256: Optional[str],
    checks: List[Dict[str, object]],
) -> None:
    if page_count is None or pdf_sha256 is None:
        _add_check(
            checks,
            "PDF 视觉复核记录",
            False,
            "PDF 自动检查未获得有效页数，无法核对视觉复核记录。",
        )
        return

    pdf_files = sorted(
        (project / "print").glob("*.pdf"), key=lambda item: item.name.casefold()
    )
    if len(pdf_files) != 1:
        _add_check(
            checks,
            "PDF 视觉复核记录",
            False,
            "print 目录应恰好有1份当前 PDF，实际为{}份。".format(len(pdf_files)),
        )
        return
    try:
        current_hash = _sha256_file(pdf_files[0])
    except ValueError as error:
        _add_check(checks, "PDF 视觉复核记录", False, str(error))
        return
    if current_hash != pdf_sha256:
        _add_check(
            checks,
            "PDF 视觉复核记录",
            False,
            "当前 PDF 在自动检查后发生变化，SHA-256 绑定无效。",
        )
        return

    path = next(
        (project / relative for relative in VISUAL_REVIEW_FILES if (project / relative).is_file()),
        None,
    )
    if path is None:
        _add_check(
            checks,
            "PDF 视觉复核记录",
            False,
            "缺少打印视觉复核记录：reports/打印视觉复核.tsv 或 reports/打印视觉复核.md。",
        )
        return

    try:
        ensure_safe_output_path(project, path)
        rows = (
            _read_visual_review_tsv(path)
            if path.suffix.lower() == ".tsv"
            else _read_visual_review_markdown(path)
        )
        errors = _validate_visual_review_rows(rows, page_count, pdf_sha256)
    except (OSError, UnicodeError, ValueError) as error:
        _add_check(checks, "PDF 视觉复核记录", False, str(error))
        return

    _add_check(
        checks,
        "PDF 视觉复核记录",
        not errors,
        "视觉复核已记录并绑定当前 PDF：{}覆盖全部{}页，SHA-256={}，状态均为已通过。".format(
            path.name, page_count, pdf_sha256
        )
        if not errors
        else "视觉复核记录未通过：{}".format("；".join(errors)),
    )


def _report_card_count(project: Path) -> Tuple[Optional[int], Optional[str]]:
    path = project / "reports/卡片统计.md"
    if not path.is_file():
        return None, "缺少 reports/卡片统计.md"
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as error:
        return None, "卡片统计报告读取失败：{}".format(error)
    match = re.search(r"^\|\s*合计\s*\|\s*(\d+)\s*\|", text, re.MULTILINE)
    if match is None:
        return None, "卡片统计报告缺少“合计”卡片数"
    return int(match.group(1)), None


def _audit_counts(
    project: Path,
    tsv_files: int,
    tsv_cards: int,
    apkg_files: int,
    apkg_cards: Optional[int],
    checks: List[Dict[str, object]],
) -> Optional[int]:
    report_cards, report_error = _report_card_count(project)
    errors = []
    if report_error:
        errors.append(report_error)
    if tsv_files != apkg_files:
        errors.append(
            "TSV 为{}份，APKG 为{}份".format(tsv_files, apkg_files)
        )
    if apkg_cards is None:
        errors.append("APKG collection 卡片数不可用")
    elif report_cards is not None and not (
        tsv_cards == apkg_cards == report_cards
    ):
        errors.append(
            "TSV/APKG/报告数量不一致：{}/{}/{}".format(
                tsv_cards, apkg_cards, report_cards
            )
        )
    _add_check(
        checks,
        "TSV/APKG/报告数量",
        not errors,
        "三者均为{}张，且 TSV/APKG 文件均为{}份。".format(
            tsv_cards, tsv_files
        )
        if not errors
        else "；".join(errors),
    )
    return report_cards


def _render_report(result: Mapping[str, object]) -> str:
    lines = ["# 输出审计 QA 清单", ""]
    for item in result["检查"]:
        marker = "x" if item["通过"] else " "
        lines.append(
            "- [{}] {}：{}".format(marker, item["项目"], item["说明"])
        )
    statistics = result["统计"]
    lines.extend(
        [
            "",
            "## 数量",
            "",
            "- TSV 文件：{}份".format(statistics["TSV文件数"]),
            "- TSV 卡片：{}张".format(statistics["TSV卡片数"]),
            "- APKG 文件：{}份".format(statistics["APKG文件数"]),
            "- APKG 卡片：{}".format(
                "不可用"
                if statistics["APKG卡片数"] is None
                else "{}张".format(statistics["APKG卡片数"])
            ),
            "- 统计报告卡片：{}".format(
                "不可用"
                if statistics["报告卡片数"] is None
                else "{}张".format(statistics["报告卡片数"])
            ),
            "- PDF 页数：{}".format(
                "不可用"
                if statistics["PDF页数"] is None
                else "{}页".format(statistics["PDF页数"])
            ),
            "- PDF SHA-256：{}".format(
                "不可用"
                if statistics["PDF_SHA256"] is None
                else statistics["PDF_SHA256"]
            ),
            "",
            "## 结论",
            "",
            "输出审计通过。" if result["通过"] else "输出审计未通过，须修复失败项后重跑。",
            "",
        ]
    )
    return "\n".join(lines)


def audit_outputs(
    project: PathLike,
    tool_paths: Optional[Mapping[str, str]] = None,
    runner: Runner = subprocess.run,
) -> Dict[str, object]:
    """审计项目成品并原子写入中文 QA 清单。"""
    root = _project_root(project)
    reports = _prepare_project_directory(root, "reports")
    build = _prepare_project_directory(root, "build")
    report_path = reports / "输出审计.md"
    ensure_safe_output_path(root, report_path)

    checks = []
    tools, missing_tools = _detect_pdf_tools(tool_paths)
    expected_chapters, expected_images, expected_formulas, config_error = (
        _expected_print_structure(root)
    )
    _add_check(
        checks,
        "打印配置与章节",
        config_error is None,
        "已按配置确认{}章。".format(expected_chapters)
        if config_error is None
        else "配置或章节笔记无效：{}".format(config_error),
    )
    _audit_coverage(root, checks)
    _audit_notes(root, checks)

    with tempfile.TemporaryDirectory(prefix=".output-audit-", dir=str(build)) as name:
        temporary = Path(name)
        tsv_files, tsv_cards = _audit_tsv(root, checks)
        apkg_files, apkg_cards = _audit_apkg(root, temporary, checks)
        _audit_docx(root, expected_images, expected_formulas, checks)
        pdf_check_start = len(checks)
        pdf_identity = _audit_pdf(
            root,
            expected_chapters,
            temporary,
            tools,
            missing_tools,
            runner,
            checks,
        )
        if pdf_identity is None:
            pdf_pages = None
            pdf_sha256 = None
        else:
            pdf_pages, pdf_sha256 = pdf_identity
        pdf_automatic_checks = checks[pdf_check_start:]
        pdf_automatic_passed = pdf_pages is not None and all(
            bool(item["通过"]) for item in pdf_automatic_checks
        )
        _add_check(
            checks,
            "PDF 自动检查",
            pdf_automatic_passed,
            (
                "PDF 自动检查通过：已确认 A4、文本规则、成功全页渲染和近空白检测。"
                if pdf_automatic_passed
                else "PDF 自动检查未通过：请查看上述自动检查失败项。"
            ),
        )
        _audit_visual_review(root, pdf_pages, pdf_sha256, checks)
        report_cards = _audit_counts(
            root,
            tsv_files,
            tsv_cards,
            apkg_files,
            apkg_cards,
            checks,
        )

    result = {
        "通过": all(bool(item["通过"]) for item in checks),
        "检查": checks,
        "统计": {
            "TSV文件数": tsv_files,
            "TSV卡片数": tsv_cards,
            "APKG文件数": apkg_files,
            "APKG卡片数": apkg_cards,
            "报告卡片数": report_cards,
            "PDF页数": pdf_pages,
            "PDF_SHA256": pdf_sha256,
        },
        "报告": report_path,
    }
    atomic_write_text(report_path, _render_report(result), root=root)
    return result


def main() -> int:
    parser = ChineseArgumentParser(description="统一审计考试制卡项目输出")
    parser.add_argument("--project", required=True, help="课程项目根目录")
    arguments = parser.parse_args()
    try:
        result = audit_outputs(arguments.project)
    except (OSError, UnicodeError, ValueError) as error:
        print("输出审计失败：{}".format(error), file=sys.stderr)
        return 1
    if result["通过"]:
        print("输出审计通过：{}".format(result["报告"]))
        return 0
    print("输出审计未通过：{}".format(result["报告"]), file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
