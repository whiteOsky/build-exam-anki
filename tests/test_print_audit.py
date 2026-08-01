import csv
import hashlib
import json
import sqlite3
import subprocess
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from scripts.build_apkg import MODEL_KEY, MODEL_NAME, stable_guid, stable_id
from scripts.common import CARD_FIELDS, to_anki_mathjax


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


def write_project_config(project, chapters, nested=False):
    config = {"章节顺序": chapters}
    payload = {"config": config} if nested else {"纳入章节": chapters}
    path = project / "build/project.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        __import__("json").dumps(payload, ensure_ascii=False),
        encoding="utf-8",
    )


def write_valid_tsv(path, count=1):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=CARD_FIELDS, delimiter="\t")
        writer.writeheader()
        for index in range(count):
            writer.writerow(
                {
                    "Front": "问题{}是什么?".format(index + 1),
                    "Back": "完整答案{}。".format(index + 1),
                    "Extra": "补充结论。",
                    "Mistake": "不要混淆。",
                    "Trigger": "看到题目关键词时。",
                    "Importance": "必背",
                    "Source": "操作系统|第1章",
                    "Tags": "408::操作系统::第1章::必背",
                }
            )


def _valid_card_rows(count):
    return [
        {
            "Front": "问题{}是什么?".format(index + 1),
            "Back": "完整答案{}。".format(index + 1),
            "Extra": "补充结论。",
            "Mistake": "不要混淆。",
            "Trigger": "看到题目关键词时。",
            "Importance": "必背",
            "Source": "操作系统|第1章",
            "Tags": "408::操作系统::第1章::必背",
        }
        for index in range(count)
    ]


def write_apkg(path, note_count=1, include_collection=True, rows=None):
    path.parent.mkdir(parents=True, exist_ok=True)
    rows = rows or _valid_card_rows(note_count)
    database = path.parent / ".collection-fixture.sqlite3"
    connection = sqlite3.connect(str(database))
    try:
        connection.execute("CREATE TABLE col (models TEXT, decks TEXT)")
        connection.execute(
            "CREATE TABLE notes (id INTEGER PRIMARY KEY, guid TEXT, mid INTEGER, flds TEXT, tags TEXT)"
        )
        connection.execute(
            "CREATE TABLE cards (id INTEGER PRIMARY KEY, nid INTEGER, did INTEGER)"
        )
        connection.execute("CREATE TABLE revlog (id INTEGER PRIMARY KEY)")
        connection.execute("CREATE TABLE graves (usn INTEGER, oid INTEGER, type INTEGER)")
        deck_name = "操作系统::第1章"
        model_id = stable_id("model", MODEL_KEY)
        deck_id = stable_id("deck", deck_name)
        models = {
            str(model_id): {
                "id": model_id,
                "name": MODEL_NAME,
                "flds": [{"name": field} for field in CARD_FIELDS],
            }
        }
        decks = {str(deck_id): {"id": deck_id, "name": deck_name}}
        connection.execute(
            "INSERT INTO col (models, decks) VALUES (?, ?)",
            (json.dumps(models, ensure_ascii=False), json.dumps(decks, ensure_ascii=False)),
        )
        for index, row in enumerate(rows, start=1):
            connection.execute(
                "INSERT INTO notes (id, guid, mid, flds, tags) VALUES (?, ?, ?, ?, ?)",
                (
                    index,
                    stable_guid(row["Source"], row["Front"]),
                    model_id,
                    "\x1f".join(
                        to_anki_mathjax(row[field]) for field in CARD_FIELDS
                    ),
                    " {} {} ".format(row["Tags"], row["Importance"]),
                ),
            )
            connection.execute(
                "INSERT INTO cards (id, nid, did) VALUES (?, ?, ?)",
                (index, index, deck_id),
            )
        connection.commit()
    finally:
        connection.close()
    try:
        with zipfile.ZipFile(path, "w") as archive:
            if include_collection:
                archive.write(database, "collection.anki2")
            else:
                archive.writestr("media", b"{}")
    finally:
        database.unlink()


def write_docx(path, include_document=True, image_count=1, formula_count=1):
    path.parent.mkdir(parents=True, exist_ok=True)
    formulas = "".join(
        "<m:oMath><m:r><m:t>x=1</m:t></m:r></m:oMath>"
        for _ in range(formula_count)
    )
    document = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main" '
        'xmlns:m="http://schemas.openxmlformats.org/officeDocument/2006/math">'
        "<w:body><w:p>{}</w:p></w:body></w:document>".format(formulas)
    )
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("[Content_Types].xml", "<Types/>")
        archive.writestr("_rels/.rels", "<Relationships/>")
        if include_document:
            archive.writestr("word/document.xml", document)
        for index in range(image_count):
            archive.writestr("word/media/image{}.png".format(index + 1), b"png")


def write_minimal_pdf(path, page_count=2, width=595.276, height=841.89):
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        (
            "<< /Type /Pages /Kids [{}] /Count {} /MediaBox [0 0 {} {}] >>".format(
                " ".join("{} 0 R".format(index + 3) for index in range(page_count)),
                page_count,
                width,
                height,
            )
        ).encode("ascii"),
    ]
    content_start = 3 + page_count
    font_number = content_start + page_count
    for index in range(page_count):
        objects.append(
            (
                "<< /Type /Page /Parent 2 0 R /Resources << /Font << /F1 {} 0 R >> >> "
                "/Contents {} 0 R >>".format(font_number, content_start + index)
            ).encode("ascii")
        )
    for index in range(page_count):
        stream = "BT /F1 12 Tf 72 760 Td (Page {} content) Tj ET".format(
            index + 1
        ).encode("ascii")
        objects.append(
            b"<< /Length "
            + str(len(stream)).encode("ascii")
            + b" >>\nstream\n"
            + stream
            + b"\nendstream"
        )
    objects.append(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>")

    data = bytearray(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n")
    offsets = [0]
    for number, body in enumerate(objects, start=1):
        offsets.append(len(data))
        data.extend("{} 0 obj\n".format(number).encode("ascii"))
        data.extend(body)
        data.extend(b"\nendobj\n")
    xref = len(data)
    data.extend("xref\n0 {}\n".format(len(objects) + 1).encode("ascii"))
    data.extend(b"0000000000 65535 f \n")
    for offset in offsets[1:]:
        data.extend("{:010d} 00000 n \n".format(offset).encode("ascii"))
    data.extend(
        (
            "trailer\n<< /Size {} /Root 1 0 R >>\nstartxref\n{}\n%%EOF\n".format(
                len(objects) + 1, xref
            )
        ).encode("ascii")
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(bytes(data))


def write_pgm(path, blank=False):
    width = 100
    height = 100
    pixels = bytearray([255] * (width * height))
    if not blank:
        for index in range(1500):
            pixels[index] = 0
    path.write_bytes(
        "P5\n{} {}\n255\n".format(width, height).encode("ascii") + pixels
    )


def completed(command, returncode=0, stdout="", stderr=""):
    return subprocess.CompletedProcess(command, returncode, stdout, stderr)


class FakePdfTools:
    def __init__(
        self, a4=True, blank=False, text="打印正文", numbered_page_size=False
    ):
        self.a4 = a4
        self.blank = blank
        self.text = text
        self.numbered_page_size = numbered_page_size
        self.commands = []

    def __call__(self, command, **kwargs):
        self.commands.append(command)
        tool = Path(command[0]).name
        if tool == "pdfinfo":
            size = "595.276 x 841.890 pts (A4)" if self.a4 else "612 x 792 pts (letter)"
            label = (
                "Page 1 size" if self.numbered_page_size and "-f" in command else "Page size"
            )
            return completed(command, stdout="Pages: 2\n{}: {}\n".format(label, size))
        if tool == "pdftotext":
            return completed(command, stdout=self.text)
        if tool == "pdftoppm":
            prefix = Path(command[-1])
            write_pgm(Path(str(prefix) + "-1.pgm"), blank=self.blank)
            write_pgm(Path(str(prefix) + "-2.pgm"), blank=self.blank)
            return completed(command)
        raise AssertionError("未预期的命令：{}".format(command))


def create_complete_project(project):
    write_project_config(project, ["第1章"])
    (project / "notes").mkdir()
    (project / "notes/第1章_纯背诵版.md").write_text(
        "# 第1章\n\n公式：$x=1$。\n", encoding="utf-8"
    )
    (project / "assets/framework").mkdir(parents=True)
    (project / "assets/framework/第1章_章首页.png").write_bytes(b"png")
    card_rows = _valid_card_rows(1)
    write_valid_tsv(project / "cards/第1章.tsv")
    write_apkg(project / "cards/第1章.apkg", rows=card_rows)
    write_docx(project / "print/课程纯背诵笔记.docx")
    write_minimal_pdf(project / "print/课程纯背诵笔记.pdf")
    pdf_sha256 = hashlib.sha256(
        (project / "print/课程纯背诵笔记.pdf").read_bytes()
    ).hexdigest()
    (project / "reports").mkdir()
    (project / "reports/卡片统计.md").write_text(
        "# 卡片统计\n\n| 章节 | 卡片数 |\n|---|---:|\n| 合计 | 1 |\n",
        encoding="utf-8",
    )
    section = {"SectionID": "1.1", "Title": "基本概念", "Source": "教材第1页"}
    write_tsv(
        project / "build/小节清单.tsv",
        SECTION_FIELDS,
        [section],
    )
    write_tsv(
        project / "reports/覆盖表.tsv",
        COVERAGE_FIELDS,
        [
            {
                "SectionID": section["SectionID"],
                "Title": section["Title"],
                "Status": "已制卡",
                "CardIDs": stable_guid("操作系统|第1章", "问题1是什么?"),
                "Reason": "",
                "Source": section["Source"],
            }
        ],
    )
    write_tsv(
        project / "reports/打印视觉复核.tsv",
        VISUAL_REVIEW_FIELDS,
        [
            {"PDF_SHA256": pdf_sha256, "页码": "1", "状态": "已通过", "问题": ""},
            {"PDF_SHA256": pdf_sha256, "页码": "2", "状态": "已通过", "问题": ""},
        ],
    )


class PrintBuildTests(unittest.TestCase):
    def test_numeric_chapter_matches_chapter_lesson_and_section_filenames(self):
        from scripts.build_print import assemble_markdown

        filenames = [
            "第1章.md",
            "第01章.md",
            "第1讲.md",
            "第01讲.md",
            "第1节.md",
            "第01节.md",
        ]
        for filename in filenames:
            with self.subTest(filename=filename), tempfile.TemporaryDirectory() as directory:
                project = Path(directory)
                write_project_config(project, [1])
                (project / "notes").mkdir()
                (project / "notes" / filename).write_text(
                    "# 数字章节测试\n", encoding="utf-8"
                )

                markdown, details = assemble_markdown(project, "课程笔记")

                self.assertIn("数字章节测试", markdown)
                self.assertEqual(details["章节数"], 1)

    def test_numeric_chapter_does_not_match_larger_chapter_number(self):
        from scripts.build_print import assemble_markdown

        with tempfile.TemporaryDirectory() as directory:
            project = Path(directory)
            write_project_config(project, [1])
            (project / "notes").mkdir()
            (project / "notes/第11章.md").write_text(
                "# 第11章\n", encoding="utf-8"
            )

            with self.assertRaisesRegex(ValueError, "找不到章节.*1"):
                assemble_markdown(project, "课程笔记")

    def test_merged_markdown_uses_config_order_framework_and_placeholders(self):
        from scripts.build_print import assemble_markdown

        with tempfile.TemporaryDirectory() as directory:
            project = Path(directory)
            write_project_config(project, ["第2章", "第1章"], nested=True)
            (project / "notes").mkdir()
            (project / "notes/第1章_一.md").write_text(
                "# 第一章笔记\n\n## 小节甲\n", encoding="utf-8"
            )
            (project / "notes/第2章_二.md").write_text(
                "# 第二章笔记\n\n## 小节乙\n", encoding="utf-8"
            )
            (project / "assets/framework").mkdir(parents=True)
            (project / "assets/framework/第2章_章首页.png").write_bytes(b"png")

            markdown, details = assemble_markdown(project, "操作系统纯背诵笔记")

            self.assertLess(markdown.index("第二章笔记"), markdown.index("第一章笔记"))
            self.assertLess(markdown.index("第2章_章首页.png"), markdown.index("第二章笔记"))
            chapter_one = markdown[markdown.index("# 第1章") :]
            self.assertIn("原教材章首页", chapter_one)
            self.assertIn("本章知识主线", chapter_one)
            self.assertEqual(details["章节数"], 2)
            self.assertEqual(details["预期图片数"], 1)

    def test_missing_external_tool_is_chinese_and_writes_no_outputs(self):
        from scripts.build_print import build_print

        with tempfile.TemporaryDirectory() as directory:
            project = Path(directory)
            write_project_config(project, ["第1章"])
            (project / "notes").mkdir()
            (project / "notes/第1章.md").write_text("# 第1章\n", encoding="utf-8")

            with self.assertRaisesRegex(RuntimeError, "brew install pandoc"):
                build_print(project, "课程笔记", "print", tool_paths={})
            self.assertEqual(list((project / "print").glob("*")), [])

    def test_failed_chrome_keeps_existing_outputs_and_no_temporary_files(self):
        from scripts.build_print import build_print

        with tempfile.TemporaryDirectory() as directory:
            project = Path(directory)
            write_project_config(project, ["第1章"])
            (project / "notes").mkdir()
            (project / "notes/第1章.md").write_text("# 第1章\n", encoding="utf-8")
            output = project / "print"
            output.mkdir()
            for suffix in ["md", "docx", "pdf"]:
                (output / "课程笔记.{}".format(suffix)).write_bytes(b"old")

            def runner(command, **kwargs):
                if Path(command[0]).name == "pandoc":
                    target = Path(command[command.index("-o") + 1])
                    target.write_bytes(b"generated")
                    return completed(command)
                return completed(command, returncode=1, stderr="Chrome 失败")

            with self.assertRaisesRegex(RuntimeError, "Chrome 生成 PDF 失败"):
                build_print(
                    project,
                    "课程笔记",
                    "print",
                    tool_paths={"pandoc": "/tools/pandoc", "chrome": "/tools/chrome"},
                    runner=runner,
                )
            for suffix in ["md", "docx", "pdf"]:
                self.assertEqual((output / "课程笔记.{}".format(suffix)).read_bytes(), b"old")
            self.assertEqual(list(output.glob(".print-build-*")), [])

    def test_build_rejects_relative_output_symlink_escape(self):
        from scripts.build_print import build_print

        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            project = base / "project"
            outside = base / "outside"
            project.mkdir()
            outside.mkdir()
            write_project_config(project, ["第1章"])
            (project / "notes").mkdir()
            (project / "notes/第1章.md").write_text("# 第1章\n", encoding="utf-8")
            (project / "print").symlink_to(outside, target_is_directory=True)

            with self.assertRaisesRegex(ValueError, "符号链接"):
                build_print(
                    project,
                    "课程笔记",
                    "print",
                    tool_paths={"pandoc": "/tools/pandoc", "chrome": "/tools/chrome"},
                )
            self.assertEqual(list(outside.iterdir()), [])


class OutputAuditTests(unittest.TestCase):
    TOOLS = {
        "pdfinfo": "/tools/pdfinfo",
        "pdftotext": "/tools/pdftotext",
        "pdftoppm": "/tools/pdftoppm",
    }

    def test_complete_minimal_outputs_pass_and_write_chinese_checklist(self):
        from scripts.audit_outputs import audit_outputs

        with tempfile.TemporaryDirectory() as directory:
            project = Path(directory)
            create_complete_project(project)
            runner = FakePdfTools()

            result = audit_outputs(project, tool_paths=self.TOOLS, runner=runner)

            self.assertTrue(result["通过"], result)
            self.assertEqual(result["统计"]["TSV卡片数"], 1)
            self.assertEqual(result["统计"]["APKG卡片数"], 1)
            report = project / "reports/输出审计.md"
            content = report.read_text(encoding="utf-8")
            self.assertIn("# 输出审计 QA 清单", content)
            self.assertIn("- [x] TSV 八字段", content)
            self.assertIn("- [x] PDF 全页渲染", content)
            self.assertTrue(any(Path(command[0]).name == "pdftoppm" for command in runner.commands))

    def test_pdfinfo_numbered_page_size_format_is_supported(self):
        from scripts.audit_outputs import audit_outputs

        with tempfile.TemporaryDirectory() as directory:
            project = Path(directory)
            create_complete_project(project)

            result = audit_outputs(
                project,
                tool_paths=self.TOOLS,
                runner=FakePdfTools(numbered_page_size=True),
            )

            self.assertTrue(result["通过"], result)

    def test_docx_requires_document_expected_image_and_formula_object(self):
        from scripts.audit_outputs import audit_outputs

        cases = [
            (False, 1, 1, "word/document.xml"),
            (True, 0, 1, "预期图片"),
            (True, 1, 0, "公式对象"),
        ]
        for include_document, image_count, formula_count, message in cases:
            with self.subTest(message=message), tempfile.TemporaryDirectory() as directory:
                project = Path(directory)
                create_complete_project(project)
                write_docx(
                    project / "print/课程纯背诵笔记.docx",
                    include_document=include_document,
                    image_count=image_count,
                    formula_count=formula_count,
                )
                result = audit_outputs(project, tool_paths=self.TOOLS, runner=FakePdfTools())
                self.assertFalse(result["通过"])
                self.assertIn(message, "\n".join(item["说明"] for item in result["检查"]))

    def test_fake_apkg_and_count_mismatch_fail(self):
        from scripts.audit_outputs import audit_outputs

        with tempfile.TemporaryDirectory() as directory:
            project = Path(directory)
            create_complete_project(project)
            (project / "cards/第1章.apkg").write_bytes(b"not a zip")
            result = audit_outputs(project, tool_paths=self.TOOLS, runner=FakePdfTools())
            self.assertFalse(result["通过"])
            self.assertIn("APKG 无法解压", "\n".join(item["说明"] for item in result["检查"]))

        with tempfile.TemporaryDirectory() as directory:
            project = Path(directory)
            create_complete_project(project)
            (project / "reports/卡片统计.md").write_text(
                "| 合计 | 2 |\n", encoding="utf-8"
            )
            result = audit_outputs(project, tool_paths=self.TOOLS, runner=FakePdfTools())
            self.assertFalse(result["通过"])
            self.assertIn("数量不一致", "\n".join(item["说明"] for item in result["检查"]))

    def test_non_a4_bare_latex_and_blank_pages_fail(self):
        from scripts.audit_outputs import audit_outputs

        cases = [
            (FakePdfTools(a4=False), "A4"),
            (FakePdfTools(text=r"裸露公式 \\frac{a}{b}"), "裸露 LaTeX"),
            (FakePdfTools(blank=True), "近空白页"),
            (FakePdfTools(text="图片 {width=50%}"), "{width=...}"),
        ]
        for runner, message in cases:
            with self.subTest(message=message), tempfile.TemporaryDirectory() as directory:
                project = Path(directory)
                create_complete_project(project)
                result = audit_outputs(project, tool_paths=self.TOOLS, runner=runner)
                self.assertFalse(result["通过"])
                self.assertIn(message, "\n".join(item["说明"] for item in result["检查"]))

    def test_missing_poppler_tools_reports_install_advice(self):
        from scripts.audit_outputs import audit_outputs

        with tempfile.TemporaryDirectory() as directory:
            project = Path(directory)
            create_complete_project(project)
            result = audit_outputs(project, tool_paths={}, runner=FakePdfTools())
            self.assertFalse(result["通过"])
            details = "\n".join(item["说明"] for item in result["检查"])
            self.assertIn("brew install poppler", details)
            self.assertIn("pdftoppm", details)

    def test_audit_report_rejects_symlink_escape(self):
        from scripts.audit_outputs import audit_outputs

        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            project = base / "project"
            outside = base / "outside"
            project.mkdir()
            outside.mkdir()
            create_complete_project(project)
            statistics = (project / "reports/卡片统计.md").read_bytes()
            for path in (project / "reports").iterdir():
                path.unlink()
            (project / "reports").rmdir()
            (outside / "卡片统计.md").write_bytes(statistics)
            (project / "reports").symlink_to(outside, target_is_directory=True)

            with self.assertRaisesRegex(ValueError, "符号链接"):
                audit_outputs(project, tool_paths=self.TOOLS, runner=FakePdfTools())
            self.assertEqual({path.name for path in outside.iterdir()}, {"卡片统计.md"})


if __name__ == "__main__":
    unittest.main()
