---
name: build-exam-anki
description: 将考研课程的 PDF、OCR Markdown、PPTX、DOCX 或已有卡片规范化为可追踪来源，再生成纯背诵笔记、按章独立的八字段 Anki TSV/APKG、逐节覆盖校验、背诵索引和合并打印版。用于根据教材制作或升级 Anki、保证知识密度与全覆盖、检查漏点、修复卡片格式、处理 OCR 资料、迁移旧 APKG，或把课程资料沉淀为长期复习资产。
---

# 考研笔记与 Anki 生成

把任务作为“来源可追踪的背诵资产工程”执行。当前模型负责理解、提取、重构和逐页视觉检查；技能脚本只负责确定性的清洗、校验、打包、打印和审计。除用户明确关闭外，默认生成分章笔记、分章卡片、覆盖报告和合并打印版。

## 运行变量

从任意课程目录执行前，先定义技能和项目的绝对路径。后续不得依赖当前目录：

```bash
SKILL_ROOT="${CODEX_HOME:-$HOME/.codex}/skills/build-exam-anki"
PROJECT_ROOT="/绝对路径/课程项目"
python3 -m venv "$SKILL_ROOT/.venv"
"$SKILL_ROOT/.venv/bin/python" -m pip install -r "$SKILL_ROOT/requirements.txt"
```

## 硬门禁

- 原始资料只读保存，不覆盖旧笔记、旧 TSV、旧 APKG 或旧打印成品。
- 先完成来源规范化提取，再清洗；先建立来源小节清单，再写笔记和卡片。
- `build/小节清单.tsv` 表头固定为 `SectionID / Title / Source`，范围内小节不得遗漏。
- `reports/覆盖表.tsv` 表头固定为 `SectionID / Title / Status / CardIDs / Reason / Source`。
- `CardIDs` 只填项目卡片按 `Source + Front` 生成的真实 GUID；禁止填序号、Front 文本或虚构占位符。
- 未完成逐节覆盖映射，不得宣称“全覆盖”。
- 打印 PDF 必须渲染到 `build/print-qa`，当前代理必须用 `view_image` 逐页检查并写 `reports/打印视觉复核.tsv`；自动渲染不等于视觉通过。
- 所有用户可见说明、报告和错误使用中文；固定字段、格式名、代码标识符和命令保留原名。

## 第一步：确定范围并初始化

确认课程、纳入章节、排除章节、复习阶段、旧卡身份保护和打印要求。没有已确认样例时，先做一章代表性样例。读取 [输入与来源约定](references/input-contract.md)，查找真实文件，不猜路径。

```bash
"$SKILL_ROOT/.venv/bin/python" "$SKILL_ROOT/scripts/init_project.py" "$PROJECT_ROOT" \
  --course '课程名' \
  --profile '408与概念型工科' \
  --include 1,2,3 \
  --exclude 4
```

## 第二步：来源规范化提取

严格按 [输入与来源约定](references/input-contract.md) 路由，只使用当前代理实际可调用的读取、文档、演示、OCR、Pandoc、Poppler、解压和 SQLite 工具，不虚构提取脚本。

- 文本型 PDF：先用 `pdftotext` 或可用 PDF 读取工具，按页保留页码；图、表、公式和章首页同时保留对应页图。
- 扫描型 PDF：先逐页渲染，再用可用 OCR 或图像读取能力逐页提取；每页文字必须能回到页码和页图。
- PPTX/DOCX：优先用演示或文档工具；也可对 DOCX 使用 Pandoc，对 PPTX 只读解包后按幻灯片顺序提取文字、表格、备注和图片。
- 旧 APKG：只读解压 collection；用只读 SQLite 或兼容读取工具导出原字段、GUID、deck/model 身份到 `source/existing_tsv` 或来源清单，禁止直接覆盖旧包。
- 已有 OCR Markdown：直接进入 `clean_ocr.py`，原文件不改。

只有 PDF 时，必须先得到 `source/extracted` 下的可检索 Markdown，或得到 `cleaned/` 下的清洗 Markdown；没有这一步不得建立小节清单或批量制卡。

规范化后清点来源：

```bash
"$SKILL_ROOT/.venv/bin/python" "$SKILL_ROOT/scripts/inspect_sources.py" "$PROJECT_ROOT" \
  --json "$PROJECT_ROOT/build/来源清单.json" \
  --report "$PROJECT_ROOT/reports/来源清单.md"
```

`--json` 只允许写入项目 `build/`，`--report` 只允许写入项目 `reports/`。两份清单默认不覆盖且作为一个事务提交；只有明确替换旧双文件时，才在上述命令末尾添加 `--force`。

来源缺失、页码不可追踪、章节边界不明或指定章节不存在时，停止批量生成并先修复来源。

## 第三步：保守清洗

对已有 OCR Markdown 和规范化提取 Markdown 逐章处理，输出写入 `cleaned/`：

```bash
"$SKILL_ROOT/.venv/bin/python" "$SKILL_ROOT/scripts/clean_ocr.py" "$PROJECT_ROOT/source/extracted/第1章.md" \
  --output "$PROJECT_ROOT/cleaned/第1章.md" \
  --report "$PROJECT_ROOT/reports/第1章_清洗记录.md"
```

脚本只清除确定性噪声。是否保留例题、解析、推导或图示，由当前模型根据考点和覆盖价值判断，不用正则表达式代替语义判断。

## 第四步：建立来源小节清单

从可检索 Markdown 和页图抽取所有纳入范围的章、节和有效小节，建立稳定 `SectionID`，写入 `build/小节清单.tsv`：

```text
SectionID / Title / Source
```

`Source` 必须能定位到原文件和页码或幻灯片号。小节清单是独立来源基线，不得从成品卡片反推。随后读取 [覆盖校验规范](references/coverage-standard.md)。

## 第五步：生成纯背诵笔记

读取 [纯背诵笔记规范](references/note-standard.md) 和 [学科制卡配置](references/subject-profiles.md)，按章生成 `notes/第N章_主题_纯背诵版.md`。保留定义、条件、公式、性质、流程、比较、二级结论、易错边界、来源定位和 OCR 存疑标记。

```bash
"$SKILL_ROOT/.venv/bin/python" "$SKILL_ROOT/scripts/validate_notes.py" "$PROJECT_ROOT/notes/第N章_主题_纯背诵版.md"
```

修复全部错误后再制卡。警告必须人工判断，不得静默忽略。

## 第六步：设计八字段卡片

读取 [八字段卡片与制卡规范](references/card-schema.md)。字段顺序固定为：

```text
Front / Back / Extra / Mistake / Trigger / Importance / Source / Tags
```

每个 Front 对应明确主动回忆任务，Back 足以独立评分。完整比较、强关联流程和公式条件不得为追求卡片数而过度拆分。重要程度只允许 `必背`、`高频`、`易错`、`理解`、`低频补充`。

```bash
"$SKILL_ROOT/.venv/bin/python" "$SKILL_ROOT/scripts/validate_cards.py" "$PROJECT_ROOT/cards/课程_第N章_Anki.tsv"
```

存在表头、空字段、重复 Front、泛化问题、标签或公式错误时，不得生成 APKG。

## 第七步：回填覆盖表

为 `build/小节清单.tsv` 中每条记录写一条 `reports/覆盖表.tsv` 记录。状态只允许：`已制卡`、`并入上级卡`、`不适合制卡`、`OCR存疑`。

```text
SectionID / Title / Status / CardIDs / Reason / Source
```

前两种状态必须填写真实 GUID；后两种必须填写理由。`SectionID` 集合、Title 和 Source 必须与小节清单严格一致。

## 第八步：生成 APKG 和报告

逐章打包，不合并用户要求独立的牌组：

```bash
"$SKILL_ROOT/.venv/bin/python" "$SKILL_ROOT/scripts/build_apkg.py" \
  "$PROJECT_ROOT/cards/课程_第N章_Anki.tsv" \
  --output "$PROJECT_ROOT/cards/课程_第N章_Anki.apkg" \
  --deck '课程::第N章'
```

生成报告，并让脚本核验覆盖表中的真实 GUID 映射：

```bash
"$SKILL_ROOT/.venv/bin/python" "$SKILL_ROOT/scripts/build_reports.py" \
  --project "$PROJECT_ROOT" \
  --coverage "$PROJECT_ROOT/reports/覆盖表.tsv"
```

必须得到覆盖校验、卡片统计和背诵索引；存在存疑项时还要生成 OCR 存疑报告。

## 第九步：生成打印版

读取 [打印版生成与检查规范](references/print-standard.md)。`--output-dir` 必须精确为项目 `print/` 根目录；版本通过 `--title` 和成品文件名管理。默认不覆盖同名旧成品；仅在用户明确授权替换时添加 `--force`。

```bash
"$SKILL_ROOT/.venv/bin/python" "$SKILL_ROOT/scripts/build_print.py" \
  --project "$PROJECT_ROOT" \
  --title '课程名纯背诵笔记' \
  --output-dir "$PROJECT_ROOT/print"
```

全局文档标题只出现一次，每章章标题只出现一次。Markdown 首个 H1 已是文档标题，Pandoc HTML 命令不得再传同名 title metadata，也不得生成 `title-block-header`。分章笔记的重复首个 H1 必须移除，其他正文 H1 降为 H2；分页只由章 H1 控制，不叠加原始 `\newpage`。笔记中的本地图片必须在构建前核验并计入 DOCX 预期图片数；远程、缺失或无法解析的图片必须失败。

## 第十步：逐页打印视觉复核

先把最终 PDF 全页渲染到本次专用目录：

```bash
PRINT_PDF="$PROJECT_ROOT/print/课程名纯背诵笔记.pdf"
PRINT_QA_DIR="$PROJECT_ROOT/build/print-qa/本次版本"
PDF_SHA256="$(shasum -a 256 "$PRINT_PDF" | awk '{print $1}')"
mkdir -p "$PRINT_QA_DIR"
pdftoppm -png -r 160 "$PRINT_PDF" "$PRINT_QA_DIR/page"
```

当前代理必须对 `PRINT_QA_DIR` 中每一页调用 `view_image`，逐页检查断图、缺字、重叠、越界、异常空白、公式裸露和章节跳转。写入 `reports/打印视觉复核.tsv`，字段固定为：

```text
PDF_SHA256 / 页码 / 状态 / 问题
```

每个 PDF 页码必须恰有一条记录，每一页都重复填写当前 PDF 的同一 `PDF_SHA256`；有问题时先修复、重新生成并重新逐页检查。自动渲染不等于视觉通过。

## 第十一步：统一审计

视觉复核完成后才能运行：

```bash
"$SKILL_ROOT/.venv/bin/python" "$SKILL_ROOT/scripts/audit_outputs.py" --project "$PROJECT_ROOT"
```

审计必须通过来源清单、逐节覆盖、真实 GUID、笔记、八字段 TSV、APKG collection、卡片数量、DOCX 图片与公式、A4 PDF 和视觉复核记录。TSV 的校验警告同样是硬失败；APKG 必须按 `stable_guid` 逐条绑定 TSV 的 MathJax 转换后八字段、Tags、Importance、固定 model 和稳定 deck。任何门禁失败，都必须报告真实状态并修复；不得把“已生成”描述为“已验证通过”。

## 交付

只交付分章笔记、分章 TSV/APKG、覆盖与统计报告、背诵索引、存疑报告、合并 DOCX/PDF，以及实际运行的测试和剩余人工边界。不要交付临时页、缓存或测试副本。
