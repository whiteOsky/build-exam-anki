"""把通过校验的八字段 TSV 原子打包为独立 Anki 牌组。"""

import hashlib
import json
import os
import shutil
import sqlite3
import sys
import tempfile
import zipfile
from pathlib import Path
from typing import Dict, Optional, Union

try:
    import genanki
except ImportError:
    genanki = None

try:
    from .common import (
        CARD_FIELDS,
        ChineseArgumentParser,
        ensure_safe_output_path,
        fsync_directory,
        normalize_front,
        read_tsv_strict,
        to_anki_mathjax,
    )
    from .validate_cards import validate_rows
except ImportError:
    from common import (
        CARD_FIELDS,
        ChineseArgumentParser,
        ensure_safe_output_path,
        fsync_directory,
        normalize_front,
        read_tsv_strict,
        to_anki_mathjax,
    )
    from validate_cards import validate_rows


PathLike = Union[str, Path]
DEFAULT_TEMPLATE_DIR = Path(__file__).resolve().parents[1] / "assets/anki-card-template"
MODEL_NAME = "考研高密全覆盖八字段"
MODEL_KEY = "build-exam-anki-v1"
INSTALL_HINT = (
    "缺少 genanki。请在技能目录运行："
    ".venv/bin/pip install -r requirements.txt"
)


def stable_id(namespace: str, value: str) -> int:
    """用固定命名空间生成 genanki 可接受的稳定正整数。"""
    digest = hashlib.sha256(
        "{}:{}".format(namespace, value).encode("utf-8")
    ).digest()
    identifier = int.from_bytes(digest[:4], "big") & 0x7FFFFFFF
    return identifier or 1


def stable_guid(source: str, front: str) -> str:
    """由去除首尾空白的 Source 和规范化 Front 生成稳定 GUID。"""
    identity = "{}\n{}".format(str(source).strip(), normalize_front(front))
    return hashlib.sha256(identity.encode("utf-8")).hexdigest()[:20]


def _load_template(path: Path, label: str) -> str:
    try:
        content = path.read_text(encoding="utf-8")
    except (OSError, UnicodeError):
        raise ValueError("无法读取{}模板：{}".format(label, path))
    if not content.strip():
        raise ValueError("{}模板为空：{}".format(label, path))
    return content


def _verify_collection(
    collection_path: Path,
    model_id: int,
    deck_id: int,
    deck_name: str,
    expected_notes: Dict[str, Dict[str, object]],
) -> None:
    connection = None
    try:
        connection = sqlite3.connect(str(collection_path))
        collection_row = connection.execute(
            "SELECT models, decks FROM col"
        ).fetchone()
        if collection_row is None:
            raise ValueError("APKG collection 缺少集合元数据")
        models = json.loads(collection_row[0])
        decks = json.loads(collection_row[1])
        note_records = connection.execute(
            "SELECT id, guid, mid, flds, tags FROM notes"
        ).fetchall()
        card_records = connection.execute(
            "SELECT nid, did FROM cards"
        ).fetchall()
    except sqlite3.Error:
        raise ValueError("APKG collection 无法读取或数据库结构错误")
    except (json.JSONDecodeError, TypeError):
        raise ValueError("APKG collection 的模型或牌组元数据无效")
    finally:
        if connection is not None:
            connection.close()

    if not isinstance(models, dict) or not isinstance(decks, dict):
        raise ValueError("APKG collection 的模型或牌组元数据无效")

    model = models.get(str(model_id))
    if not isinstance(model, dict) or model.get("name") != MODEL_NAME:
        raise ValueError("APKG collection 中的八字段模型不正确")
    field_definitions = model.get("flds")
    if not isinstance(field_definitions, list) or any(
        not isinstance(field, dict) for field in field_definitions
    ):
        raise ValueError("APKG collection 的模型字段元数据无效")
    model_fields = [field.get("name") for field in field_definitions]
    if model_fields != CARD_FIELDS:
        raise ValueError("APKG collection 模型必须严格包含八个固定字段")

    deck = decks.get(str(deck_id))
    if (
        not isinstance(deck, dict)
        or deck.get("id") != deck_id
        or deck.get("name") != deck_name
    ):
        raise ValueError("APKG collection 的牌组 ID 或名称不正确")

    actual_notes = {record[1]: record for record in note_records}
    if (
        len(note_records) != len(expected_notes)
        or set(actual_notes) != set(expected_notes)
    ):
        raise ValueError("APKG collection 的卡片数量或 note GUID 不正确")

    note_ids = set()
    for guid, expected in expected_notes.items():
        note_id, _, note_model_id, fields_text, tags_text = actual_notes[guid]
        if not isinstance(fields_text, str) or not isinstance(tags_text, str):
            raise ValueError("APKG collection 的 note 字段或标签无效")
        fields = fields_text.split("\x1f")
        if note_model_id != model_id or len(fields) != len(CARD_FIELDS):
            raise ValueError("APKG collection 的 note 模型或字段数量不正确")
        if fields != expected["fields"]:
            raise ValueError("APKG collection 的 note 字段内容不正确")
        if set(tags_text.split()) != set(expected["tags"]):
            raise ValueError("APKG collection 的 note 标签不正确")
        note_ids.add(note_id)

    if (
        len(card_records) != len(expected_notes)
        or {record[0] for record in card_records} != note_ids
        or any(record[1] != deck_id for record in card_records)
    ):
        raise ValueError("APKG collection 的卡片与牌组关联不正确")


def _verify_package(
    path: Path,
    model_id: int,
    deck_id: int,
    deck_name: str,
    expected_notes: Dict[str, Dict[str, object]],
) -> None:
    collection_path = None
    try:
        try:
            with zipfile.ZipFile(str(path)) as archive:
                broken = archive.testzip()
                names = set(archive.namelist())
                collection_name = next(
                    (
                        name
                        for name in ["collection.anki21", "collection.anki2"]
                        if name in names
                    ),
                    None,
                )
                if collection_name is not None:
                    descriptor, temporary_name = tempfile.mkstemp(
                        prefix="build-exam-anki-collection-",
                        suffix=Path(collection_name).suffix,
                    )
                    collection_path = Path(temporary_name)
                    with os.fdopen(descriptor, "wb") as target:
                        with archive.open(collection_name) as source:
                            shutil.copyfileobj(source, target)
        except (OSError, zipfile.BadZipFile):
            raise ValueError("APKG 打包结果不是有效压缩包")

        if broken is not None:
            raise ValueError("APKG 打包结果包含损坏文件：{}".format(broken))
        if collection_path is None:
            raise ValueError("APKG 打包结果缺少 Anki collection")
        _verify_collection(
            collection_path,
            model_id,
            deck_id,
            deck_name,
            expected_notes,
        )
    finally:
        if collection_path is not None:
            try:
                collection_path.unlink()
            except FileNotFoundError:
                pass


def _safe_output_path(tsv: Path, output: PathLike) -> Path:
    destination = ensure_safe_output_path(tsv.parent, output)
    if destination.suffix.lower() != ".apkg":
        raise ValueError("输出文件必须使用 .apkg 扩展名")
    if destination == tsv:
        raise ValueError("APKG 输出不得覆盖源 TSV")
    if destination.is_symlink():
        raise ValueError("APKG 输出不得使用符号链接")
    if destination.exists() and os.path.samefile(str(tsv), str(destination)):
        raise ValueError("APKG 输出与源 TSV 指向同一文件")
    return destination


def _existing_output_error(destination: Path) -> FileExistsError:
    return FileExistsError(
        "APKG 输出文件已存在，拒绝覆盖；请明确使用 force=True 或 CLI --force：{}".format(
            destination
        )
    )


def build_package(
    tsv: PathLike,
    output: PathLike,
    deck_name: str,
    template_dir: Optional[PathLike] = None,
    force: bool = False,
) -> int:
    """严格校验 TSV，并以临时文件构建和提交 APKG。"""
    source = Path(tsv).expanduser().resolve()
    rows = read_tsv_strict(source)
    validation = validate_rows(rows)
    if not validation["通过"]:
        details = "；".join(validation["错误"])
        raise ValueError(
            "卡片校验失败，共{}项错误：{}".format(
                len(validation["错误"]), details
            )
        )
    if validation["警告"]:
        details = "；".join(validation["警告"])
        raise ValueError(
            "卡片存在泛化问题，拒绝打包，共{}项警告：{}".format(
                len(validation["警告"]), details
            )
        )
    if genanki is None:
        raise RuntimeError(INSTALL_HINT)
    if not str(deck_name).strip():
        raise ValueError("牌组名称不得为空")

    templates = (
        Path(template_dir).expanduser()
        if template_dir is not None
        else DEFAULT_TEMPLATE_DIR
    )
    front = _load_template(templates / "front.html", "正面")
    back = _load_template(templates / "back.html", "背面")
    style = _load_template(templates / "style.css", "样式")

    destination = _safe_output_path(source, output)
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination = _safe_output_path(source, destination)
    if os.path.lexists(str(destination)) and not force:
        raise _existing_output_error(destination)

    model_id = stable_id("model", MODEL_KEY)
    deck_id = stable_id("deck", str(deck_name))
    model = genanki.Model(
        model_id,
        MODEL_NAME,
        fields=[{"name": field} for field in CARD_FIELDS],
        templates=[
            {
                "name": "正向回忆",
                "qfmt": front,
                "afmt": back,
            }
        ],
        css=style,
    )
    deck = genanki.Deck(deck_id, str(deck_name))
    expected_notes = {}
    for row in rows:
        fields = [to_anki_mathjax(row[field]) for field in CARD_FIELDS]
        tags = [row["Tags"], row["Importance"]]
        guid = stable_guid(row["Source"], row["Front"])
        note = genanki.Note(
            model=model,
            fields=fields,
            tags=tags,
            guid=guid,
        )
        deck.add_note(note)
        expected_notes[guid] = {"fields": fields, "tags": tags}

    temporary_path = None
    try:
        descriptor, temporary_name = tempfile.mkstemp(
            dir=str(destination.parent),
            prefix=".{}.tmp-".format(destination.name),
            suffix=".apkg",
        )
        os.close(descriptor)
        temporary_path = Path(temporary_name)
        genanki.Package(deck).write_to_file(str(temporary_path))
        _verify_package(
            temporary_path,
            model_id,
            deck_id,
            str(deck_name),
            expected_notes,
        )
        if force:
            os.replace(str(temporary_path), str(destination))
        else:
            try:
                os.link(str(temporary_path), str(destination))
            except FileExistsError:
                raise _existing_output_error(destination)
            temporary_path.unlink()
        temporary_path = None
        fsync_directory(destination.parent)
    finally:
        if temporary_path is not None:
            try:
                temporary_path.unlink()
            except FileNotFoundError:
                pass
    return len(rows)


def main() -> int:
    parser = ChineseArgumentParser(description="生成独立 Anki 牌组")
    parser.add_argument("卡片", help="通过校验的八字段 TSV")
    parser.add_argument(
        "--output", required=True, metavar="APKG路径", help="APKG 输出路径"
    )
    parser.add_argument(
        "--deck", required=True, metavar="牌组名", help="独立牌组名称"
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="明确允许原子替换已有 APKG",
    )
    arguments = parser.parse_args()

    try:
        count = build_package(
            getattr(arguments, "卡片"),
            arguments.output,
            arguments.deck,
            force=arguments.force,
        )
    except (OSError, RuntimeError, ValueError) as error:
        print("APKG 生成失败：{}".format(error), file=sys.stderr)
        return 1
    print("已生成 APKG：{}，共{}张卡片".format(arguments.output, count))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
