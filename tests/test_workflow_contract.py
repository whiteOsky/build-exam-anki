import inspect
import os
import re
import subprocess
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest import mock


SKILL_ROOT = Path(__file__).resolve().parents[1]


def write_project(project, chapters):
    (project / "build").mkdir(parents=True)
    payload = '{"章节顺序": [' + ", ".join(
        '"{}"'.format(chapter) for chapter in chapters
    ) + "]}"
    (project / "build/project.json").write_text(payload, encoding="utf-8")
    (project / "notes").mkdir()


class WorkflowDocumentationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.skill = (SKILL_ROOT / "SKILL.md").read_text(encoding="utf-8")
        cls.input_contract = (
            SKILL_ROOT / "references/input-contract.md"
        ).read_text(encoding="utf-8")
        cls.print_standard = (
            SKILL_ROOT / "references/print-standard.md"
        ).read_text(encoding="utf-8")

    def test_skill_is_short_and_commands_are_cwd_independent(self):
        self.assertLess(len(self.skill.splitlines()), 500)
        self.assertIn(
            'SKILL_ROOT="${CODEX_HOME:-$HOME/.codex}/skills/build-exam-anki"',
            self.skill,
        )
        self.assertRegex(self.skill, r'PROJECT_ROOT="[^"\n]+"')

        shell_blocks = re.findall(r"```bash\n(.*?)```", self.skill, re.DOTALL)
        self.assertTrue(shell_blocks)
        commands = "\n".join(shell_blocks)
        script_lines = [line for line in commands.splitlines() if "scripts/" in line]
        self.assertTrue(script_lines)
        for line in script_lines:
            with self.subTest(line=line):
                self.assertIn('"$SKILL_ROOT/.venv/bin/python"', line)
                self.assertIn('"$SKILL_ROOT/scripts/', line)
        self.assertNotRegex(commands, r"(?m)^\s*python3\s+scripts/")
        self.assertNotRegex(commands, r"(?m)^\s*\.venv/bin/python\s+scripts/")

    def test_source_normalization_routes_are_explicit(self):
        combined = self.skill + "\n" + self.input_contract
        required = [
            "来源规范化提取",
            "pdftotext",
            "扫描型 PDF",
            "OCR",
            "按页",
            "PPTX",
            "DOCX",
            "Pandoc",
            "只读",
            "SQLite",
            "GUID",
            "deck",
            "model",
            "source/existing_tsv",
            "clean_ocr.py",
            "source/extracted",
            "可检索 Markdown",
        ]
        for item in required:
            with self.subTest(item=item):
                self.assertIn(item, combined)

        pdf_only = self.input_contract.index("只有 PDF")
        searchable = self.input_contract.index("可检索 Markdown", pdf_only)
        checklist = self.input_contract.index("build/小节清单.tsv", pdf_only)
        self.assertLess(searchable, checklist)

    def test_hard_gates_and_visual_review_are_documented(self):
        combined = self.skill + "\n" + self.print_standard
        required = [
            "build/小节清单.tsv",
            "SectionID / Title / Source",
            "reports/覆盖表.tsv",
            "SectionID / Title / Status / CardIDs / Reason / Source",
            "真实 GUID",
            "pdftoppm",
            "build/print-qa",
            "view_image",
            "逐页",
            "断图",
            "缺字",
            "重叠",
            "越界",
            "章节跳转",
            "reports/打印视觉复核.tsv",
            "PDF_SHA256 / 页码 / 状态 / 问题",
            "自动渲染不等于视觉通过",
            "audit_outputs.py",
        ]
        for item in required:
            with self.subTest(item=item):
                self.assertIn(item, combined)

    def test_print_output_and_force_contract_are_documented(self):
        combined = self.skill + "\n" + self.print_standard
        self.assertIn("精确为项目 `print/` 根目录", combined)
        self.assertNotIn("新建子目录", combined)
        self.assertIn("--force", combined)
        self.assertIn("默认不覆盖", combined)
        for forbidden in [
            "source/",
            "cleaned/",
            "notes/",
            "cards/",
            "reports/",
            "build/",
            "assets/",
        ]:
            with self.subTest(forbidden=forbidden):
                self.assertIn(forbidden, self.print_standard)


class PrintAssemblyContractTests(unittest.TestCase):
    def test_short_note_has_single_document_and_chapter_h1_without_raw_pagebreak(self):
        from scripts.build_print import assemble_markdown

        with tempfile.TemporaryDirectory() as directory:
            project = Path(directory)
            write_project(project, ["第1章"])
            (project / "notes/第1章_进程_纯背诵版.md").write_text(
                "# 第1章 进程 纯背诵版\n\n## 调度\n\n正文。\n",
                encoding="utf-8",
            )

            markdown, _ = assemble_markdown(project, "操作系统纯背诵笔记")

            h1_titles = re.findall(r"(?m)^# ([^#].*)$", markdown)
            self.assertEqual(h1_titles, ["操作系统纯背诵笔记", "第1章"])
            self.assertNotIn("\\newpage", markdown)
            self.assertEqual(markdown.count("# 操作系统纯背诵笔记"), 1)
            self.assertEqual(markdown.count("# 第1章\n"), 1)
            self.assertNotIn("第1章 进程 纯背诵版", markdown)
            self.assertLess(markdown.index("# 第1章"), markdown.index("## 原教材章首页"))
            self.assertLess(
                markdown.index("## 原教材章首页"),
                markdown.index("## 本章知识主线"),
            )

    def test_nonredundant_note_h1_is_demoted_to_keep_chapter_paging_stable(self):
        from scripts.build_print import assemble_markdown

        with tempfile.TemporaryDirectory() as directory:
            project = Path(directory)
            write_project(project, ["第1章"])
            (project / "notes/第1章.md").write_text(
                "# 核心概念\n\n正文。\n", encoding="utf-8"
            )

            markdown, _ = assemble_markdown(project, "课程笔记")

            self.assertEqual(
                re.findall(r"(?m)^# ([^#].*)$", markdown),
                ["课程笔记", "第1章"],
            )
            self.assertIn("## 核心概念", markdown)
            self.assertNotIn("\\newpage", markdown)

    def test_local_note_images_are_validated_and_counted_as_unique_assets(self):
        from scripts.build_print import assemble_markdown

        with tempfile.TemporaryDirectory() as directory:
            project = Path(directory)
            write_project(project, ["第1章"])
            (project / "assets").mkdir()
            (project / "assets/结构图.png").write_bytes(b"png")
            (project / "notes/第1章.md").write_text(
                "# 第1章\n\n![结构](../assets/结构图.png)\n\n"
                "![重复引用](<../assets/结构图.png>)\n",
                encoding="utf-8",
            )

            _, details = assemble_markdown(project, "课程笔记")

            self.assertEqual(details["预期图片数"], 1)
            self.assertEqual(
                details["笔记图片"], [(project / "assets/结构图.png").resolve()]
            )

    def test_remote_or_missing_note_image_fails_before_rendering(self):
        from scripts.build_print import assemble_markdown

        cases = [
            ("![远程](https://example.com/a.png)", "远程图片"),
            ("![缺失](../assets/missing.png)", "失效图片"),
        ]
        for body, message in cases:
            with self.subTest(message=message), tempfile.TemporaryDirectory() as directory:
                project = Path(directory)
                write_project(project, ["第1章"])
                (project / "notes/第1章.md").write_text(
                    "# 第1章\n\n{}\n".format(body), encoding="utf-8"
                )

                with self.assertRaisesRegex(ValueError, message):
                    assemble_markdown(project, "课程笔记")

    def test_html_note_image_is_counted_and_remote_html_fails(self):
        from scripts.build_print import assemble_markdown

        with tempfile.TemporaryDirectory() as directory:
            project = Path(directory)
            write_project(project, ["第1章"])
            (project / "assets").mkdir()
            (project / "assets/图.png").write_bytes(b"png")
            note = project / "notes/第1章.md"
            note.write_text(
                '# 第1章\n\n<img src="../assets/图.png" alt="图">\n',
                encoding="utf-8",
            )

            _, details = assemble_markdown(project, "课程笔记")
            self.assertEqual(details["预期图片数"], 1)

            note.write_text(
                '# 第1章\n\n<img src="https://example.com/图.png">\n',
                encoding="utf-8",
            )
            with self.assertRaisesRegex(ValueError, "远程图片"):
                assemble_markdown(project, "课程笔记")


class PrintOutputContractTests(unittest.TestCase):
    @staticmethod
    def _render_runner(commands):
        def runner(command, **kwargs):
            commands.append(command)
            tool = Path(command[0]).name
            if tool == "pandoc":
                target = Path(command[command.index("-o") + 1])
                if "--to=docx" in command:
                    with zipfile.ZipFile(target, "w") as archive:
                        archive.writestr("word/document.xml", "<document/>")
                else:
                    target.write_text("<html></html>", encoding="utf-8")
            elif tool == "chrome":
                target = next(
                    Path(item.split("=", 1)[1])
                    for item in command
                    if item.startswith("--print-to-pdf=")
                )
                target.write_bytes(b"%PDF-1.4\n")
            else:
                raise AssertionError("未预期的工具：{}".format(command[0]))
            return subprocess.CompletedProcess(command, 0, "", "")

        return runner

    def test_output_directory_is_exactly_project_print_root(self):
        from scripts.build_print import _prepare_output_directory

        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            project = base / "project"
            project.mkdir()

            self.assertEqual(
                _prepare_output_directory(project, "print"),
                (project / "print").resolve(),
            )
            for invalid in [
                "print/版本一",
                project / "print/版本二",
                "source",
                "cleaned",
                "notes",
                "cards",
                "reports",
                "build",
                "assets",
                base / "outside",
            ]:
                with self.subTest(invalid=invalid), self.assertRaisesRegex(
                    ValueError, "print"
                ):
                    _prepare_output_directory(project, invalid)
            self.assertFalse((base / "outside").exists())

    def test_existing_output_requires_force_and_force_replaces_it(self):
        from scripts.build_print import _promote_files, build_print

        self.assertFalse(inspect.signature(build_print).parameters["force"].default)
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory)
            destination = output / "课程笔记.md"
            destination.write_text("旧内容", encoding="utf-8")
            temporary = output / "新内容.md"
            temporary.write_text("新内容", encoding="utf-8")

            with self.assertRaisesRegex(FileExistsError, "--force"):
                _promote_files([(temporary, destination)], output, force=False)
            self.assertEqual(destination.read_text(encoding="utf-8"), "旧内容")
            self.assertTrue(temporary.exists())

            _promote_files([(temporary, destination)], output, force=True)
            self.assertEqual(destination.read_text(encoding="utf-8"), "新内容")

    def test_build_print_succeeds_then_requires_force_for_same_title(self):
        from scripts.build_print import build_print

        with tempfile.TemporaryDirectory() as directory:
            project = Path(directory)
            write_project(project, ["第1章"])
            (project / "notes/第1章.md").write_text(
                "# 第1章\n\n正文。\n", encoding="utf-8"
            )
            commands = []
            tools = {"pandoc": "/tools/pandoc", "chrome": "/tools/chrome"}
            runner = self._render_runner(commands)

            result = build_print(
                project,
                "课程笔记",
                "print",
                tool_paths=tools,
                runner=runner,
            )
            for path in result["输出"].values():
                self.assertTrue(path.is_file())
            markdown_before = result["输出"]["Markdown"].read_text(encoding="utf-8")

            with self.assertRaisesRegex(FileExistsError, "--force"):
                build_print(
                    project,
                    "课程笔记",
                    "print",
                    tool_paths=tools,
                    runner=runner,
                )
            self.assertEqual(
                result["输出"]["Markdown"].read_text(encoding="utf-8"),
                markdown_before,
            )

            build_print(
                project,
                "课程笔记",
                "print",
                tool_paths=tools,
                runner=runner,
                force=True,
            )
            resource_arguments = [
                item
                for command in commands
                for item in command
                if item.startswith("--resource-path=")
            ]
            self.assertTrue(resource_arguments)
            self.assertTrue(
                all(str(project / "notes") in item for item in resource_arguments)
            )

    def test_default_promotion_does_not_overwrite_file_created_during_commit(self):
        from scripts.build_print import _promote_files

        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory)
            destination = output / "课程笔记.md"
            temporary = output / "临时.md"
            temporary.write_text("新内容", encoding="utf-8")
            real_link = os.link

            def create_racing_output(source, target):
                Path(target).write_text("并发旧成品", encoding="utf-8")
                return real_link(source, target)

            with mock.patch(
                "scripts.build_print.os.link", side_effect=create_racing_output
            ):
                with self.assertRaises(FileExistsError):
                    _promote_files([(temporary, destination)], output, force=False)

            self.assertEqual(
                destination.read_text(encoding="utf-8"), "并发旧成品"
            )
            self.assertEqual(temporary.read_text(encoding="utf-8"), "新内容")

    def test_cli_exposes_force_flag(self):
        source = (SKILL_ROOT / "scripts/build_print.py").read_text(encoding="utf-8")
        self.assertIn('"--force"', source)
        self.assertIn("force=arguments.force", source)


if __name__ == "__main__":
    unittest.main()
