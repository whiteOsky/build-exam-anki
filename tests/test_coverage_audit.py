import csv
import hashlib
import json
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

from scripts.build_apkg import stable_guid
from scripts.common import CARD_FIELDS
from test_print_audit import FakePdfTools, create_complete_project, write_apkg


COVERAGE_FIELDS = [
    "SectionID",
    "Title",
    "Status",
    "CardIDs",
    "Reason",
    "Source",
]
SECTION_FIELDS = ["SectionID", "Title", "Source"]
VISUAL_FIELDS = ["PDF_SHA256", "页码", "状态", "问题"]
PDF_TOOLS = {
    "pdfinfo": "/tools/pdfinfo",
    "pdftotext": "/tools/pdftotext",
    "pdftoppm": "/tools/pdftoppm",
}


def write_tsv(path, fields, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, delimiter="\t")
        writer.writeheader()
        writer.writerows(rows)


def card(front="问题1是什么?", source="操作系统|第1章"):
    return {
        "Front": front,
        "Back": "完整答案。",
        "Extra": "补充结论。",
        "Mistake": "不要混淆。",
        "Trigger": "看到题目关键词时。",
        "Importance": "必背",
        "Source": source,
        "Tags": "408::操作系统::第1章::必背",
    }


def coverage_row(card_ids, section_id="1.1", title="基本概念", source="教材第1页"):
    return {
        "SectionID": section_id,
        "Title": title,
        "Status": "已制卡",
        "CardIDs": card_ids,
        "Reason": "",
        "Source": source,
    }


def section_row(section_id="1.1", title="基本概念", source="教材第1页"):
    return {"SectionID": section_id, "Title": title, "Source": source}


def prepare_coverage_project(project, cards=None, coverage_path=None):
    cards = cards or [card()]
    write_tsv(project / "cards/第1章.tsv", CARD_FIELDS, cards)
    write_tsv(project / "build/小节清单.tsv", SECTION_FIELDS, [section_row()])
    card_ids = [stable_guid(item["Source"], item["Front"]) for item in cards]
    coverage = coverage_path or project / "reports/覆盖表.tsv"
    write_tsv(coverage, COVERAGE_FIELDS, [coverage_row("，".join(card_ids))])
    return coverage, card_ids


def prepare_complete_audit_project(project, coverage_relative="reports/覆盖表.tsv"):
    create_complete_project(project)
    rows = [card()]
    write_tsv(project / "cards/第1章.tsv", CARD_FIELDS, rows)
    write_apkg(project / "cards/第1章.apkg", rows=rows)
    write_tsv(project / "build/小节清单.tsv", SECTION_FIELDS, [section_row()])
    identifier = stable_guid(rows[0]["Source"], rows[0]["Front"])
    coverage = project / coverage_relative
    write_tsv(coverage, COVERAGE_FIELDS, [coverage_row(identifier)])
    write_tsv(
        project / "reports/打印视觉复核.tsv",
        VISUAL_FIELDS,
        [
            {
                "PDF_SHA256": hashlib.sha256(
                    (project / "print/课程纯背诵笔记.pdf").read_bytes()
                ).hexdigest(),
                "页码": "1",
                "状态": "已通过",
                "问题": "",
            },
            {
                "PDF_SHA256": hashlib.sha256(
                    (project / "print/课程纯背诵笔记.pdf").read_bytes()
                ).hexdigest(),
                "页码": "2",
                "状态": "已通过",
                "问题": "",
            },
        ],
    )
    return coverage


class CoverageProofTests(unittest.TestCase):
    def test_empty_coverage_table_is_rejected(self):
        from scripts.build_reports import build_reports

        with tempfile.TemporaryDirectory() as directory:
            project = Path(directory)
            prepare_coverage_project(project)
            write_tsv(project / "reports/覆盖表.tsv", COVERAGE_FIELDS, [])

            with self.assertRaisesRegex(ValueError, "覆盖表.*至少一条"):
                build_reports(project, "reports/覆盖表.tsv")

    def test_missing_section_manifest_cannot_claim_complete_coverage(self):
        from scripts.build_reports import build_reports

        with tempfile.TemporaryDirectory() as directory:
            project = Path(directory)
            coverage, _ = prepare_coverage_project(project)
            (project / "build/小节清单.tsv").unlink()

            with self.assertRaisesRegex(ValueError, "小节清单.*无法证明逐节完整"):
                build_reports(project, coverage)
            self.assertFalse((project / "reports/覆盖校验.md").exists())

    def test_real_card_ids_allow_multiple_chinese_and_ascii_separators(self):
        from scripts.build_reports import build_reports

        with tempfile.TemporaryDirectory() as directory:
            project = Path(directory)
            rows = [card("问题1是什么?"), card("问题2是什么?")]
            coverage, identifiers = prepare_coverage_project(project, rows)
            write_tsv(
                coverage,
                COVERAGE_FIELDS,
                [coverage_row("{}； {}".format(*identifiers))],
            )

            result = build_reports(project, coverage)

            self.assertEqual(result["小节数"], 1)
            report = (project / "reports/覆盖校验.md").read_text(encoding="utf-8")
            self.assertIn("已按来源小节清单逐节核对", report)
            self.assertIn("CardIDs 均对应项目内真实卡片", report)

    def test_missing_repeated_or_ambiguous_card_ids_are_rejected(self):
        from scripts.build_reports import build_reports

        cases = [
            ("不存在", lambda identifiers: "0" * 20),
            ("重复", lambda identifiers: "{0},{0}".format(identifiers[0])),
        ]
        for message, make_ids in cases:
            with self.subTest(message=message), tempfile.TemporaryDirectory() as directory:
                project = Path(directory)
                coverage, identifiers = prepare_coverage_project(project)
                write_tsv(
                    coverage,
                    COVERAGE_FIELDS,
                    [coverage_row(make_ids(identifiers))],
                )
                with self.assertRaisesRegex(ValueError, message):
                    build_reports(project, coverage)

        with tempfile.TemporaryDirectory() as directory:
            project = Path(directory)
            duplicate = card()
            coverage, _ = prepare_coverage_project(project, [duplicate])
            write_tsv(project / "cards/重复.tsv", CARD_FIELDS, [duplicate])
            with self.assertRaisesRegex(ValueError, "卡片稳定编号重复"):
                build_reports(project, coverage)

    def test_coverage_and_manifest_must_have_identical_identity_fields(self):
        from scripts.build_reports import build_reports

        cases = [
            (section_row("1.2"), "SectionID 集合"),
            (section_row(title="不同标题"), "Title 不一致"),
            (section_row(source="不同来源"), "Source 不一致"),
        ]
        for manifest_row, message in cases:
            with self.subTest(message=message), tempfile.TemporaryDirectory() as directory:
                project = Path(directory)
                coverage, _ = prepare_coverage_project(project)
                write_tsv(
                    project / "build/小节清单.tsv",
                    SECTION_FIELDS,
                    [manifest_row],
                )
                with self.assertRaisesRegex(ValueError, message):
                    build_reports(project, coverage)


class UnifiedCoverageAuditTests(unittest.TestCase):
    def audit(self, project):
        from scripts.audit_outputs import audit_outputs

        return audit_outputs(
            project,
            tool_paths=PDF_TOOLS,
            runner=FakePdfTools(),
        )

    def test_complete_assets_pass_with_distinct_automatic_and_visual_results(self):
        with tempfile.TemporaryDirectory() as directory:
            project = Path(directory)
            prepare_complete_audit_project(project)
            visual = project / "reports/打印视觉复核.tsv"
            original = visual.read_bytes()

            result = self.audit(project)

            self.assertTrue(result["通过"], result)
            self.assertTrue(
                any(item["项目"] == "PDF 视觉复核记录" for item in result["检查"])
            )
            self.assertEqual(visual.read_bytes(), original)
            report = (project / "reports/输出审计.md").read_text(encoding="utf-8")
            self.assertIn("PDF 自动检查通过", report)
            self.assertIn("视觉复核已记录", report)
            for unsupported_claim in ["断图检查通过", "重叠检查通过", "缺字检查通过"]:
                self.assertNotIn(unsupported_claim, report)

    def test_project_config_can_select_a_nondefault_coverage_table(self):
        with tempfile.TemporaryDirectory() as directory:
            project = Path(directory)
            prepare_complete_audit_project(project, "reports/自定义覆盖.tsv")
            config_path = project / "build/project.json"
            config = json.loads(config_path.read_text(encoding="utf-8"))
            config["覆盖表"] = "reports/自定义覆盖.tsv"
            config_path.write_text(
                json.dumps(config, ensure_ascii=False), encoding="utf-8"
            )

            result = self.audit(project)

            self.assertTrue(result["通过"], result)
            coverage_check = next(
                item for item in result["检查"] if item["项目"] == "逐节覆盖与卡片映射"
            )
            self.assertIn("自定义覆盖.tsv", coverage_check["说明"])

    def test_missing_required_coverage_note_or_visual_assets_fail(self):
        removals = [
            ("build/小节清单.tsv", "小节清单"),
            ("reports/覆盖表.tsv", "覆盖表"),
            ("notes/第1章_纯背诵版.md", "notes 目录中没有 Markdown"),
            ("reports/打印视觉复核.tsv", "缺少打印视觉复核"),
        ]
        for relative, message in removals:
            with self.subTest(relative=relative), tempfile.TemporaryDirectory() as directory:
                project = Path(directory)
                prepare_complete_audit_project(project)
                (project / relative).unlink()

                result = self.audit(project)

                self.assertFalse(result["通过"])
                details = "\n".join(item["说明"] for item in result["检查"])
                self.assertIn(message, details)

    def test_note_errors_and_warnings_are_both_audit_failures(self):
        cases = [
            ("# 第1章\n\n扫码关注公众号。\n", "错误"),
            ("# 第1章\n\n![图](https://example.com/a.png)\n", "警告"),
        ]
        for content, message in cases:
            with self.subTest(message=message), tempfile.TemporaryDirectory() as directory:
                project = Path(directory)
                prepare_complete_audit_project(project)
                (project / "notes/第1章_纯背诵版.md").write_text(
                    content, encoding="utf-8"
                )

                result = self.audit(project)

                self.assertFalse(result["通过"])
                note_check = next(
                    item for item in result["检查"] if item["项目"] == "笔记校验"
                )
                self.assertIn(message, note_check["说明"])

    def test_visual_review_requires_every_pdf_page_and_passed_status(self):
        cases = [
            (
                [{"页码": "1", "状态": "已通过", "问题": ""}],
                "缺少页码：2",
            ),
            (
                [
                    {"页码": "1", "状态": "已通过", "问题": ""},
                    {"页码": "2", "状态": "待复核", "问题": "公式位置待看"},
                ],
                "第2页状态不是已通过",
            ),
        ]
        for rows, message in cases:
            with self.subTest(message=message), tempfile.TemporaryDirectory() as directory:
                project = Path(directory)
                prepare_complete_audit_project(project)
                pdf_sha256 = hashlib.sha256(
                    (project / "print/课程纯背诵笔记.pdf").read_bytes()
                ).hexdigest()
                for row in rows:
                    row["PDF_SHA256"] = pdf_sha256
                write_tsv(
                    project / "reports/打印视觉复核.tsv",
                    VISUAL_FIELDS,
                    rows,
                )

                result = self.audit(project)

                self.assertFalse(result["通过"])
                visual_check = next(
                    item for item in result["检查"] if item["项目"] == "PDF 视觉复核记录"
                )
                self.assertIn(message, visual_check["说明"])

    def test_markdown_visual_review_table_is_supported(self):
        with tempfile.TemporaryDirectory() as directory:
            project = Path(directory)
            prepare_complete_audit_project(project)
            (project / "reports/打印视觉复核.tsv").unlink()
            pdf_sha256 = hashlib.sha256(
                (project / "print/课程纯背诵笔记.pdf").read_bytes()
            ).hexdigest()
            (project / "reports/打印视觉复核.md").write_text(
                "# 打印视觉复核\n\n"
                "| PDF_SHA256 | 页码 | 状态 | 问题 |\n"
                "|---|---:|---|---|\n"
                "| {0} | 1 | 已通过 | |\n"
                "| {0} | 2 | 已通过 | |\n".format(pdf_sha256),
                encoding="utf-8",
            )

            result = self.audit(project)

            self.assertTrue(result["通过"], result)


if __name__ == "__main__":
    unittest.main()
