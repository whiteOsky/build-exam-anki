import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from scripts.common import find_math_delimiter_error
from scripts.validate_notes import validate_note


class NoteValidationTests(unittest.TestCase):
    def test_valid_note_passes(self):
        text = r"""# 进程管理

## 状态转换

注意：周转时间为 $T=t_c-t_a$。

$$
W = T - t_s
$$

Anki 行内公式为 \(P=1\)，块公式为 \[Q=2\]。
"""
        result = validate_note(text)
        self.assertTrue(result["通过"])
        self.assertEqual(result["错误"], [])

    def test_detects_advertising_qr_and_warns_for_unverified_images(self):
        text = """# 标题
扫码关注公众号领取资料
二维码见下图
![图](missing.png)
<img src="bad.png">
"""
        result = validate_note(text)
        messages = "\n".join(result["错误"])
        self.assertFalse(result["通过"])
        self.assertIn("广告", messages)
        self.assertIn("二维码", messages)
        self.assertIn("无法验证图片", "\n".join(result["警告"]))

    def test_detects_qr_promotional_guidance(self):
        lines = [
            "扫描书中二维码即可快速定位视频讲解",
            "扫码获取资料",
            "二维码获取配套题库",
        ]
        for line in lines:
            with self.subTest(line=line):
                result = validate_note("# 标题\n\n{}\n".format(line))
                self.assertFalse(result["通过"], result)
                self.assertIn("二维码", "\n".join(result["错误"]))

    def test_allows_academic_qr_discussion(self):
        lines = [
            "二维码技术通过黑白矩阵编码数据，并使用纠错码提高可靠性。",
            "扫描二维码属于图像识别流程，解码器再恢复其中的数据。",
            "扫描二维码是本课程图像识别部分的示例。",
        ]
        for line in lines:
            with self.subTest(line=line):
                result = validate_note("# 标题\n\n{}\n".format(line))
                self.assertTrue(result["通过"], result)

    def test_skips_fenced_and_inline_code(self):
        text = r"""# 标题

```text
### 1
扫码关注公众号
![图](missing.png)
$未闭合
\frac{x}{y}
```

正文代码 `扫码关注 $ \frac{x}{y} ![图](missing.png)` 不参与校验。
"""
        result = validate_note(text)
        self.assertTrue(result["通过"], result)

    def test_markdown_images_use_note_path_and_plain_text_only_warns(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            note_path = root / "笔记.md"
            assets = root / "assets"
            assets.mkdir()
            (assets / "存在.png").write_bytes(b"png")

            existing = validate_note(
                "# 标题\n\n![图](assets/存在.png)\n",
                note_path=note_path,
            )
            self.assertTrue(existing["通过"], existing)

            missing = validate_note(
                "# 标题\n\n![图](assets/缺失.png)\n",
                note_path=note_path,
            )
            self.assertFalse(missing["通过"])
            self.assertIn("失效图片", "\n".join(missing["错误"]))

            plain = validate_note("# 标题\n\n![图](assets/未知.png)\n")
            self.assertTrue(plain["通过"], plain)
            self.assertIn("无法验证图片", "\n".join(plain["警告"]))

    def test_markdown_image_targets_support_balancing_escapes_and_angles(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            note_path = root / "笔记.md"
            assets = root / "assets"
            assets.mkdir()
            (assets / "a_(1).png").write_bytes(b"png")
            text = r"""# 标题

![括号平衡](assets/a_(1).png)
![反斜杠转义](assets/a_\(1\).png)
![尖括号目标](<assets/a_(1).png>)
"""

            result = validate_note(text, note_path=note_path)

            self.assertTrue(result["通过"], result)
            self.assertEqual(result["警告"], [])

    def test_html_images_validate_local_and_warn_for_remote_sources(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            note_path = root / "笔记.md"
            assets = root / "assets"
            assets.mkdir()
            (assets / "存在.png").write_bytes(b"png")

            existing = validate_note(
                '# 标题\n\n<img alt=">" src="assets/存在.png">\n',
                note_path=note_path,
            )
            self.assertTrue(existing["通过"], existing)
            self.assertEqual(existing["警告"], [])

            missing = validate_note(
                '# 标题\n\n<img src="assets/缺失.png" alt="图">\n',
                note_path=note_path,
            )
            self.assertFalse(missing["通过"])
            self.assertIn("失效图片", "\n".join(missing["错误"]))

            remote = validate_note(
                '# 标题\n\n<IMG SRC="https://example.com/图.png">\n',
                note_path=note_path,
            )
            self.assertTrue(remote["通过"], remote)
            self.assertIn("无法验证图片", "\n".join(remote["警告"]))

    def test_currency_dollar_is_not_math(self):
        currency_lines = ["原 $5", "价格$5元", "价格为 $5，限时优惠。"]
        for line in currency_lines:
            with self.subTest(line=line):
                self.assertIsNone(find_math_delimiter_error(line))
                result = validate_note("# 费用\n\n{}\n".format(line))
                self.assertTrue(result["通过"], result)

    def test_dollar_prefixed_math_is_not_mistaken_for_currency(self):
        formulas = [
            r"$0,1,\cdots,r-1$",
            r"$0...$",
            r"$5+x$",
            r"$12.5\times x$",
        ]
        for formula in formulas:
            with self.subTest(formula=formula):
                self.assertIsNone(find_math_delimiter_error(formula))
                result = validate_note("# 数学\n\n公式为 {}。\n".format(formula))
                self.assertTrue(result["通过"], result)

    def test_detects_heading_structure_problems(self):
        text = """# 一级
正文后紧跟标题
### 跨级标题
#### 1.
"""
        result = validate_note(text)
        messages = "\n".join(result["错误"])
        self.assertIn("标题层级跳跃", messages)
        self.assertIn("标题前缺少空行", messages)
        self.assertIn("纯序号标题", messages)

    def test_detects_math_delimiters_and_bare_latex(self):
        text = """# 数学

公式 $a+b。

裸露命令为 \\operatorname{avg}，下标是 x_1。
"""
        result = validate_note(text)
        messages = "\n".join(result["错误"])
        self.assertIn("数学定界符不配对", messages)
        self.assertIn("数学环境外裸露 LaTeX", messages)

    def test_detects_reversed_and_nested_math_delimiters(self):
        invalid_fragments = [
            r"\) x \(",
            r"\] x \[",
            r"\( a \[ b \) c \]",
            r"$a $$b$$ c$",
            r"$$a $b$ c$$",
        ]
        for fragment in invalid_fragments:
            with self.subTest(fragment=fragment):
                result = validate_note("# 数学\n\n{}\n".format(fragment))
                self.assertFalse(result["通过"])
                self.assertIn("数学定界符", "\n".join(result["错误"]))

    def test_detects_first_heading_level_and_parenthesized_number_titles(self):
        result = validate_note("### 首个标题\n")
        self.assertFalse(result["通过"])
        self.assertIn("标题层级异常", "\n".join(result["错误"]))

        for title in ["（1）", "(1)", "（一）", "(一)"]:
            with self.subTest(title=title):
                result = validate_note("# 总标题\n\n## {}\n".format(title))
                self.assertFalse(result["通过"])
                self.assertIn("纯序号标题", "\n".join(result["错误"]))

    def test_detects_common_ocr_image_placeholders(self):
        for placeholder in ["【图片】", "[图片]", "【图】", "[图]"]:
            with self.subTest(placeholder=placeholder):
                result = validate_note("# 标题\n\n{}\n".format(placeholder))
                self.assertFalse(result["通过"])
                self.assertIn("失效图片", "\n".join(result["错误"]))

    def test_cli_returns_nonzero_and_chinese_output(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "bad.md"
            path.write_text("# 标题\n扫码关注公众号\n", encoding="utf-8")
            completed = subprocess.run(
                [sys.executable, str(ROOT / "scripts/validate_notes.py"), str(path)],
                check=False,
                capture_output=True,
                text=True,
            )
            self.assertNotEqual(completed.returncode, 0)
            self.assertEqual(completed.stdout, "")
            self.assertIn("未通过", completed.stderr)
            self.assertIn(str(path), completed.stderr)
            self.assertNotIn("Traceback", completed.stderr)


if __name__ == "__main__":
    unittest.main()
