import csv
import hashlib
import json
import os
import sqlite3
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from scripts.build_apkg import MODEL_KEY, MODEL_NAME, stable_guid, stable_id
from scripts.build_reports import REPORT_NAMES
from scripts.common import CARD_FIELDS


COVERAGE_FIELDS = [
    "SectionID",
    "Title",
    "Status",
    "CardIDs",
    "Reason",
    "Source",
]
SECTION_FIELDS = ["SectionID", "Title", "Source"]
VISUAL_REVIEW_FIELDS = ["PDF_SHA256", "页码", "状态", "问题"]


def write_tsv(path, fields, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, delimiter="\t")
        writer.writeheader()
        writer.writerows(rows)


def card(chapter, front):
    return {
        "Front": front,
        "Back": "完整答案。",
        "Extra": "补充结论。",
        "Mistake": "不要混淆。",
        "Trigger": "看到关键词时。",
        "Importance": "必背",
        "Source": "操作系统|{}".format(chapter),
        "Tags": "408::操作系统::{}::必背".format(chapter),
    }


def prepare_report_project(project):
    row = card("第1章", "进程与程序的区别是什么?")
    write_tsv(project / "cards/第1章.tsv", CARD_FIELDS, [row])
    section = {"SectionID": "1.1", "Title": "基本概念", "Source": "教材第1页"}
    write_tsv(project / "build/小节清单.tsv", SECTION_FIELDS, [section])
    coverage = project / "reports/覆盖表.tsv"
    write_tsv(
        coverage,
        COVERAGE_FIELDS,
        [
            {
                "SectionID": section["SectionID"],
                "Title": section["Title"],
                "Status": "已制卡",
                "CardIDs": stable_guid(row["Source"], row["Front"]),
                "Reason": "",
                "Source": section["Source"],
            }
        ],
    )
    return coverage


def write_apkg(path, rows, collection_name="collection.anki2", mutation=None):
    mutation = mutation or {}
    path.parent.mkdir(parents=True, exist_ok=True)
    database = path.parent / ".{}.sqlite3".format(path.stem)
    connection = sqlite3.connect(str(database))
    deck_name = "操作系统::逐章"
    model_id = stable_id("model", MODEL_KEY)
    deck_id = stable_id("deck", deck_name)
    try:
        if not mutation.get("missing_col"):
            connection.execute("CREATE TABLE col (models TEXT, decks TEXT)")
        connection.execute(
            "CREATE TABLE notes (id INTEGER PRIMARY KEY, guid TEXT, mid INTEGER, flds TEXT, tags TEXT)"
        )
        if not mutation.get("missing_cards"):
            connection.execute(
                "CREATE TABLE cards (id INTEGER PRIMARY KEY, nid INTEGER, did INTEGER)"
            )
        connection.execute("CREATE TABLE revlog (id INTEGER PRIMARY KEY)")
        connection.execute("CREATE TABLE graves (usn INTEGER, oid INTEGER, type INTEGER)")

        model_fields = mutation.get("model_fields", CARD_FIELDS)
        models = {
            str(model_id): {
                "id": model_id,
                "name": MODEL_NAME,
                "flds": [{"name": field} for field in model_fields],
            }
        }
        decks = {str(deck_id): {"id": deck_id, "name": deck_name}}
        if not mutation.get("missing_col"):
            connection.execute(
                "INSERT INTO col (models, decks) VALUES (?, ?)",
                (
                    mutation.get("models_text", json.dumps(models, ensure_ascii=False)),
                    mutation.get("decks_text", json.dumps(decks, ensure_ascii=False)),
                ),
            )

        for index, row in enumerate(rows, start=1):
            guid = stable_guid(row["Source"], row["Front"])
            if mutation.get("stale_guid"):
                guid = "stale-{}".format(index)
            fields = [row[field] for field in CARD_FIELDS]
            if mutation.get("field_count") == 7:
                fields = fields[:-1]
            connection.execute(
                "INSERT INTO notes (id, guid, mid, flds, tags) VALUES (?, ?, ?, ?, ?)",
                (
                    index,
                    guid,
                    model_id,
                    "\x1f".join(fields),
                    " {} {} ".format(row["Tags"], row["Importance"]),
                ),
            )
            if not mutation.get("missing_cards"):
                connection.execute(
                    "INSERT INTO cards (id, nid, did) VALUES (?, ?, ?)",
                    (index, index, deck_id),
                )
        connection.commit()
    finally:
        connection.close()
    try:
        with zipfile.ZipFile(str(path), "w") as archive:
            archive.write(str(database), collection_name)
            archive.writestr("media", "{}")
    finally:
        database.unlink()


def run_apkg_audit(project):
    from scripts.audit_outputs import _audit_apkg

    checks = []
    with tempfile.TemporaryDirectory(dir=str(project)) as directory:
        counts = _audit_apkg(project, Path(directory), checks)
    return counts, checks


class ReportTransactionTests(unittest.TestCase):
    def _fail_third_promotion(self, project):
        real_replace = os.replace
        failed = {"value": False}

        def replace(source, destination):
            target = Path(destination)
            if (
                not failed["value"]
                and target.name == "背诵索引.md"
            ):
                failed["value"] = True
                raise OSError("模拟第三份报告提升失败")
            return real_replace(source, destination)

        return replace

    def test_promotion_failure_restores_all_four_old_reports(self):
        from scripts.build_reports import build_reports

        with tempfile.TemporaryDirectory() as directory:
            project = Path(directory)
            coverage = prepare_report_project(project)
            old = {}
            for name in REPORT_NAMES:
                content = "旧版本：{}".format(name).encode("utf-8")
                (project / "reports" / name).write_bytes(content)
                old[name] = content

            with mock.patch(
                "scripts.build_reports.os.replace",
                side_effect=self._fail_third_promotion(project),
            ):
                with self.assertRaisesRegex(OSError, "第三份报告提升失败"):
                    build_reports(project, coverage)

            for name, content in old.items():
                self.assertEqual((project / "reports" / name).read_bytes(), content)
            self.assertEqual(list((project / "reports").glob(".reports-transaction-*")), [])

    def test_promotion_failure_from_empty_state_leaves_all_four_absent(self):
        from scripts.build_reports import build_reports

        with tempfile.TemporaryDirectory() as directory:
            project = Path(directory)
            coverage = prepare_report_project(project)

            with mock.patch(
                "scripts.build_reports.os.replace",
                side_effect=self._fail_third_promotion(project),
            ):
                with self.assertRaisesRegex(OSError, "第三份报告提升失败"):
                    build_reports(project, coverage)

            self.assertFalse(any((project / "reports" / name).exists() for name in REPORT_NAMES))
            self.assertEqual(list((project / "reports").glob(".reports-transaction-*")), [])

    def test_report_destinations_reject_symlinks_and_unrelated_hardlinks(self):
        from scripts.build_reports import build_reports

        for link_kind in ["symbolic", "hard"]:
            with self.subTest(link_kind=link_kind), tempfile.TemporaryDirectory() as directory:
                project = Path(directory)
                coverage = prepare_report_project(project)
                destination = project / "reports/覆盖校验.md"
                unrelated = project / "reports/外部内容.md"
                unrelated.write_text("不得覆盖", encoding="utf-8")
                if link_kind == "symbolic":
                    destination.symlink_to(unrelated)
                else:
                    os.link(str(unrelated), str(destination))

                with self.assertRaisesRegex(ValueError, "符号链接|硬链接"):
                    build_reports(project, coverage)
                self.assertEqual(unrelated.read_text(encoding="utf-8"), "不得覆盖")

    def test_reports_directory_rejects_internal_symlink(self):
        from scripts.build_reports import build_reports

        with tempfile.TemporaryDirectory() as directory:
            project = Path(directory)
            project.mkdir(exist_ok=True)
            coverage = project / "覆盖表.tsv"
            row = card("第1章", "进程与程序的区别是什么?")
            write_tsv(project / "cards/第1章.tsv", CARD_FIELDS, [row])
            section = {"SectionID": "1.1", "Title": "基本概念", "Source": "教材第1页"}
            write_tsv(project / "build/小节清单.tsv", SECTION_FIELDS, [section])
            write_tsv(
                coverage,
                COVERAGE_FIELDS,
                [{
                    "SectionID": "1.1",
                    "Title": "基本概念",
                    "Status": "已制卡",
                    "CardIDs": stable_guid(row["Source"], row["Front"]),
                    "Reason": "",
                    "Source": "教材第1页",
                }],
            )
            redirected = project / "redirected"
            redirected.mkdir()
            (project / "reports").symlink_to(redirected, target_is_directory=True)

            with self.assertRaisesRegex(ValueError, "符号链接"):
                build_reports(project, coverage)
            self.assertEqual(list(redirected.iterdir()), [])


class PerChapterApkgAuditTests(unittest.TestCase):
    def test_same_counts_with_cross_chapter_packages_are_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            project = Path(directory)
            first = [card("第1章", "进程的定义是什么?")]
            second = [card("第2章", "线程的定义是什么?")]
            write_tsv(project / "cards/第1章.tsv", CARD_FIELDS, first)
            write_tsv(project / "cards/第2章.tsv", CARD_FIELDS, second)
            write_apkg(project / "cards/第1章.apkg", second)
            write_apkg(project / "cards/第2章.apkg", first)

            _, checks = run_apkg_audit(project)

            self.assertFalse(checks[-1]["通过"])
            self.assertIn("GUID", checks[-1]["说明"])

    def test_missing_same_stem_pair_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            project = Path(directory)
            rows = [card("第1章", "进程的定义是什么?")]
            write_tsv(project / "cards/第1章.tsv", CARD_FIELDS, rows)
            write_apkg(project / "cards/第2章.apkg", rows)

            _, checks = run_apkg_audit(project)

            self.assertFalse(checks[-1]["通过"])
            self.assertIn("同名", checks[-1]["说明"])

    def test_collection_anki2_and_anki21_are_both_supported(self):
        for collection_name in ["collection.anki2", "collection.anki21"]:
            with self.subTest(collection_name=collection_name), tempfile.TemporaryDirectory() as directory:
                project = Path(directory)
                rows = [card("第1章", "进程的定义是什么?")]
                write_tsv(project / "cards/第1章.tsv", CARD_FIELDS, rows)
                write_apkg(
                    project / "cards/第1章.apkg",
                    rows,
                    collection_name=collection_name,
                )

                counts, checks = run_apkg_audit(project)

                self.assertTrue(checks[-1]["通过"], checks[-1])
                self.assertEqual(counts, (1, 1))

    def test_schema_guid_fields_and_metadata_are_hard_gates(self):
        cases = [
            ({"missing_cards": True}, "cards"),
            ({"field_count": 7}, "8"),
            ({"stale_guid": True}, "GUID"),
            ({"model_fields": CARD_FIELDS[:-1] + ["错误字段"]}, "八字段模型"),
            ({"decks_text": "not-json"}, "deck"),
        ]
        for mutation, message in cases:
            with self.subTest(mutation=mutation), tempfile.TemporaryDirectory() as directory:
                project = Path(directory)
                rows = [card("第1章", "进程的定义是什么?")]
                write_tsv(project / "cards/第1章.tsv", CARD_FIELDS, rows)
                write_apkg(project / "cards/第1章.apkg", rows, mutation=mutation)

                _, checks = run_apkg_audit(project)

                self.assertFalse(checks[-1]["通过"])
                self.assertIn(message, checks[-1]["说明"])


class VisualReviewBindingTests(unittest.TestCase):
    def _audit(self, project, page_count, digest):
        from scripts.audit_outputs import _audit_visual_review

        checks = []
        _audit_visual_review(project, page_count, digest, checks)
        return checks[-1]

    def test_legacy_visual_review_without_hash_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            project = Path(directory)
            pdf = project / "print/讲义.pdf"
            pdf.parent.mkdir(parents=True)
            pdf.write_bytes(b"%PDF-current")
            digest = hashlib.sha256(pdf.read_bytes()).hexdigest()
            write_tsv(
                project / "reports/打印视觉复核.tsv",
                ["页码", "状态", "问题"],
                [{"页码": "1", "状态": "已通过", "问题": ""}],
            )

            check = self._audit(project, 1, digest)

            self.assertFalse(check["通过"])
            self.assertIn("PDF_SHA256", check["说明"])

    def test_same_page_count_old_pdf_hash_cannot_be_reused(self):
        with tempfile.TemporaryDirectory() as directory:
            project = Path(directory)
            pdf = project / "print/讲义.pdf"
            pdf.parent.mkdir(parents=True)
            pdf.write_bytes(b"%PDF-old-two-pages")
            old_digest = hashlib.sha256(pdf.read_bytes()).hexdigest()
            rows = [
                {"PDF_SHA256": old_digest, "页码": str(page), "状态": "已通过", "问题": ""}
                for page in [1, 2]
            ]
            write_tsv(
                project / "reports/打印视觉复核.tsv",
                VISUAL_REVIEW_FIELDS,
                rows,
            )
            self.assertTrue(self._audit(project, 2, old_digest)["通过"])

            pdf.write_bytes(b"%PDF-new-but-still-two-pages")
            new_digest = hashlib.sha256(pdf.read_bytes()).hexdigest()
            check = self._audit(project, 2, new_digest)

            self.assertFalse(check["通过"])
            self.assertIn("SHA-256", check["说明"])


if __name__ == "__main__":
    unittest.main()
