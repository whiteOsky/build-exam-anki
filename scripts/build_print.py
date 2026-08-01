"""按项目章节顺序生成合并打印版 Markdown、DOCX 和 PDF。"""

import os
import re
import shutil
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path
from typing import Callable, Dict, List, Mapping, Optional, Tuple, Union
from urllib.parse import unquote, urlsplit

try:
    from .common import (
        ChineseArgumentParser,
        atomic_write_text,
        ensure_safe_output_path,
        fsync_directory,
        read_json,
    )
    from .validate_notes import (
        _HtmlImageParser,
        _iter_markdown_image_targets,
        _mask_code,
        _unescape_markdown_target,
    )
except ImportError:
    from common import (
        ChineseArgumentParser,
        atomic_write_text,
        ensure_safe_output_path,
        fsync_directory,
        read_json,
    )
    from validate_notes import (
        _HtmlImageParser,
        _iter_markdown_image_targets,
        _mask_code,
        _unescape_markdown_target,
    )


PathLike = Union[str, Path]
Runner = Callable[..., subprocess.CompletedProcess]

IMAGE_SUFFIXES = {".jpeg", ".jpg", ".png", ".svg", ".webp"}
CHROME_LOCATIONS = [
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    "/Applications/Chromium.app/Contents/MacOS/Chromium",
]

PRINT_CSS = """
@page { size: A4; margin: 18mm 16mm 18mm 16mm; }
html { color: #202421; background: #ffffff; }
body {
  font-family: "PingFang SC", "Microsoft YaHei", sans-serif;
  font-size: 10.5pt;
  line-height: 1.55;
}
h1 { page-break-before: always; page-break-after: avoid; color: #173f35; }
body > h1:first-child { page-break-before: avoid; }
h2, h3 { color: #31576b; page-break-after: avoid; }
p, li { orphans: 3; widows: 3; }
img { display: block; max-width: 100%; max-height: 245mm; margin: 0 auto; }
table { width: 100%; border-collapse: collapse; }
th, td { border: 1px solid #aeb8b3; padding: 4px 6px; }
code { overflow-wrap: anywhere; }
""".strip()


def _project_root(project: PathLike) -> Path:
    root = Path(project).expanduser().resolve()
    if not root.is_dir():
        raise ValueError("项目根目录不存在：{}".format(root))
    return root


def _read_chapters(project: Path) -> List[str]:
    candidates = [project / "build/project.json", project / "project.json"]
    config_path = next((path for path in candidates if path.is_file()), None)
    if config_path is None:
        raise ValueError("缺少项目配置：build/project.json 或 project.json")
    payload = read_json(config_path)
    if not isinstance(payload, dict):
        raise ValueError("项目配置必须是 JSON 对象")
    nested = payload.get("config")
    config = nested if isinstance(nested, dict) else payload
    chapters = config.get("章节顺序")
    if chapters is None:
        chapters = config.get("纳入章节", payload.get("纳入章节"))
    if not isinstance(chapters, list) or not chapters:
        raise ValueError("项目配置缺少非空的章节顺序或纳入章节")

    result = []
    for item in chapters:
        if isinstance(item, dict):
            item = item.get("名称", item.get("标题", item.get("chapter")))
        chapter = str(item).strip() if item is not None else ""
        if not chapter:
            raise ValueError("项目配置包含空章节名称")
        result.append(chapter)
    return result


def _normalized_name(value: str) -> str:
    return re.sub(r"[\s_\-]+", "", value).casefold()


def _matches_chapter(path: Path, chapter: str) -> bool:
    stem = _normalized_name(path.stem)
    normalized = _normalized_name(chapter)
    if normalized.isdigit():
        chapter_number = str(int(normalized))
        pattern = r"第0*{}(?!\d)[章讲节]".format(re.escape(chapter_number))
        return re.search(pattern, stem) is not None
    return normalized in stem


def _find_chapter_file(
    directory: Path, chapter: str, suffixes: Mapping[str, object]
) -> Optional[Path]:
    if not directory.is_dir():
        return None
    candidates = sorted(
        (
            path
            for path in directory.iterdir()
            if path.is_file()
            and path.suffix.casefold() in suffixes
            and _matches_chapter(path, chapter)
        ),
        key=lambda path: path.name.casefold(),
    )
    if len(candidates) > 1:
        raise ValueError(
            "章节“{}”匹配到多个文件：{}".format(
                chapter, "、".join(path.name for path in candidates)
            )
        )
    return candidates[0] if candidates else None


def _knowledge_mainline(note: str) -> List[str]:
    headings = []
    for line in note.splitlines():
        match = re.match(r"^#{2,6}\s+(.+?)\s*$", line)
        if match:
            heading = re.sub(r"[*_`]+", "", match.group(1)).strip()
            if heading and heading not in headings:
                headings.append(heading)
    if not headings:
        return ["- 以本章纯背诵笔记为知识主线。"]
    return ["- {}".format(heading) for heading in headings]


def _normalize_note_headings(
    note: str,
    note_path: Path,
    chapter: str,
    document_title: str,
) -> str:
    """移除重复的分章标题，并让笔记正文不再产生一级分页。"""
    lines = note.splitlines()
    masked_lines = _mask_code(note).splitlines()
    first_h1 = next(
        (
            index
            for index, line in enumerate(masked_lines)
            if re.match(r"^#\s+.+?\s*$", line)
        ),
        None,
    )
    redundant_titles = {
        _normalized_name(chapter),
        _normalized_name(document_title),
        _normalized_name(note_path.stem),
    }

    normalized = []
    for index, line in enumerate(lines):
        masked = masked_lines[index] if index < len(masked_lines) else line
        heading = re.match(r"^#\s+(.+?)\s*$", masked)
        if not heading:
            normalized.append(line)
            continue
        title = heading.group(1).strip()
        if index == first_h1 and _normalized_name(title) in redundant_titles:
            continue
        normalized.append("## {}".format(title))
    return "\n".join(normalized).strip()


def _resolve_note_image(raw_target: str, note_path: Path) -> Path:
    target = _unescape_markdown_target(raw_target.strip())
    parsed = urlsplit(target)
    if parsed.scheme or parsed.netloc or target.startswith("//"):
        raise ValueError(
            "笔记“{}”包含无法核验的远程图片：{}".format(
                note_path.name,
                target,
            )
        )
    if parsed.query:
        raise ValueError(
            "笔记“{}”包含无法核验的图片查询参数：{}".format(
                note_path.name,
                target,
            )
        )

    local_target = unquote(parsed.path)
    if not local_target:
        raise ValueError("笔记“{}”包含空图片路径".format(note_path.name))
    image_path = Path(local_target).expanduser()
    if not image_path.is_absolute():
        image_path = note_path.parent / image_path
    image_path = image_path.resolve()
    if not image_path.is_file():
        raise ValueError(
            "笔记“{}”包含失效图片引用：{}".format(
                note_path.name,
                image_path,
            )
        )
    return image_path


def _collect_note_images(note: str, note_path: Path) -> List[Path]:
    """严格解析并核验笔记中的 Markdown 与 HTML 图片。"""
    masked = _mask_code(note)
    images = set()
    for line_number, line in enumerate(masked.splitlines(), start=1):
        targets = list(_iter_markdown_image_targets(line))
        image_markers = len(re.findall(r"(?<!\\)!\[", line))
        if image_markers != len(targets):
            raise ValueError(
                "笔记“{}”第{}行存在无法解析的图片引用".format(
                    note_path.name,
                    line_number,
                )
            )
        for target in targets:
            images.add(_resolve_note_image(target, note_path))

    parser = _HtmlImageParser()
    parser.feed(masked)
    parser.close()
    for line_number, source in parser.images:
        if not source:
            raise ValueError(
                "笔记“{}”第{}行的 HTML 图片缺少 src".format(
                    note_path.name,
                    line_number,
                )
            )
        images.add(_resolve_note_image(source, note_path))
    return sorted(images, key=lambda path: str(path).casefold())


def _count_math_expressions(text: str) -> int:
    display = re.findall(r"(?<!\\)\$\$(.+?)(?<!\\)\$\$", text, re.DOTALL)
    without_display = re.sub(
        r"(?<!\\)\$\$(.+?)(?<!\\)\$\$", "", text, flags=re.DOTALL
    )
    inline = re.findall(r"(?<!\\)\$(?!\$)(.+?)(?<!\\)\$", without_display)
    parenthesized = re.findall(r"\\\((.+?)\\\)", text, re.DOTALL)
    bracketed = re.findall(r"\\\[(.+?)\\\]", text, re.DOTALL)
    return len(display) + len(inline) + len(parenthesized) + len(bracketed)


def assemble_markdown(
    project: PathLike, title: str
) -> Tuple[str, Dict[str, object]]:
    """读取配置与分章笔记，返回合并 Markdown 和预期结构统计。"""
    root = _project_root(project)
    chapters = _read_chapters(root)
    sections = ["# {}".format(title.strip())]
    expected_image_paths = set()
    note_image_paths = set()
    formula_count = 0
    note_paths = []

    for chapter in chapters:
        note_path = _find_chapter_file(root / "notes", chapter, {".md": True})
        if note_path is None:
            raise ValueError("找不到章节“{}”对应的 Markdown 笔记".format(chapter))
        raw_note = note_path.read_text(encoding="utf-8")
        for image_path in _collect_note_images(raw_note, note_path):
            expected_image_paths.add(image_path)
            note_image_paths.add(image_path)
        note = _normalize_note_headings(raw_note, note_path, chapter, title)
        framework = _find_chapter_file(
            root / "assets/framework",
            chapter,
            {suffix: True for suffix in IMAGE_SUFFIXES},
        )

        chapter_lines = ["# {}".format(chapter), "## 原教材章首页"]
        if framework is None:
            chapter_lines.append("> 未提供对应的原教材章首页图。")
        else:
            relative = framework.relative_to(root).as_posix()
            chapter_lines.append(
                "![{} 原教材章首页](<{}>)".format(chapter, relative)
            )
            expected_image_paths.add(framework.resolve())
        chapter_lines.append("## 本章知识主线")
        chapter_lines.extend(_knowledge_mainline(note))
        chapter_lines.append(note.strip())
        sections.append("\n\n".join(chapter_lines))
        formula_count += _count_math_expressions(note)
        note_paths.append(note_path)

    markdown = "\n\n".join(sections).rstrip() + "\n"
    return markdown, {
        "章节数": len(chapters),
        "预期图片数": len(expected_image_paths),
        "预期公式数": formula_count,
        "笔记": note_paths,
        "笔记图片": sorted(note_image_paths, key=lambda path: str(path).casefold()),
    }


def _safe_filename(title: str) -> str:
    filename = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", title).strip(" .")
    if not filename:
        raise ValueError("打印版标题不能生成有效文件名")
    return filename


def _prepare_output_directory(project: Path, output_dir: PathLike) -> Path:
    project = project.expanduser().resolve()
    print_root = project / "print"
    if print_root.is_symlink():
        raise ValueError(
            "输出目录只允许固定 print/ 目录，路径包含符号链接：{}".format(
                print_root
            )
        )
    requested = Path(output_dir).expanduser()
    destination = requested if requested.is_absolute() else project / requested
    destination = Path(os.path.abspath(str(destination))).resolve(strict=False)
    if destination != print_root.resolve(strict=False):
        raise ValueError(
            "输出目录必须精确为项目固定 print/ 根目录：{}".format(
                destination
            )
        )
    ensure_safe_output_path(project, destination)
    destination.mkdir(parents=True, exist_ok=True)
    ensure_safe_output_path(project, destination)
    return destination.resolve()


def _detect_tools(tool_paths: Optional[Mapping[str, str]]) -> Dict[str, str]:
    if tool_paths is not None:
        tools = dict(tool_paths)
    else:
        tools = {"pandoc": shutil.which("pandoc") or ""}
        chrome = (
            shutil.which("google-chrome")
            or shutil.which("chromium")
            or shutil.which("chromium-browser")
        )
        if not chrome:
            chrome = next(
                (path for path in CHROME_LOCATIONS if Path(path).is_file()), ""
            )
        tools["chrome"] = chrome
    if not tools.get("pandoc"):
        raise RuntimeError("未找到 Pandoc。请安装后重试：brew install pandoc")
    if not tools.get("chrome"):
        raise RuntimeError(
            "未找到 Chrome 或 Chromium。请安装 Google Chrome，"
            "或运行：brew install --cask google-chrome"
        )
    return tools


def _run_checked(
    command: List[str], label: str, runner: Runner
) -> subprocess.CompletedProcess:
    try:
        completed = runner(command, check=False, capture_output=True, text=True)
    except OSError as error:
        raise RuntimeError("{}失败：{}".format(label, error))
    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout or "未提供错误详情").strip()
        raise RuntimeError("{}失败：{}".format(label, detail))
    return completed


def _validate_generated_docx(path: Path) -> None:
    try:
        with zipfile.ZipFile(path) as archive:
            if archive.testzip() is not None:
                raise ValueError("DOCX 压缩数据损坏")
            if "word/document.xml" not in archive.namelist():
                raise ValueError("DOCX 缺少 word/document.xml")
    except (OSError, zipfile.BadZipFile) as error:
        raise ValueError("DOCX 不是有效 ZIP 文件：{}".format(error))


def _validate_generated_files(markdown: Path, docx: Path, pdf: Path) -> None:
    if not markdown.is_file() or markdown.stat().st_size == 0:
        raise ValueError("合并 Markdown 未成功生成")
    _validate_generated_docx(docx)
    try:
        signature = pdf.read_bytes()[:5]
    except OSError as error:
        raise ValueError("PDF 读取失败：{}".format(error))
    if signature != b"%PDF-":
        raise ValueError("Chrome 未生成有效 PDF")


def _promote_files(
    pairs: List[Tuple[Path, Path]], output_root: Path, force: bool = False
) -> None:
    for _, destination in pairs:
        ensure_safe_output_path(output_root, destination)
    if not force:
        existing = [
            destination
            for _, destination in pairs
            if destination.exists() or destination.is_symlink()
        ]
        if existing:
            raise FileExistsError(
                "打印成品已存在，默认不覆盖；确认替换时显式使用 --force：{}".format(
                    "、".join(path.name for path in existing)
                )
            )

    backups = []
    promoted = []
    try:
        for temporary, destination in pairs:
            ensure_safe_output_path(output_root, destination)
            backup = None
            if force and destination.exists():
                backup = temporary.parent / (destination.name + ".backup")
                shutil.copy2(str(destination), str(backup))
            backups.append((destination, backup))
            if force:
                os.replace(str(temporary), str(destination))
                promoted.append(destination)
            else:
                os.link(str(temporary), str(destination))
                promoted.append(destination)
                temporary.unlink()
        fsync_directory(output_root)
    except OSError:
        for destination, backup in reversed(backups):
            if backup is not None and backup.exists():
                os.replace(str(backup), str(destination))
            elif destination in promoted:
                try:
                    destination.unlink()
                except FileNotFoundError:
                    pass
        raise


def build_print(
    project: PathLike,
    title: str,
    output_dir: PathLike,
    tool_paths: Optional[Mapping[str, str]] = None,
    runner: Runner = subprocess.run,
    force: bool = False,
) -> Dict[str, object]:
    """构建三份打印成品；任一外部工具失败时不提升临时文件。"""
    root = _project_root(project)
    markdown, details = assemble_markdown(root, title)
    output_root = _prepare_output_directory(root, output_dir)
    tools = _detect_tools(tool_paths)
    basename = _safe_filename(title)

    destinations = {
        "Markdown": output_root / (basename + ".md"),
        "DOCX": output_root / (basename + ".docx"),
        "PDF": output_root / (basename + ".pdf"),
    }
    for destination in destinations.values():
        ensure_safe_output_path(output_root, destination)

    with tempfile.TemporaryDirectory(prefix=".print-build-", dir=str(output_root)) as name:
        temporary = Path(name)
        markdown_path = temporary / (basename + ".md")
        docx_path = temporary / (basename + ".docx")
        html_path = temporary / (basename + ".html")
        css_path = temporary / "print.css"
        pdf_path = temporary / (basename + ".pdf")
        atomic_write_text(markdown_path, markdown, root=output_root)
        atomic_write_text(css_path, PRINT_CSS + "\n", root=output_root)

        markdown_reader = "markdown+tex_math_dollars+tex_math_single_backslash"
        resource_path = os.pathsep.join([str(root), str(root / "notes")])
        _run_checked(
            [
                tools["pandoc"],
                str(markdown_path),
                "--from={}".format(markdown_reader),
                "--to=docx",
                "--resource-path={}".format(resource_path),
                "--wrap=none",
                "-o",
                str(docx_path),
            ],
            "Pandoc 生成 DOCX ",
            runner,
        )
        _run_checked(
            [
                tools["pandoc"],
                str(markdown_path),
                "--from={}".format(markdown_reader),
                "--to=html5",
                "--standalone",
                "--embed-resources",
                "--mathml",
                "--resource-path={}".format(resource_path),
                "--css={}".format(css_path),
                "-o",
                str(html_path),
            ],
            "Pandoc 生成嵌入资源 HTML ",
            runner,
        )
        _run_checked(
            [
                tools["chrome"],
                "--headless",
                "--disable-gpu",
                "--no-pdf-header-footer",
                "--print-to-pdf={}".format(pdf_path),
                html_path.as_uri(),
            ],
            "Chrome 生成 PDF ",
            runner,
        )
        _validate_generated_files(markdown_path, docx_path, pdf_path)
        _promote_files(
            [
                (markdown_path, destinations["Markdown"]),
                (docx_path, destinations["DOCX"]),
                (pdf_path, destinations["PDF"]),
            ],
            output_root,
            force=force,
        )

    result = dict(details)
    result["输出"] = destinations
    return result


def main() -> int:
    parser = ChineseArgumentParser(description="生成合并打印版 Markdown、DOCX 和 PDF")
    parser.add_argument("--project", required=True, help="课程项目根目录")
    parser.add_argument("--title", required=True, help="打印版标题和文件名")
    parser.add_argument(
        "--output-dir", required=True, help="必须精确指定项目 print/ 根目录"
    )
    parser.add_argument(
        "--force", action="store_true", help="显式覆盖 print/ 中的同名旧成品"
    )
    arguments = parser.parse_args()
    try:
        result = build_print(
            arguments.project,
            arguments.title,
            arguments.output_dir,
            force=arguments.force,
        )
    except (OSError, UnicodeError, ValueError, RuntimeError) as error:
        print("打印版生成失败：{}".format(error), file=sys.stderr)
        return 1
    print(
        "打印版生成完成：{}章，Markdown、DOCX、PDF 各1份。".format(
            result["章节数"]
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
