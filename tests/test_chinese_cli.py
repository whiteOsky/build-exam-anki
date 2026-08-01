"""公开命令行入口的中文文案与空内容门禁回归测试。"""

import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from scripts.common import CARD_FIELDS
from scripts.validate_notes import validate_note


SCRIPTS = [
    "init_project.py",
    "inspect_sources.py",
    "clean_ocr.py",
    "validate_notes.py",
    "validate_cards.py",
    "build_apkg.py",
    "build_reports.py",
    "build_print.py",
    "audit_outputs.py",
]
POSITIONAL_SCRIPTS = {
    "init_project.py",
    "inspect_sources.py",
    "clean_ocr.py",
    "validate_notes.py",
    "validate_cards.py",
    "build_apkg.py",
}
FORBIDDEN_ARGPARSE_TEXT = [
    "usage",
    "positional arguments",
    "optional arguments",
    "options",
    "show this help message and exit",
    "error",
]


def run_script(script, *arguments):
    return subprocess.run(
        [sys.executable, str(ROOT / "scripts" / script), *arguments],
        check=False,
        capture_output=True,
        text=True,
    )


class ChineseCliTests(unittest.TestCase):
    def assert_argparse_text_is_chinese(self, output):
        lowered = output.lower()
        for phrase in FORBIDDEN_ARGPARSE_TEXT:
            self.assertNotIn(phrase, lowered, output)

    def test_all_public_scripts_have_chinese_help(self):
        for script in SCRIPTS:
            with self.subTest(script=script):
                completed = run_script(script, "--help")
                output = completed.stdout + completed.stderr

                self.assertEqual(completed.returncode, 0, output)
                self.assertIn("用法", output)
                self.assertIn("选项", output)
                self.assertIn("显示帮助信息并退出", output)
                if script in POSITIONAL_SCRIPTS:
                    self.assertIn("位置参数", output)
                self.assert_argparse_text_is_chinese(output)

    def test_all_public_scripts_have_chinese_missing_argument_errors(self):
        for script in SCRIPTS:
            with self.subTest(script=script):
                completed = run_script(script)
                output = completed.stdout + completed.stderr

                self.assertEqual(completed.returncode, 2, output)
                self.assertIn("用法", output)
                self.assertIn("参数错误", output)
                self.assert_argparse_text_is_chinese(output)


class EmptyContentGateTests(unittest.TestCase):
    def test_empty_whitespace_and_heading_only_notes_fail(self):
        cases = [
            "",
            " \n\t\n",
            "# 标题\n",
            "# 标题\n\n## 小节\n",
            "#\n\n##   \n",
        ]
        for text in cases:
            with self.subTest(text=text):
                result = validate_note(text)

                self.assertFalse(result["通过"], result)
                self.assertIn(
                    "笔记为空或无有效知识内容",
                    result["错误"],
                )

    def test_minimal_note_with_knowledge_content_passes(self):
        result = validate_note("# 进程\n\n进程是程序的一次执行过程。\n")

        self.assertTrue(result["通过"], result)
        self.assertNotIn("笔记为空或无有效知识内容", result["错误"])


class ValidationCliGateTests(unittest.TestCase):
    def test_note_warnings_are_counted_and_listed_on_stdout(self):
        with tempfile.TemporaryDirectory() as directory:
            note = Path(directory) / "warning.md"
            note.write_text(
                "# 标题\n\n有效正文。\n\n![图](https://example.com/a.png)\n",
                encoding="utf-8",
            )

            completed = run_script("validate_notes.py", str(note))

        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertIn("警告：共1项", completed.stdout)
        self.assertIn("远程 URL", completed.stdout)

    def test_note_errors_keep_nonzero_exit_and_still_list_warnings(self):
        with tempfile.TemporaryDirectory() as directory:
            note = Path(directory) / "error-and-warning.md"
            note.write_text(
                (
                    "# 标题\n\n扫码关注公众号。\n\n"
                    "![图](https://example.com/a.png)\n"
                ),
                encoding="utf-8",
            )

            completed = run_script("validate_notes.py", str(note))

        self.assertNotEqual(completed.returncode, 0)
        self.assertIn("警告：共1项", completed.stdout)
        self.assertIn("远程 URL", completed.stdout)
        self.assertIn("明确广告或二维码导流", completed.stderr)

    def test_empty_card_tsv_fails_cli_with_chinese_error(self):
        with tempfile.TemporaryDirectory() as directory:
            cards = Path(directory) / "empty.tsv"
            cards.write_text("\t".join(CARD_FIELDS) + "\n", encoding="utf-8")

            completed = run_script("validate_cards.py", str(cards))

        self.assertNotEqual(completed.returncode, 0)
        output = completed.stdout + completed.stderr
        self.assertIn("卡片 TSV 至少包含1张卡片", output)


if __name__ == "__main__":
    unittest.main()
