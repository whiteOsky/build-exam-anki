import csv
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path
from urllib.parse import unquote, urlsplit
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from scripts import inspect_sources as inspect_sources_module
from scripts.build_apkg import build_package, stable_guid
from scripts.common import CARD_FIELDS, to_anki_mathjax


DECK_NAME = "操作系统::第2章"
COVERAGE_FIELDS = [
    "SectionID",
    "Title",
    "Status",
    "CardIDs",
    "Reason",
    "Source",
]
SECTION_FIELDS = ["SectionID", "Title", "Source"]


def write_tsv(path, fields, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, delimiter="\t")
        writer.writeheader()
        writer.writerows(rows)


def valid_card(front="进程与程序的区别是什么?"):
    return {
        "Front": front,
        "Back": r"进程是动态执行过程，周转时间为 $T=t_c-t_a$。",
        "Extra": "程序是静态代码。",
        "Mistake": "不要把程序视为运行实体。",
        "Trigger": "看到动态性与并发性时。",
        "Importance": "必背",
        "Source": "操作系统|第2章 进程管理",
        "Tags": "408::操作系统::第2章::必背",
    }


def prepare_report_project(project, front):
    row = valid_card(front)
    write_tsv(project / "cards/第2章.tsv", CARD_FIELDS, [row])
    section = {"SectionID": "2.1", "Title": "进程", "Source": "教材第20页"}
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


def mutate_apkg(path, mutation):
    with tempfile.TemporaryDirectory() as directory:
        temporary = Path(directory)
        with zipfile.ZipFile(path) as archive:
            members = {name: archive.read(name) for name in archive.namelist()}
        collection_name = next(
            name
            for name in ["collection.anki21", "collection.anki2"]
            if name in members
        )
        database = temporary / "collection.sqlite3"
        database.write_bytes(members[collection_name])
        connection = sqlite3.connect(str(database))
        try:
            mutation(connection)
            connection.commit()
        finally:
            connection.close()
        members[collection_name] = database.read_bytes()
        replacement = temporary / "replacement.apkg"
        with zipfile.ZipFile(replacement, "w") as archive:
            for name, data in members.items():
                archive.writestr(name, data)
        os.replace(str(replacement), str(path))


def run_apkg_audit(project):
    from scripts.audit_outputs import _audit_apkg

    checks = []
    with tempfile.TemporaryDirectory(dir=str(project)) as directory:
        result = _audit_apkg(project, Path(directory), checks)
    return result, checks[-1]


class SourceOutputContractTests(unittest.TestCase):
    def run_cli(self, project, json_path, report_path, force=False):
        command = [
            sys.executable,
            str(ROOT / "scripts/inspect_sources.py"),
            str(project),
            "--json",
            str(json_path),
            "--report",
            str(report_path),
        ]
        if force:
            command.append("--force")
        return subprocess.run(command, check=False, capture_output=True, text=True)

    def test_cli_limits_each_output_to_its_fixed_tree(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            project = base / "项目"
            project.mkdir()
            (project / "source.pdf").write_bytes(b"pdf")
            cases = [
                (project / "source/清单.json", project / "reports/清单.md"),
                (project / "build/清单.json", project / "notes/清单.md"),
                (project / "reports/清单.json", project / "reports/清单.md"),
                (project / "build/清单.json", project / "build/清单.md"),
                (base / "外部.json", project / "reports/清单.md"),
            ]
            for json_path, report_path in cases:
                with self.subTest(json_path=json_path, report_path=report_path):
                    completed = self.run_cli(project, json_path, report_path)
                    self.assertNotEqual(completed.returncode, 0)
                    self.assertIn("来源检查失败", completed.stderr)

    def test_cli_defaults_to_no_overwrite_and_force_replaces_both(self):
        with tempfile.TemporaryDirectory() as directory:
            project = Path(directory)
            source = project / "第1章.pdf"
            source.write_bytes(b"old")
            json_path = project / "build/来源清单.json"
            report_path = project / "reports/来源清单.md"

            first = self.run_cli(project, json_path, report_path)
            self.assertEqual(first.returncode, 0, first.stderr)
            old_json = json_path.read_bytes()
            old_report = report_path.read_bytes()
            source.write_bytes(b"new-content")

            refused = self.run_cli(project, json_path, report_path)
            self.assertNotEqual(refused.returncode, 0)
            self.assertIn("--force", refused.stderr)
            self.assertEqual(json_path.read_bytes(), old_json)
            self.assertEqual(report_path.read_bytes(), old_report)

            forced = self.run_cli(project, json_path, report_path, force=True)
            self.assertEqual(forced.returncode, 0, forced.stderr)
            self.assertNotEqual(json_path.read_bytes(), old_json)
            self.assertNotEqual(report_path.read_bytes(), old_report)

    def test_outputs_reject_symbolic_and_hard_links_to_source(self):
        for link_kind in ["symbolic", "hard"]:
            with self.subTest(link_kind=link_kind), tempfile.TemporaryDirectory() as directory:
                project = Path(directory)
                source = project / "原始讲义.pdf"
                source.write_bytes(b"immutable-source")
                (project / "build").mkdir()
                json_path = project / "build/来源清单.json"
                report_path = project / "reports/来源清单.md"
                if link_kind == "symbolic":
                    json_path.symlink_to(source)
                else:
                    os.link(str(source), str(json_path))

                completed = self.run_cli(
                    project, json_path, report_path, force=True
                )

                self.assertNotEqual(completed.returncode, 0)
                self.assertRegex(completed.stderr, "符号链接|硬链接|来源")
                self.assertEqual(source.read_bytes(), b"immutable-source")
                self.assertFalse(report_path.exists())

    def test_force_transaction_failure_restores_both_old_files(self):
        writer = getattr(inspect_sources_module, "write_source_outputs", None)
        self.assertIsNotNone(writer, "inspect_sources 缺少双文件事务写入入口")
        with tempfile.TemporaryDirectory() as directory:
            project = Path(directory)
            (project / "第1章.pdf").write_bytes(b"pdf")
            json_path = project / "build/来源清单.json"
            report_path = project / "reports/来源清单.md"
            json_path.parent.mkdir()
            report_path.parent.mkdir()
            json_path.write_bytes(b"old-json")
            report_path.write_bytes(b"old-report")
            inventory = inspect_sources_module.inspect_sources(project)
            real_replace = os.replace
            failed = {"value": False}

            def fail_report_promotion(source, destination):
                if (
                    not failed["value"]
                    and Path(destination) == report_path.resolve()
                    and ".tmp-" in Path(source).name
                ):
                    failed["value"] = True
                    raise OSError("模拟报告提交失败")
                return real_replace(source, destination)

            with mock.patch(
                "scripts.inspect_sources.os.replace",
                side_effect=fail_report_promotion,
            ):
                with self.assertRaisesRegex(OSError, "报告提交失败"):
                    writer(
                        project,
                        inventory,
                        json_path,
                        report_path,
                        force=True,
                    )

            self.assertEqual(json_path.read_bytes(), b"old-json")
            self.assertEqual(report_path.read_bytes(), b"old-report")

    def test_default_transaction_failure_restores_empty_state(self):
        writer = getattr(inspect_sources_module, "write_source_outputs", None)
        self.assertIsNotNone(writer, "inspect_sources 缺少双文件事务写入入口")
        with tempfile.TemporaryDirectory() as directory:
            project = Path(directory)
            (project / "第1章.pdf").write_bytes(b"pdf")
            json_path = project / "build/来源清单.json"
            report_path = project / "reports/来源清单.md"
            inventory = inspect_sources_module.inspect_sources(project)
            real_link = os.link

            def fail_report_promotion(source, destination):
                if Path(destination) == report_path.resolve():
                    raise OSError("模拟报告提交失败")
                return real_link(source, destination)

            with mock.patch(
                "scripts.inspect_sources.os.link",
                side_effect=fail_report_promotion,
            ):
                with self.assertRaisesRegex(OSError, "报告提交失败"):
                    writer(project, inventory, json_path, report_path)

            self.assertFalse(json_path.exists())
            self.assertFalse(report_path.exists())


class ApkgFieldBindingTests(unittest.TestCase):
    def test_audit_binds_all_fields_tags_model_and_deck_to_tsv(self):
        mutations = {
            "Back": lambda connection: connection.execute(
                "UPDATE notes SET flds = replace(flds, ?, ?)",
                (
                    to_anki_mathjax(valid_card()["Back"]),
                    "被篡改的答案",
                ),
            ),
            "Tags": lambda connection: connection.execute(
                "UPDATE notes SET tags = ' 篡改::标签 必背 '"
            ),
            "model name": self._tamper_model_name,
            "model id": self._tamper_model_id,
            "deck name": self._tamper_deck_name,
            "deck id": self._tamper_deck_id,
        }
        for label, mutation in mutations.items():
            with self.subTest(label=label), tempfile.TemporaryDirectory() as directory:
                project = Path(directory)
                tsv = project / "cards/第2章.tsv"
                apkg = project / "cards/第2章.apkg"
                write_tsv(tsv, CARD_FIELDS, [valid_card()])
                build_package(tsv, apkg, DECK_NAME)
                _, clean_check = run_apkg_audit(project)
                self.assertTrue(clean_check["通过"], clean_check)

                mutate_apkg(apkg, mutation)
                _, check = run_apkg_audit(project)

                self.assertFalse(check["通过"], check)
                self.assertIn(label.split()[0], check["说明"])

    @staticmethod
    def _tamper_model_name(connection):
        models_text = connection.execute("SELECT models FROM col").fetchone()[0]
        models = json.loads(models_text)
        next(iter(models.values()))["name"] = "篡改模型"
        connection.execute(
            "UPDATE col SET models = ?", (json.dumps(models, ensure_ascii=False),)
        )

    @staticmethod
    def _tamper_model_id(connection):
        models_text = connection.execute("SELECT models FROM col").fetchone()[0]
        models = json.loads(models_text)
        old_key = next(iter(models))
        model = models.pop(old_key)
        new_id = int(old_key) + 77
        model["id"] = new_id
        models[str(new_id)] = model
        connection.execute(
            "UPDATE col SET models = ?", (json.dumps(models, ensure_ascii=False),)
        )
        connection.execute("UPDATE notes SET mid = ?", (new_id,))

    @staticmethod
    def _tamper_deck_name(connection):
        decks_text = connection.execute("SELECT decks FROM col").fetchone()[0]
        decks = json.loads(decks_text)
        deck_id = connection.execute("SELECT did FROM cards LIMIT 1").fetchone()[0]
        decks[str(deck_id)]["name"] = "操作系统::篡改章"
        connection.execute(
            "UPDATE col SET decks = ?", (json.dumps(decks, ensure_ascii=False),)
        )

    @staticmethod
    def _tamper_deck_id(connection):
        decks_text = connection.execute("SELECT decks FROM col").fetchone()[0]
        decks = json.loads(decks_text)
        old_key = str(
            connection.execute("SELECT did FROM cards LIMIT 1").fetchone()[0]
        )
        deck = decks.pop(old_key)
        new_id = int(old_key) + 99
        deck["id"] = new_id
        decks[str(new_id)] = deck
        connection.execute(
            "UPDATE col SET decks = ?", (json.dumps(decks, ensure_ascii=False),)
        )
        connection.execute("UPDATE cards SET did = ?", (new_id,))


class PrintAndWarningGateTests(unittest.TestCase):
    def test_real_pandoc_html_has_no_duplicate_title_block(self):
        from scripts.build_print import build_print

        pandoc = shutil.which("pandoc")
        self.assertIsNotNone(pandoc, "测试环境缺少 Pandoc")
        with tempfile.TemporaryDirectory() as directory:
            project = Path(directory)
            (project / "build").mkdir()
            (project / "notes").mkdir()
            (project / "build/project.json").write_text(
                '{"章节顺序":["第1章"]}', encoding="utf-8"
            )
            (project / "notes/第1章.md").write_text(
                "# 第1章\n\n## 极限\n\n极限定义。\n", encoding="utf-8"
            )
            commands = []
            observed_html = {}

            def runner(command, **kwargs):
                commands.append(command)
                if command[0] == pandoc:
                    return subprocess.run(command, **kwargs)
                html_path = Path(unquote(urlsplit(command[-1]).path))
                observed_html["text"] = html_path.read_text(encoding="utf-8")
                pdf_path = next(
                    Path(item.split("=", 1)[1])
                    for item in command
                    if item.startswith("--print-to-pdf=")
                )
                pdf_path.write_bytes(b"%PDF-1.4\n")
                return subprocess.CompletedProcess(command, 0, "", "")

            build_print(
                project,
                "高数纯背诵笔记",
                project / "print",
                tool_paths={"pandoc": pandoc, "chrome": "fake-chrome"},
                runner=runner,
            )

            html_command = next(command for command in commands if "--to=html5" in command)
            self.assertFalse(
                any(item.startswith("--metadata=title:") for item in html_command)
            )
            html = observed_html["text"]
            self.assertNotIn("title-block-header", html)
            self.assertEqual(html.count(">高数纯背诵笔记</h1>"), 1)
            self.assertEqual(html.count(">第1章</h1>"), 1)

    def test_build_reports_rejects_generic_front_warning(self):
        from scripts.build_reports import build_reports

        with tempfile.TemporaryDirectory() as directory:
            project = Path(directory)
            coverage = prepare_report_project(project, "本节第1个知识点是什么")

            with self.assertRaisesRegex(ValueError, "警告|泛化"):
                build_reports(project, coverage)

    def test_audit_tsv_rejects_generic_front_warning(self):
        from scripts.audit_outputs import _audit_tsv

        with tempfile.TemporaryDirectory() as directory:
            project = Path(directory)
            write_tsv(
                project / "cards/迁移包.tsv",
                CARD_FIELDS,
                [valid_card("本节第1个知识点是什么")],
            )
            checks = []

            _audit_tsv(project, checks)

            self.assertFalse(checks[-1]["通过"])
            self.assertRegex(checks[-1]["说明"], "警告|泛化")


class DocumentationContractTests(unittest.TestCase):
    def test_final_contracts_are_documented_exactly(self):
        skill = (ROOT / "SKILL.md").read_text(encoding="utf-8")
        print_standard = (ROOT / "references/print-standard.md").read_text(
            encoding="utf-8"
        )
        coverage_standard = (ROOT / "references/coverage-standard.md").read_text(
            encoding="utf-8"
        )
        combined = skill + "\n" + print_standard

        self.assertIn("PDF_SHA256 / 页码 / 状态 / 问题", combined)
        self.assertRegex(combined, r"每一页.*同一.*SHA-256|同一.*SHA-256.*每一页")
        self.assertIn("精确为项目 `print/` 根目录", combined)
        self.assertNotIn("新建子目录", combined)
        self.assertIn("stable_guid(Source,Front)", coverage_standard)
        self.assertRegex(coverage_standard, r"不允许.*Front|禁止.*Front")
        self.assertIn("--force", skill)


if __name__ == "__main__":
    unittest.main()
