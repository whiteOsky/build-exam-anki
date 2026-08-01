import csv
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from scripts.common import CARD_FIELDS
from scripts.validate_cards import validate_rows, validate_tsv


def valid_row(front="进程与程序的主要区别是什么?"):
    return {
        "Front": front,
        "Back": "进程是动态执行过程，程序是静态代码。",
        "Extra": "区分动态与静态。",
        "Mistake": "不要把程序视为运行实体。",
        "Trigger": "看到动态性、并发性时触发。",
        "Importance": "必背",
        "Source": "第2章 进程管理",
        "Tags": "408::操作系统::进程",
    }


class CardValidationTests(unittest.TestCase):
    def test_valid_rows_pass(self):
        row = valid_row()
        row["Extra"] = r"公式依次为 $a$、$$b$$、\(c\)、\[d\]。"
        result = validate_rows([row])
        self.assertEqual(set(result), {"通过", "卡片数", "错误", "警告"})
        self.assertTrue(result["通过"])
        self.assertEqual(result["卡片数"], 1)
        self.assertEqual(result["错误"], [])

    def test_detects_normalized_duplicate_front(self):
        result = validate_rows(
            [valid_row("Ａ＋Ｂ 是什么？"), valid_row("  A+B\n是什么?  ")]
        )
        self.assertFalse(result["通过"])
        self.assertIn("Front 重复", "\n".join(result["错误"]))

    def test_detects_empty_fields_importance_tags_source_and_math(self):
        row = valid_row()
        row["Back"] = ""
        row["Importance"] = "重点"
        row["Tags"] = "操作系统"
        row["Source"] = ""
        row["Extra"] = "公式 $T=t_c-t_a"

        result = validate_rows([row])
        messages = "\n".join(result["错误"])
        self.assertIn("Back 为空", messages)
        self.assertIn("Importance 非法", messages)
        self.assertIn("Tags 缺少 :: 层级", messages)
        self.assertIn("Source 为空", messages)
        self.assertIn("MathJax 定界符不配对", messages)

    def test_generic_fronts_are_warnings(self):
        fronts = [
            "第3个知识点是什么?",
            "第N个知识点是什么?",
            "本节第1个知识点是什么",
            "本节调度是什么?",
            "请回忆上述内容",
            "这是什么?",
            "请简述本节内容",
            "请概述本章内容",
            "请说明上述内容",
        ]
        result = validate_rows([valid_row(front) for front in fronts])
        self.assertTrue(result["通过"])
        self.assertEqual(len(result["警告"]), 9)
        self.assertTrue(all("问题过于泛化" in item for item in result["警告"]))

    def test_detects_reversed_and_nested_mathjax_delimiters(self):
        invalid_fragments = [
            r"\) x \(",
            r"\] x \[",
            r"\( a \[ b \) c \]",
            r"$a $$b$$ c$",
            r"$$a $b$ c$$",
        ]
        for fragment in invalid_fragments:
            with self.subTest(fragment=fragment):
                row = valid_row()
                row["Extra"] = fragment
                result = validate_rows([row])
                self.assertFalse(result["通过"])
                self.assertIn("MathJax 定界符不配对", "\n".join(result["错误"]))

    def test_currency_dollar_is_not_mathjax(self):
        row = valid_row()
        row["Extra"] = "原价 $5，限时优惠。"
        result = validate_rows([row])
        self.assertTrue(result["通过"], result)

    def test_validate_tsv_checks_header_and_valid_content(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            valid_path = root / "valid.tsv"
            with valid_path.open("w", encoding="utf-8", newline="") as handle:
                writer = csv.DictWriter(handle, fieldnames=CARD_FIELDS, delimiter="\t")
                writer.writeheader()
                writer.writerow(valid_row())
            self.assertTrue(validate_tsv(valid_path)["通过"])

            bad_path = root / "bad.tsv"
            bad_path.write_text("Front\tBack\n问\t答\n", encoding="utf-8")
            result = validate_tsv(bad_path)
            self.assertFalse(result["通过"])
            self.assertIn("表头", "\n".join(result["错误"]))

            completed = subprocess.run(
                [
                    sys.executable,
                    str(ROOT / "scripts/validate_cards.py"),
                    str(bad_path),
                ],
                check=False,
                capture_output=True,
                text=True,
            )
            self.assertNotEqual(completed.returncode, 0)
            self.assertEqual(completed.stdout, "")
            self.assertIn(str(bad_path), completed.stderr)


if __name__ == "__main__":
    unittest.main()
