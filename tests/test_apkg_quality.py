import csv
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from scripts.build_apkg import (
    MODEL_KEY,
    MODEL_NAME,
    build_package,
    stable_guid,
    stable_id,
)
from scripts.common import CARD_FIELDS, normalize_front
from scripts.validate_cards import validate_rows


DECK_NAME = "操作系统::第2章"


def valid_card(front="进程与程序的主要区别是什么?", importance="必背"):
    return {
        "Front": front,
        "Back": "进程是动态执行过程，程序是静态代码。",
        "Extra": "区分动态与静态。",
        "Mistake": "不要把程序视为运行实体。",
        "Trigger": "看到动态性、并发性时触发。",
        "Importance": importance,
        "Source": "操作系统|第2章 进程管理",
        "Tags": "408::操作系统::第2章::{}".format(importance),
    }


def write_tsv(path, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=CARD_FIELDS, delimiter="\t")
        writer.writeheader()
        writer.writerows(rows)


def extract_collection(apkg, directory):
    with zipfile.ZipFile(str(apkg)) as archive:
        names = set(archive.namelist())
        member = next(
            name
            for name in ["collection.anki2", "collection.anki21"]
            if name in names
        )
        destination = Path(directory) / member
        destination.write_bytes(archive.read(member))
    return destination


class ApkgQualityTests(unittest.TestCase):
    def test_generic_warning_blocks_package_and_includes_section_probe(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            tsv = root / "cards/泛化卡.tsv"
            output = root / "cards/泛化卡.apkg"
            row = valid_card("本节第1个知识点是什么")
            write_tsv(tsv, [row])

            validation = validate_rows([row])
            self.assertTrue(validation["通过"])
            self.assertIn("问题过于泛化", "\n".join(validation["警告"]))

            with self.assertRaisesRegex(ValueError, "泛化.*拒绝打包"):
                build_package(tsv, output, DECK_NAME)
            self.assertFalse(output.exists())

    def test_empty_rows_fail_validation_and_package(self):
        validation = validate_rows([])
        self.assertFalse(validation["通过"])
        self.assertIn("至少包含1张卡片", "\n".join(validation["错误"]))

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            tsv = root / "cards/空卡.tsv"
            output = root / "cards/空卡.apkg"
            write_tsv(tsv, [])

            with self.assertRaisesRegex(ValueError, "至少包含1张卡片"):
                build_package(tsv, output, DECK_NAME)
            self.assertFalse(output.exists())

    def test_collection_has_exact_model_deck_notes_fields_math_and_tags(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            tsv = root / "cards/高质量卡.tsv"
            output = root / "cards/高质量卡.apkg"
            first = valid_card("调度公式如何表示?")
            first["Back"] = (
                r"行内 $T=t_c-t_a$；已有 \(x+y\)；价格 $5；转义 \$z\$。"
            )
            first["Extra"] = (
                r"展示：$$W=\frac{T}{t_s}$$；已有 \[E=mc^2\]。"
            )
            second = valid_card("进程有哪些基本特征?", "高频")
            rows = [first, second]
            write_tsv(tsv, rows)

            self.assertEqual(build_package(tsv, output, DECK_NAME), 2)
            database = extract_collection(output, root)
            connection = sqlite3.connect(str(database))
            try:
                models_text, decks_text = connection.execute(
                    "SELECT models, decks FROM col"
                ).fetchone()
                note_records = connection.execute(
                    "SELECT id, guid, mid, flds, tags FROM notes ORDER BY id"
                ).fetchall()
                card_records = connection.execute(
                    "SELECT nid, did FROM cards ORDER BY nid"
                ).fetchall()
            finally:
                connection.close()

            model_id = stable_id("model", MODEL_KEY)
            deck_id = stable_id("deck", DECK_NAME)
            model = json.loads(models_text)[str(model_id)]
            deck = json.loads(decks_text)[str(deck_id)]
            self.assertEqual(model["name"], MODEL_NAME)
            self.assertEqual(
                [field["name"] for field in model["flds"]], CARD_FIELDS
            )
            self.assertEqual(deck["id"], deck_id)
            self.assertEqual(deck["name"], DECK_NAME)
            self.assertEqual(len(note_records), 2)

            notes_by_guid = {record[1]: record for record in note_records}
            for row in rows:
                guid = stable_guid(row["Source"], row["Front"])
                self.assertIn(guid, notes_by_guid)
                note_id, _, note_model_id, fields_text, tags_text = notes_by_guid[guid]
                fields = fields_text.split("\x1f")
                self.assertEqual(note_model_id, model_id)
                self.assertEqual(len(fields), 8)
                self.assertEqual(
                    set(tags_text.split()), {row["Tags"], row["Importance"]}
                )
                self.assertNotIn(row["Tags"].replace("::", "_"), tags_text)
                self.assertIn((note_id, deck_id), card_records)

            first_fields = notes_by_guid[
                stable_guid(first["Source"], first["Front"])
            ][3].split("\x1f")
            self.assertEqual(
                first_fields[1],
                r"行内 \(T=t_c-t_a\)；已有 \(x+y\)；价格 $5；转义 \$z\$。",
            )
            self.assertEqual(
                first_fields[2],
                r"展示：\[W=\frac{T}{t_s}\]；已有 \[E=mc^2\]。",
            )

    def test_normalized_guid_handles_case_punctuation_and_cjk_ocr_spaces(self):
        source = "操作系统|第2章"
        noisy = r"  进 程调度、PROCESS   State 是什么。公式 \(甲 乙\)  "
        canonical = r"进程调度,process state 是什么.公式 \(甲 乙\)"

        self.assertEqual(normalize_front(noisy), canonical)
        self.assertEqual(
            stable_guid(source, noisy), stable_guid(source, canonical)
        )
        self.assertNotEqual(
            stable_guid(source, canonical),
            stable_guid(source, canonical.replace("process state", "processstate")),
        )
        self.assertNotEqual(
            stable_guid(source, canonical),
            stable_guid(source, canonical.replace(r"\(甲 乙\)", r"\(甲乙\)")),
        )

    def test_existing_output_requires_force_and_cli_force_replaces_it(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            tsv = root / "cards/卡片.tsv"
            output = root / "cards/卡片.apkg"
            write_tsv(tsv, [valid_card()])
            old_package = "旧牌组".encode("utf-8")
            output.write_bytes(old_package)

            with self.assertRaisesRegex(FileExistsError, "已存在"):
                build_package(tsv, output, DECK_NAME)
            self.assertEqual(output.read_bytes(), old_package)
            self.assertEqual(list(output.parent.glob(".*.tmp-*")), [])

            completed = subprocess.run(
                [
                    sys.executable,
                    str(ROOT / "scripts/build_apkg.py"),
                    str(tsv),
                    "--output",
                    str(output),
                    "--deck",
                    DECK_NAME,
                    "--force",
                ],
                check=False,
                capture_output=True,
                text=True,
            )
            self.assertEqual(completed.returncode, 0, completed.stderr)
            self.assertNotEqual(output.read_bytes(), old_package)
            with zipfile.ZipFile(str(output)) as archive:
                self.assertIsNone(archive.testzip())

    def test_default_commit_does_not_overwrite_file_created_during_build(self):
        import scripts.build_apkg as build_apkg_module

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            tsv = root / "cards/卡片.tsv"
            output = root / "cards/卡片.apkg"
            write_tsv(tsv, [valid_card()])
            concurrent_content = "并发创建的文件".encode("utf-8")
            original_write = build_apkg_module.genanki.Package.write_to_file

            def racing_write(package, path):
                original_write(package, path)
                output.write_bytes(concurrent_content)

            with mock.patch(
                "scripts.build_apkg.genanki.Package.write_to_file",
                new=racing_write,
            ):
                with self.assertRaisesRegex(FileExistsError, "已存在"):
                    build_apkg_module.build_package(tsv, output, DECK_NAME)

            self.assertEqual(output.read_bytes(), concurrent_content)
            self.assertEqual(list(output.parent.glob(".*.tmp-*")), [])

    def test_force_replace_failure_keeps_old_output_and_removes_temporary_file(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            tsv = root / "cards/卡片.tsv"
            output = root / "cards/卡片.apkg"
            write_tsv(tsv, [valid_card()])
            old_package = "旧牌组".encode("utf-8")
            output.write_bytes(old_package)

            with mock.patch(
                "scripts.build_apkg.os.replace",
                side_effect=OSError("模拟覆盖失败"),
            ):
                with self.assertRaisesRegex(OSError, "模拟覆盖失败"):
                    build_package(tsv, output, DECK_NAME, force=True)

            self.assertEqual(output.read_bytes(), old_package)
            self.assertEqual(list(output.parent.glob(".*.tmp-*")), [])

    def test_output_links_to_tsv_are_rejected_even_with_force(self):
        for link_kind in ["hard", "symbolic"]:
            with self.subTest(link_kind=link_kind):
                with tempfile.TemporaryDirectory() as directory:
                    root = Path(directory)
                    tsv = root / "cards/卡片.tsv"
                    output = root / "cards/卡片.apkg"
                    write_tsv(tsv, [valid_card()])
                    if link_kind == "hard":
                        os.link(str(tsv), str(output))
                    else:
                        output.symlink_to(tsv)

                    with self.assertRaisesRegex(
                        ValueError, "源 TSV|同一文件|符号链接"
                    ):
                        build_package(tsv, output, DECK_NAME, force=True)

    def test_build_rejects_zip_with_unreadable_collection_before_commit(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            tsv = root / "cards/卡片.tsv"
            output = root / "cards/卡片.apkg"
            write_tsv(tsv, [valid_card()])

            def write_invalid_collection(package, path):
                with zipfile.ZipFile(path, "w") as archive:
                    archive.writestr("collection.anki2", b"not a sqlite database")
                    archive.writestr("media", "{}")

            with mock.patch(
                "scripts.build_apkg.genanki.Package.write_to_file",
                new=write_invalid_collection,
            ):
                with self.assertRaisesRegex(ValueError, "collection"):
                    build_package(tsv, output, DECK_NAME)

            self.assertFalse(output.exists())
            self.assertEqual(list(output.parent.glob(".*.tmp-*")), [])


if __name__ == "__main__":
    unittest.main()
