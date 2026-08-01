import csv
import os
import subprocess
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from scripts.build_apkg import stable_guid
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


def write_tsv(path, fields, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, delimiter="\t")
        writer.writeheader()
        writer.writerows(rows)


def valid_coverage_rows():
    return [
        {
            "SectionID": "2.1",
            "Title": "进程|定义\n与特征",
            "Status": "已制卡",
            "CardIDs": "C-001",
            "Reason": "",
            "Source": "教材|第20页",
        },
        {
            "SectionID": "2.2",
            "Title": "进程状态",
            "Status": "并入上级卡",
            "CardIDs": "C-001",
            "Reason": "",
            "Source": "教材第21页",
        },
        {
            "SectionID": "2.3",
            "Title": "章节导语",
            "Status": "不适合制卡",
            "CardIDs": "",
            "Reason": "仅为导语",
            "Source": "教材第22页",
        },
        {
            "SectionID": "2.4",
            "Title": "调度公式",
            "Status": "OCR存疑",
            "CardIDs": "",
            "Reason": "下标无法辨认|需核对",
            "Source": "教材第23页",
        },
    ]


def write_valid_coverage_assets(project, coverage, mapped_card):
    rows = valid_coverage_rows()
    identifier = stable_guid(mapped_card["Source"], mapped_card["Front"])
    for row in rows:
        if row["Status"] in ["已制卡", "并入上级卡"]:
            row["CardIDs"] = identifier
    write_tsv(coverage, COVERAGE_FIELDS, rows)
    write_tsv(
        project / "build/小节清单.tsv",
        SECTION_FIELDS,
        [
            {field: row[field] for field in SECTION_FIELDS}
            for row in rows
        ],
    )


class AnkiPackageTests(unittest.TestCase):
    def test_templates_use_required_fields_chinese_labels_and_mobile_layout(self):
        template_dir = ROOT / "assets/anki-card-template"
        front = (template_dir / "front.html").read_text(encoding="utf-8")
        back = (template_dir / "back.html").read_text(encoding="utf-8")
        style = (template_dir / "style.css").read_text(encoding="utf-8")

        for field in ["Importance", "Front", "Trigger"]:
            self.assertIn("{{{{{}}}}}".format(field), front)
        for label in ["重要程度", "问题", "触发条件"]:
            self.assertIn(label, front)
        for field in ["Back", "Extra", "Mistake", "Source"]:
            self.assertIn("{{{{{}}}}}".format(field), back)
        for label in ["答案", "补充", "易错", "来源"]:
            self.assertIn(label, back)
        self.assertIn("@media", style)
        self.assertIn("grid-template-columns: 1fr", style)

    def test_build_package_is_stable_and_creates_valid_apkg(self):
        from scripts.build_apkg import build_package, stable_guid, stable_id

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            tsv = root / "cards/操作系统_第2章_Anki.tsv"
            output = root / "cards/操作系统_第2章_Anki.apkg"
            rows = [
                valid_card(),
                valid_card("进程有哪些基本特征?", "高频"),
            ]
            write_tsv(tsv, CARD_FIELDS, rows)
            original = tsv.read_bytes()

            count = build_package(tsv, output, "操作系统::第2章")

            self.assertEqual(count, 2)
            self.assertEqual(tsv.read_bytes(), original)
            self.assertEqual(stable_id("deck", "操作系统::第2章"), stable_id("deck", "操作系统::第2章"))
            self.assertNotEqual(stable_id("deck", "操作系统::第2章"), stable_id("deck", "操作系统::第3章"))
            self.assertEqual(
                stable_guid(" 操作系统|第2章 进程管理 ", "  Ａ＋Ｂ\n是什么？ "),
                stable_guid("操作系统|第2章 进程管理", "A+B 是什么?"),
            )
            with zipfile.ZipFile(output) as archive:
                self.assertIsNone(archive.testzip())
                names = set(archive.namelist())
            self.assertTrue({"collection.anki2", "collection.anki21"} & names)

    def test_build_package_rejects_invalid_cards_before_writing(self):
        from scripts.build_apkg import build_package

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            tsv = root / "cards/坏卡.tsv"
            output = root / "cards/坏卡.apkg"
            row = valid_card()
            row["Back"] = ""
            write_tsv(tsv, CARD_FIELDS, [row])

            with self.assertRaisesRegex(ValueError, "卡片校验失败"):
                build_package(tsv, output, "操作系统::坏卡")
            self.assertFalse(output.exists())

    def test_build_package_missing_dependency_and_write_failure_leave_no_partial_file(self):
        import scripts.build_apkg as build_apkg

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            tsv = root / "cards/卡片.tsv"
            output = root / "cards/卡片.apkg"
            write_tsv(tsv, CARD_FIELDS, [valid_card()])

            with mock.patch.object(build_apkg, "genanki", None):
                with self.assertRaisesRegex(RuntimeError, "pip install -r requirements.txt"):
                    build_apkg.build_package(tsv, output, "操作系统::第2章")
            self.assertFalse(output.exists())

            old_package = "旧牌组".encode("utf-8")
            output.write_bytes(old_package)
            with mock.patch(
                "scripts.build_apkg.genanki.Package.write_to_file",
                side_effect=OSError("模拟写入失败"),
            ):
                with self.assertRaisesRegex(OSError, "模拟写入失败"):
                    build_apkg.build_package(
                        tsv, output, "操作系统::第2章", force=True
                    )
            self.assertEqual(output.read_bytes(), old_package)
            self.assertEqual(list(output.parent.glob(".*.tmp-*")), [])

    def test_build_package_rejects_output_symlink_escape(self):
        from scripts.build_apkg import build_package

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            cards = root / "cards"
            outside = root / "outside"
            outside.mkdir()
            write_tsv(cards / "卡片.tsv", CARD_FIELDS, [valid_card()])
            (cards / "导出").symlink_to(outside, target_is_directory=True)

            with self.assertRaisesRegex(ValueError, "超出"):
                build_package(
                    cards / "卡片.tsv",
                    cards / "导出/卡片.apkg",
                    "操作系统::第2章",
                )
            self.assertEqual(list(outside.iterdir()), [])

    def test_build_package_cli_matches_skill_command(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            tsv = root / "cards/卡片.tsv"
            output = root / "cards/卡片.apkg"
            write_tsv(tsv, CARD_FIELDS, [valid_card()])
            completed = subprocess.run(
                [
                    sys.executable,
                    str(ROOT / "scripts/build_apkg.py"),
                    str(tsv),
                    "--output",
                    str(output),
                    "--deck",
                    "操作系统::第2章",
                ],
                check=False,
                capture_output=True,
                text=True,
            )
            self.assertEqual(completed.returncode, 0, completed.stderr)
            self.assertIn("已生成", completed.stdout)
            self.assertNotIn("Traceback", completed.stderr)


class ReportTests(unittest.TestCase):
    def test_coverage_validation_enforces_exact_fields_and_four_statuses(self):
        from scripts.build_reports import validate_coverage_rows

        for status, missing_field in [
            ("已制卡", "CardIDs"),
            ("并入上级卡", "CardIDs"),
            ("不适合制卡", "Reason"),
            ("OCR存疑", "Reason"),
        ]:
            with self.subTest(status=status):
                row = valid_coverage_rows()[0]
                row["Status"] = status
                row["CardIDs"] = "C-001"
                row["Reason"] = "有明确原因"
                row[missing_field] = ""
                result = validate_coverage_rows([row])
                self.assertFalse(result["通过"])
                self.assertIn(missing_field, "\n".join(result["错误"]))

        unknown = valid_coverage_rows()[0]
        unknown["Status"] = "待处理"
        result = validate_coverage_rows([unknown])
        self.assertFalse(result["通过"])
        self.assertIn("Status 非法", "\n".join(result["错误"]))

        wrong_fields = dict(valid_coverage_rows()[0])
        wrong_fields["Unexpected"] = "x"
        result = validate_coverage_rows([wrong_fields])
        self.assertFalse(result["通过"])
        self.assertIn("字段顺序错误", "\n".join(result["错误"]))

    def test_build_reports_creates_four_escaped_sorted_chapter_reports(self):
        from scripts.build_reports import build_reports

        with tempfile.TemporaryDirectory() as directory:
            project = Path(directory)
            chapter_two = project / "cards/第2章_进程管理_Anki.tsv"
            chapter_three = project / "cards/第3章_内存管理_Anki.tsv"
            chapter_two_rows = [
                valid_card("低频问题|A", "低频补充"),
                valid_card("必背问题", "必背"),
                valid_card("易错问题", "易错"),
            ]
            write_tsv(
                chapter_two,
                CARD_FIELDS,
                chapter_two_rows,
            )
            write_tsv(
                chapter_three,
                CARD_FIELDS,
                [
                    valid_card("理解问题", "理解"),
                    valid_card("高频问题", "高频"),
                ],
            )
            coverage = project / "reports/覆盖表.tsv"
            write_valid_coverage_assets(
                project, coverage, chapter_two_rows[0]
            )

            result = build_reports(project, coverage)

            self.assertEqual(result["卡片数"], 5)
            self.assertEqual(result["小节数"], 4)
            expected = {
                "覆盖校验.md",
                "卡片统计.md",
                "背诵索引.md",
                "OCR存疑.md",
            }
            self.assertEqual({path.name for path in result["报告"]}, expected)

            coverage_report = (project / "reports/覆盖校验.md").read_text(encoding="utf-8")
            self.assertIn(r"进程\|定义<br>与特征", coverage_report)
            self.assertIn(r"教材\|第20页", coverage_report)

            statistics = (project / "reports/卡片统计.md").read_text(encoding="utf-8")
            self.assertIn("第2章_进程管理_Anki", statistics)
            self.assertIn("第3章_内存管理_Anki", statistics)
            self.assertIn("| 合计 | 5 | 1 | 1 | 1 | 1 | 1 |", statistics)

            index = (project / "reports/背诵索引.md").read_text(encoding="utf-8")
            positions = [index.index("## {}".format(value)) for value in ["必背", "高频", "易错", "理解", "低频补充"]]
            self.assertEqual(positions, sorted(positions))
            self.assertIn(r"低频问题\|A", index)

            ocr = (project / "reports/OCR存疑.md").read_text(encoding="utf-8")
            self.assertIn("调度公式", ocr)
            self.assertIn(r"下标无法辨认\|需核对", ocr)

    def test_invalid_coverage_writes_no_reports(self):
        from scripts.build_reports import build_reports

        with tempfile.TemporaryDirectory() as directory:
            project = Path(directory)
            coverage = project / "reports/覆盖表.tsv"
            rows = valid_coverage_rows()
            rows[0]["CardIDs"] = ""
            write_tsv(coverage, COVERAGE_FIELDS, rows)

            with self.assertRaisesRegex(ValueError, "覆盖表校验失败"):
                build_reports(project, coverage)
            for name in ["覆盖校验.md", "卡片统计.md", "背诵索引.md", "OCR存疑.md"]:
                self.assertFalse((project / "reports" / name).exists())

    def test_report_output_rejects_symlink_escape(self):
        from scripts.build_reports import build_reports

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            project = root / "project"
            project.mkdir()
            outside = root / "outside"
            outside.mkdir()
            (project / "reports").symlink_to(outside, target_is_directory=True)
            coverage = project / "覆盖表.tsv"
            write_tsv(coverage, COVERAGE_FIELDS, valid_coverage_rows())

            with self.assertRaisesRegex(ValueError, "超出"):
                build_reports(project, coverage)
            self.assertEqual(list(outside.iterdir()), [])

    def test_reports_never_overwrite_the_coverage_input(self):
        from scripts.build_reports import build_reports

        with tempfile.TemporaryDirectory() as directory:
            project = Path(directory)
            coverage = project / "reports/覆盖校验.md"
            write_tsv(coverage, COVERAGE_FIELDS, valid_coverage_rows())
            original = coverage.read_bytes()

            with self.assertRaisesRegex(ValueError, "不得覆盖覆盖表"):
                build_reports(project, coverage)
            self.assertEqual(coverage.read_bytes(), original)
            for name in ["卡片统计.md", "背诵索引.md", "OCR存疑.md"]:
                self.assertFalse((project / "reports" / name).exists())

    def test_build_reports_cli_matches_skill_command(self):
        with tempfile.TemporaryDirectory() as directory:
            project = Path(directory)
            coverage = project / "reports/覆盖表.tsv"
            mapped_card = valid_card()
            write_tsv(project / "cards/第2章.tsv", CARD_FIELDS, [mapped_card])
            write_valid_coverage_assets(project, coverage, mapped_card)
            completed = subprocess.run(
                [
                    sys.executable,
                    str(ROOT / "scripts/build_reports.py"),
                    "--project",
                    str(project),
                    "--coverage",
                    str(coverage),
                ],
                check=False,
                capture_output=True,
                text=True,
            )
            self.assertEqual(completed.returncode, 0, completed.stderr)
            self.assertIn("已生成4份报告", completed.stdout)
            self.assertNotIn("Traceback", completed.stderr)


class CommandHelpTests(unittest.TestCase):
    def test_new_command_help_is_entirely_chinese(self):
        for script in ["build_apkg.py", "build_reports.py"]:
            with self.subTest(script=script):
                completed = subprocess.run(
                    [sys.executable, str(ROOT / "scripts" / script), "--help"],
                    check=False,
                    capture_output=True,
                    text=True,
                )
                self.assertEqual(completed.returncode, 0, completed.stderr)
                self.assertIn("用法：", completed.stdout)
                self.assertIn("选项：", completed.stdout)
                self.assertIn("显示帮助信息并退出", completed.stdout)
                self.assertNotIn("usage:", completed.stdout)
                self.assertNotIn("positional arguments:", completed.stdout)
                self.assertNotIn("optional arguments:", completed.stdout)
                self.assertNotIn("show this help message", completed.stdout)


if __name__ == "__main__":
    unittest.main()
