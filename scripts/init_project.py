"""初始化考试制卡项目目录。"""

import json
import sys
from pathlib import Path
from typing import Dict, List, Union

try:
    from .common import ChineseArgumentParser, ensure_safe_output_path, write_json
except ImportError:
    from common import ChineseArgumentParser, ensure_safe_output_path, write_json


PathLike = Union[str, Path]

PROJECT_DIRECTORIES = [
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


def initialize_project(
    root: PathLike,
    course: str,
    subject_profile: str,
    included_chapters: List[str],
    excluded_chapters: List[str],
) -> Dict[str, object]:
    """创建项目骨架并写入项目元数据，不移动任何已有来源。"""
    project_root = Path(root).expanduser().resolve()
    project_root.mkdir(parents=True, exist_ok=True)

    planned_paths = [project_root / item for item in PROJECT_DIRECTORIES]
    planned_paths.append(project_root / "build/project.json")
    for path in planned_paths:
        ensure_safe_output_path(project_root, path)

    for relative_path in PROJECT_DIRECTORIES:
        directory = project_root / relative_path
        directory.mkdir(parents=True, exist_ok=True)
        ensure_safe_output_path(project_root, directory)

    metadata = {
        "根目录": str(project_root),
        "课程": course,
        "科目画像": subject_profile,
        "纳入章节": list(included_chapters),
        "排除章节": list(excluded_chapters),
    }
    write_json(
        project_root / "build/project.json",
        metadata,
        root=project_root,
    )
    return metadata


def _parse_chapters(value: str) -> List[str]:
    return [item.strip() for item in value.split(",") if item.strip()]


def main() -> int:
    parser = ChineseArgumentParser(description="初始化考试制卡项目目录")
    parser.add_argument("目标目录", help="项目根目录")
    parser.add_argument("--course", required=True, help="课程名称")
    parser.add_argument("--profile", required=True, help="科目特点说明")
    parser.add_argument("--include", default="", help="逗号分隔的纳入章节")
    parser.add_argument("--exclude", default="", help="逗号分隔的排除章节")
    arguments = parser.parse_args()

    try:
        metadata = initialize_project(
            getattr(arguments, "目标目录"),
            arguments.course,
            arguments.profile,
            _parse_chapters(arguments.include),
            _parse_chapters(arguments.exclude),
        )
    except (OSError, ValueError) as error:
        print("项目初始化失败：{}".format(error), file=sys.stderr)
        return 1
    print(json.dumps(metadata, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
