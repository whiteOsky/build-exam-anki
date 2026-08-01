import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from scripts.clean_ocr import clean_ocr, clean_ocr_file
from scripts.common import (
    CARD_FIELDS,
    COVERAGE_STATUSES,
    IMPORTANCE_VALUES,
    normalize_front,
    read_json,
    read_tsv_strict,
    write_json,
)
from scripts.init_project import initialize_project
from scripts.inspect_sources import inspect_sources, write_source_report


class CommonTests(unittest.TestCase):
    def test_constants_match_contract(self):
        self.assertEqual(
            CARD_FIELDS,
            [
                "Front",
                "Back",
                "Extra",
                "Mistake",
                "Trigger",
                "Importance",
                "Source",
                "Tags",
            ],
        )
        self.assertEqual(
            IMPORTANCE_VALUES,
            ["必背", "高频", "易错", "理解", "低频补充"],
        )
        self.assertEqual(
            COVERAGE_STATUSES,
            ["已制卡", "并入上级卡", "不适合制卡", "OCR存疑"],
        )

    def test_front_normalization_uses_nfkc_and_whitespace(self):
        self.assertEqual(
            normalize_front("  Ａ＋Ｂ\n 是什么？  "),
            "a+b 是什么?",
        )

    def test_strict_tsv_reader_rejects_bad_header_and_column_count(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            bad_header = root / "bad-header.tsv"
            bad_header.write_text("Back\tFront\n答\t问\n", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "表头"):
                read_tsv_strict(bad_header)

            bad_row = root / "bad-row.tsv"
            bad_row.write_text(
                "\t".join(CARD_FIELDS) + "\n只有一列\n",
                encoding="utf-8",
            )
            with self.assertRaisesRegex(ValueError, "列数"):
                read_tsv_strict(bad_row)

    def test_json_round_trip(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "data.json"
            payload = {"课程": "操作系统", "章节": ["进程", "内存"]}
            write_json(path, payload)
            self.assertEqual(read_json(path), payload)
            self.assertFalse(path.read_text(encoding="utf-8").endswith("\n\n"))

    def test_json_error_is_chinese(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "bad.json"
            path.write_text("{", encoding="utf-8")
            with self.assertRaisesRegex(
                ValueError,
                "JSON 格式错误：第1行第2列",
            ):
                read_json(path)

    def test_json_writer_rejects_symlink_escape(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            root = base / "项目"
            outside = base / "外部"
            root.mkdir()
            outside.mkdir()
            (root / "build").symlink_to(outside, target_is_directory=True)

            with self.assertRaisesRegex(ValueError, "符号链接"):
                write_json(root / "build/project.json", {}, root=root)
            self.assertFalse((outside / "project.json").exists())


class ProjectAndSourceTests(unittest.TestCase):
    def test_initialize_project_creates_layout_and_chinese_metadata(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            original = root / "原始讲义.pdf"
            original.write_bytes(b"pdf")

            metadata = initialize_project(
                root,
                "408",
                "操作系统：重概念与调度计算",
                ["进程管理", "内存管理"],
                ["无"],
            )

            expected = [
                "source/pdf",
                "source/ocr",
                "source/existing_tsv",
                "source/existing_apkg",
                "cleaned",
                "notes",
                "cards",
                "reports",
                "print",
                "assets/framework",
                "build",
            ]
            for relative in expected:
                self.assertTrue((root / relative).is_dir(), relative)
            self.assertEqual(metadata["课程"], "408")
            self.assertEqual(
                metadata["科目画像"],
                "操作系统：重概念与调度计算",
            )
            self.assertEqual(metadata["纳入章节"], ["进程管理", "内存管理"])
            self.assertEqual(metadata["排除章节"], ["无"])
            self.assertEqual(read_json(root / "build/project.json"), metadata)
            self.assertEqual(original.read_bytes(), b"pdf")

    def test_initialize_project_rejects_symlinked_fixed_directory(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            root = base / "项目"
            outside = base / "外部"
            root.mkdir()
            outside.mkdir()
            (root / "build").symlink_to(outside, target_is_directory=True)

            with self.assertRaisesRegex(ValueError, "符号链接"):
                initialize_project(root, "408", "操作系统", [], [])
            self.assertFalse((outside / "project.json").exists())

    def test_initialize_project_cli_matches_public_command(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "项目"
            completed = subprocess.run(
                [
                    sys.executable,
                    str(ROOT / "scripts/init_project.py"),
                    str(root),
                    "--course",
                    "408",
                    "--profile",
                    "概念型工科",
                    "--include",
                    "1,2,3",
                    "--exclude",
                    "4",
                ],
                check=False,
                capture_output=True,
                text=True,
            )
            self.assertEqual(completed.returncode, 0, completed.stderr)
            metadata = read_json(root / "build/project.json")
            self.assertEqual(metadata["纳入章节"], ["1", "2", "3"])
            self.assertEqual(metadata["排除章节"], ["4"])

    def test_inspection_filters_generated_and_hidden_files(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "资料").mkdir()
            (root / "资料/第03章_进程.md").write_text("正文", encoding="utf-8")
            (root / "讲义.pdf").write_bytes(b"1234")
            (root / "卡片.tsv").write_text("x", encoding="utf-8")
            (root / "课件.pptx").write_bytes(b"pptx")
            (root / "文档.docx").write_bytes(b"docx")
            (root / "旧卡.apkg").write_bytes(b"apkg")
            (root / ".隐藏.docx").write_bytes(b"hidden")
            (root / ".缓存").mkdir()
            (root / ".缓存/内容.pptx").write_bytes(b"hidden")
            (root / "reports").mkdir()
            (root / "reports/生成.md").write_text("generated", encoding="utf-8")
            (root / "notes").mkdir()
            (root / "notes/笔记.md").write_text("generated", encoding="utf-8")
            (root / "source/assets").mkdir(parents=True)
            (root / "source/assets/图表.md").write_text("正文", encoding="utf-8")
            (root / "source/reports").mkdir()
            (root / "source/reports/补充.md").write_text("正文", encoding="utf-8")

            inventory = inspect_sources(root)

            self.assertEqual(inventory["根目录"], str(root.resolve()))
            paths = [item["路径"] for item in inventory["文件"]]
            expected_paths = [
                "资料/第03章_进程.md",
                "讲义.pdf",
                "卡片.tsv",
                "课件.pptx",
                "文档.docx",
                "旧卡.apkg",
                "source/assets/图表.md",
                "source/reports/补充.md",
            ]
            self.assertEqual(paths, sorted(expected_paths, key=str.casefold))
            chapter = next(
                item
                for item in inventory["文件"]
                if item["路径"].endswith("进程.md")
            )
            self.assertEqual(chapter["推测章节"], "第03章")
            self.assertEqual(chapter["类型"], "Markdown 文档")
            self.assertEqual(chapter["字节数"], len("正文".encode("utf-8")))
            self.assertEqual(
                {item["类型"] for item in inventory["文件"]},
                {
                    "PDF 文档",
                    "Markdown 文档",
                    "PowerPoint 文档",
                    "Word 文档",
                    "TSV 卡片",
                    "Anki 卡包",
                },
            )

            report_path = write_source_report(root, inventory)
            report = report_path.read_text(encoding="utf-8")
            self.assertIn("# 来源清单", report)
            self.assertIn("资料/第03章_进程.md", report)

    def test_source_report_escapes_table_cells(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            inventory = {
                "根目录": str(root),
                "文件": [
                    {
                        "路径": "资料/第1章|补充\n讲义.md",
                        "类型": "Markdown 文档",
                        "字节数": 1,
                        "推测章节": "第1章",
                    }
                ],
            }
            report = write_source_report(root, inventory).read_text(encoding="utf-8")
            self.assertIn(r"第1章\|补充<br>讲义.md", report)

    def test_source_report_rejects_symlink_escape(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            root = base / "项目"
            outside = base / "外部"
            root.mkdir()
            outside.mkdir()
            (root / "reports").symlink_to(outside, target_is_directory=True)
            inventory = {"根目录": str(root), "文件": []}

            with self.assertRaisesRegex(ValueError, "符号链接"):
                write_source_report(root, inventory)
            self.assertFalse((outside / "来源清单.md").exists())

    def test_inspection_cli_outputs_json_and_report(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "第1章.pdf").write_bytes(b"pdf")
            json_path = root / "build/来源清单.json"
            report_path = root / "reports/来源清单.md"
            completed = subprocess.run(
                [
                    sys.executable,
                    str(ROOT / "scripts/inspect_sources.py"),
                    str(root),
                    "--json",
                    str(json_path),
                    "--report",
                    str(report_path),
                ],
                check=False,
                capture_output=True,
                text=True,
            )
            self.assertEqual(completed.returncode, 0, completed.stderr)
            inventory = read_json(json_path)
            self.assertEqual(inventory["文件"][0]["推测章节"], "第1章")
            self.assertTrue(report_path.is_file())


class OcrCleaningTests(unittest.TestCase):
    def test_url_cleaning_preserves_adjacent_punctuation_and_text(self):
        source = (
            "查询 https://example.com/search?q=进程&lang=zh，继续正文。\n"
            "括号（https://example.com/a_(b)?x=1）后文保留。\n"
            "句末 https://example.com/path. 下一句不丢失！\n"
        )
        cleaned, record = clean_ocr(source)
        self.assertEqual(
            cleaned,
            (
                "查询 进程&lang=zh，继续正文。\n"
                "括号（）后文保留。\n"
                "句末 . 下一句不丢失！\n"
            ),
        )
        self.assertEqual(record["删除链接数"], 3)

    def test_url_cleaning_stops_at_first_non_ascii_character(self):
        source = (
            "https://example.com：后文必须保留。\n"
            "https://example.com“后文必须保留”。\n"
            "https://example.com后文必须保留。\n"
        )

        cleaned, record = clean_ocr(source)

        self.assertEqual(
            cleaned,
            "：后文必须保留。\n"
            "“后文必须保留”。\n"
            "后文必须保留。\n",
        )
        self.assertEqual(record["删除链接数"], 3)

    def test_conservative_cleaning_removes_noise_and_keeps_study_content(self):
        source = """命题追踪：进程状态转换
<div>注意：阻塞态不能直接变为运行态。</div>
<img src="bad.png">
![失效图](https://example.com/a.png)
访问 https://example.com 或 www.example.org 获取更多资料
扫码关注公众号领取资料



例题：计算周转时间 $T=t_c-t_a$。
解析：代入公式即可。
<table><tr><td>公式：$W=T-t_s$</td></tr></table>
"""
        cleaned, record = clean_ocr(source)

        self.assertIn("命题追踪", cleaned)
        self.assertIn("注意：阻塞态", cleaned)
        self.assertIn("例题：", cleaned)
        self.assertIn("解析：", cleaned)
        self.assertIn("$T=t_c-t_a$", cleaned)
        self.assertIn("公式：$W=T-t_s$", cleaned)
        self.assertNotIn("<img", cleaned)
        self.assertNotIn("![", cleaned)
        self.assertNotIn("https://", cleaned)
        self.assertNotIn("扫码关注", cleaned)
        self.assertNotIn("\n\n\n", cleaned)
        self.assertGreater(record["删除HTML标签数"], 0)
        self.assertEqual(record["删除广告水印行数"], 1)
        self.assertGreaterEqual(record["删除链接数"], 3)

    def test_qr_marketing_is_removed_but_technical_discussion_is_kept(self):
        source = """扫描书中二维码即可快速定位视频讲解
扫码获取资料
二维码获取配套题库
二维码技术通过黑白矩阵编码数据，并使用纠错码提高可靠性。
扫描二维码属于图像识别流程，解码器再恢复其中的数据。
"""

        cleaned, record = clean_ocr(source)

        self.assertNotIn("视频讲解", cleaned)
        self.assertNotIn("扫码获取资料", cleaned)
        self.assertNotIn("配套题库", cleaned)
        self.assertIn("二维码技术通过黑白矩阵编码数据", cleaned)
        self.assertIn("扫描二维码属于图像识别流程", cleaned)
        self.assertEqual(record["删除广告水印行数"], 3)

    def test_html_parser_removes_target_tags_and_preserves_cell_text(self):
        source = (
            "<DIV>注意<table><tr><td>A</td><td>B</td></tr></table></DIV>"
            "<p>保留段落</p>"
        )
        cleaned, record = clean_ocr(source)
        lowered = cleaned.lower()
        for tag in ["<table", "</table", "<tr", "</tr", "<td", "</td", "<div", "</div"]:
            self.assertNotIn(tag, lowered)
        self.assertIn("A", cleaned)
        self.assertIn("B", cleaned)
        self.assertIn("<p>保留段落</p>", cleaned)
        self.assertEqual(record["删除HTML标签数"], 10)

    def test_file_cleaning_keeps_source_unchanged_and_rejects_path_conflicts(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "原始OCR.md"
            output = root / "清洗后.md"
            record_path = root / "清洗记录.md"
            original = "注意：保留正文。\n扫码关注公众号\n"
            source.write_text(original, encoding="utf-8")

            record = clean_ocr_file(source, output, record_path)

            self.assertEqual(source.read_text(encoding="utf-8"), original)
            self.assertEqual(
                output.read_text(encoding="utf-8"),
                "注意：保留正文。\n",
            )
            report = record_path.read_text(encoding="utf-8")
            self.assertIn("# OCR 清洗记录", report)
            self.assertIn(str(record["删除广告水印行数"]), report)
            with self.assertRaisesRegex(ValueError, "必须使用不同路径"):
                clean_ocr_file(source, source, record_path)

    def test_cleaning_rejects_hard_links_and_existing_outputs_by_default(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "来源.md"
            output = root / "输出.md"
            report = root / "报告.md"
            source.write_text("原始正文\n", encoding="utf-8")
            os.link(source, output)

            with self.assertRaisesRegex(ValueError, "同一文件"):
                clean_ocr_file(source, output, report, force=True)
            self.assertEqual(source.read_text(encoding="utf-8"), "原始正文\n")

            output.unlink()
            output.write_text("旧输出\n", encoding="utf-8")
            report.write_text("旧报告\n", encoding="utf-8")
            with self.assertRaisesRegex(FileExistsError, "已存在"):
                clean_ocr_file(source, output, report)
            self.assertEqual(output.read_text(encoding="utf-8"), "旧输出\n")
            self.assertEqual(report.read_text(encoding="utf-8"), "旧报告\n")

    def test_force_write_rolls_back_both_files_when_commit_fails(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "来源.md"
            output = root / "输出.md"
            report = root / "报告.md"
            source.write_text("新正文\n", encoding="utf-8")
            output.write_text("旧输出\n", encoding="utf-8")
            report.write_text("旧报告\n", encoding="utf-8")
            real_replace = os.replace
            failure_raised = False

            def fail_second_commit(source_path, destination_path):
                nonlocal failure_raised
                destination = Path(destination_path)
                staged = ".tmp-" in Path(source_path).name
                if destination == report and staged and not failure_raised:
                    failure_raised = True
                    raise OSError("模拟提交失败")
                return real_replace(source_path, destination_path)

            with mock.patch(
                "scripts.clean_ocr.os.replace",
                side_effect=fail_second_commit,
                create=True,
            ):
                with self.assertRaisesRegex(OSError, "模拟提交失败"):
                    clean_ocr_file(source, output, report, force=True)

            self.assertEqual(output.read_text(encoding="utf-8"), "旧输出\n")
            self.assertEqual(report.read_text(encoding="utf-8"), "旧报告\n")
            self.assertEqual(list(root.glob(".*.tmp-*")), [])
            self.assertEqual(list(root.glob(".*.bak-*")), [])

    def test_default_write_never_overwrites_file_created_during_commit(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "来源.md"
            output = root / "输出.md"
            report = root / "报告.md"
            source.write_text("新正文\n", encoding="utf-8")
            real_link = os.link
            race_created = False

            def create_racing_report(source_path, destination_path):
                nonlocal race_created
                destination = Path(destination_path)
                if destination == report and not race_created:
                    report.write_text("竞态报告\n", encoding="utf-8")
                    race_created = True
                return real_link(source_path, destination_path)

            with mock.patch(
                "scripts.clean_ocr.os.link",
                side_effect=create_racing_report,
                create=True,
            ):
                with self.assertRaisesRegex(FileExistsError, "已存在"):
                    clean_ocr_file(source, output, report)

            self.assertTrue(race_created)
            self.assertFalse(output.exists())
            self.assertEqual(report.read_text(encoding="utf-8"), "竞态报告\n")
            self.assertEqual(list(root.glob(".*.tmp-*")), [])

    def test_clean_ocr_cli_matches_public_command_and_force(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "输入.md"
            output = root / "cleaned/第1章.md"
            report = root / "reports/第1章_清洗记录.md"
            source.write_text("注意：正文。\n", encoding="utf-8")
            command = [
                sys.executable,
                str(ROOT / "scripts/clean_ocr.py"),
                str(source),
                "--output",
                str(output),
                "--report",
                str(report),
            ]
            completed = subprocess.run(
                command,
                check=False,
                capture_output=True,
                text=True,
            )
            self.assertEqual(completed.returncode, 0, completed.stderr)
            self.assertTrue(output.is_file())
            self.assertTrue(report.is_file())

            rejected = subprocess.run(
                command,
                check=False,
                capture_output=True,
                text=True,
            )
            self.assertNotEqual(rejected.returncode, 0)
            self.assertIn(str(output), rejected.stderr)
            self.assertEqual(rejected.stdout, "")

            forced = subprocess.run(
                command + ["--force"],
                check=False,
                capture_output=True,
                text=True,
            )
            self.assertEqual(forced.returncode, 0, forced.stderr)


if __name__ == "__main__":
    unittest.main()
