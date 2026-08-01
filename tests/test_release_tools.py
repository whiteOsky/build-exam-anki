import importlib.util
import subprocess
import sys
import tempfile
import unittest
import zipfile
import os
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def load_tool(name):
    path = ROOT / "tools" / (name + ".py")
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class ReleaseToolTests(unittest.TestCase):
    def test_readme_official_installer_uses_explicit_root_name(self):
        readme = (ROOT / "README.md").read_text(encoding="utf-8")
        self.assertIn("--path .", readme)
        self.assertIn("--name build-exam-anki", readme)

    def test_readme_uses_project_relative_coverage_path(self):
        readme = (ROOT / "README.md").read_text(encoding="utf-8")
        self.assertIn("--coverage reports/覆盖表.tsv", readme)
        self.assertNotIn(
            "--coverage examples/minimal-project/reports/覆盖表.tsv", readme
        )

    def test_public_repository_check_passes(self):
        check_public = load_tool("check_public")
        self.assertEqual(check_public.check_repository(ROOT), [])

    def test_installer_copies_only_runtime_skill(self):
        with tempfile.TemporaryDirectory() as temporary_name:
            target = Path(temporary_name) / "skills" / "build-exam-anki"
            result = subprocess.run(
                [
                    sys.executable,
                    str(ROOT / "tools/install.py"),
                    "--target",
                    str(target),
                    "--skip-deps",
                ],
                text=True,
                capture_output=True,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertTrue((target / "SKILL.md").is_file())
            self.assertTrue((target / "LICENSE").is_file())
            self.assertTrue((target / "scripts/build_apkg.py").is_file())
            self.assertFalse((target / "README.md").exists())
            self.assertFalse((target / "tests").exists())
            self.assertFalse((target / "tools").exists())

            second = subprocess.run(
                [
                    sys.executable,
                    str(ROOT / "tools/install.py"),
                    "--target",
                    str(target),
                    "--skip-deps",
                ],
                text=True,
                capture_output=True,
            )
            self.assertNotEqual(second.returncode, 0)
            self.assertIn("--force", second.stderr)

    def test_installer_force_rejects_unrelated_directory(self):
        with tempfile.TemporaryDirectory() as temporary_name:
            target = Path(temporary_name) / "skills" / "build-exam-anki"
            target.mkdir(parents=True)
            marker = target / "keep.txt"
            marker.write_text("用户目录", encoding="utf-8")
            result = subprocess.run(
                [
                    sys.executable,
                    str(ROOT / "tools/install.py"),
                    "--target",
                    str(target),
                    "--skip-deps",
                    "--force",
                ],
                text=True,
                capture_output=True,
            )
            self.assertNotEqual(result.returncode, 0)
            self.assertTrue(marker.is_file())

    @unittest.skipUnless(hasattr(os, "symlink"), "当前平台不支持符号链接")
    def test_installer_and_package_reject_runtime_symlink(self):
        install_tool = load_tool("install")
        package_tool = load_tool("package_release")
        with tempfile.TemporaryDirectory() as temporary_name:
            source = Path(temporary_name) / "source"
            source.mkdir()
            for name in ("SKILL.md", "requirements.txt", "LICENSE"):
                (source / name).write_text("name: build-exam-anki\n", encoding="utf-8")
            for name in ("agents", "assets", "references", "scripts"):
                (source / name).mkdir()
            outside = Path(temporary_name) / "outside.txt"
            outside.write_text("不应进入发布包", encoding="utf-8")
            (source / "scripts/leak.py").symlink_to(outside)

            with self.assertRaisesRegex(ValueError, "符号链接"):
                install_tool.validate_runtime_source(source)
            with self.assertRaisesRegex(ValueError, "符号链接"):
                list(package_tool.runtime_paths(source))

    def test_runtime_dependencies_are_exactly_pinned(self):
        requirements = (ROOT / "requirements.txt").read_text(encoding="utf-8")
        lines = [line.strip() for line in requirements.splitlines() if line.strip()]
        self.assertTrue(lines)
        self.assertTrue(all("==" in line for line in lines))

    def test_release_zip_contains_only_runtime_skill(self):
        package_release = load_tool("package_release")
        with tempfile.TemporaryDirectory() as temporary_name:
            output = Path(temporary_name) / "release.zip"
            package_release.build_package(ROOT, output, force=False)
            with zipfile.ZipFile(str(output)) as archive:
                names = set(archive.namelist())
            self.assertIn("build-exam-anki/SKILL.md", names)
            self.assertIn("build-exam-anki/LICENSE", names)
            self.assertIn("build-exam-anki/scripts/build_apkg.py", names)
            self.assertNotIn("build-exam-anki/README.md", names)
            self.assertFalse(any("tests/" in name for name in names))
            self.assertFalse(any(".venv/" in name for name in names))


if __name__ == "__main__":
    unittest.main()
