# 输入与来源约定

## 处理前确认

- 明确课程名称、纳入章节、排除章节和当前阶段。
- 明确旧笔记、旧 TSV、旧 APKG 是否存在，以及是否必须保留旧卡 GUID、deck/model 身份。
- 先查找真实文件和目录，再设置绝对路径；不根据类似项目猜文件名。
- 原始资料保持只读。规范化结果写入 `source/extracted/` 或 `source/existing_tsv/`，清洗结果写入 `cleaned/`。

## 来源规范化提取总门禁

任何来源都必须先转成可检索、可按页或按幻灯片定位的 Markdown，再建立 `build/小节清单.tsv`。文字、表格、公式和图片之间的对应关系必须保留；无法定位的内容标记为存疑，不得伪造页码。

推荐统一设置：

```bash
PROJECT_ROOT="/绝对路径/课程项目"
mkdir -p "$PROJECT_ROOT/source/extracted"
mkdir -p "$PROJECT_ROOT/source/existing_tsv"
```

## PDF 路由

先用 `pdfinfo`、`pdftotext` 或当前可用的 PDF 读取工具判断每页是否有可靠文本层。混合型 PDF 按页选择文本提取或 OCR，不按整本文件粗暴二选一。

### 文本型 PDF

```bash
PDF_SOURCE="$PROJECT_ROOT/source/教材.pdf"
PDF_EXTRACT="$PROJECT_ROOT/source/extracted/教材"
mkdir -p "$PDF_EXTRACT/pages"
pdfinfo "$PDF_SOURCE" > "$PDF_EXTRACT/页信息.txt"
pdftotext -layout "$PDF_SOURCE" "$PDF_EXTRACT/按页文本.txt"
pdftoppm -png -r 160 "$PDF_SOURCE" "$PDF_EXTRACT/pages/page"
```

`pdftotext` 的换页符是分页边界。当前代理按页读取文字，写成带 `来源页码` 标记的可检索 Markdown；同时保留每页图像，用来核对公式、表格、插图、章首页和乱码。提取结果出现大量空白、乱序或乱码时，相关页改走扫描型 PDF 路由。

### 扫描型 PDF

```bash
PDF_SOURCE="$PROJECT_ROOT/source/扫描教材.pdf"
PDF_EXTRACT="$PROJECT_ROOT/source/extracted/扫描教材"
mkdir -p "$PDF_EXTRACT/pages"
pdfinfo "$PDF_SOURCE" > "$PDF_EXTRACT/页信息.txt"
pdftoppm -png -r 200 "$PDF_SOURCE" "$PDF_EXTRACT/pages/page"
```

对每一页调用当前可用的 OCR、PDF 图像读取能力或 `view_image`，逐页提取正文、标题、表格和公式。输出 Markdown 必须保留 PDF 页码，并引用对应页图；低置信度文字和无法核实的公式标记为 `OCR存疑`。不得只保留 OCR 文字而丢弃页图。

### 只有 PDF 时

执行顺序固定：

1. 在 `source/extracted/` 产出按页、可检索 Markdown，或进一步在 `cleaned/` 产出清洗 Markdown。
2. 用页图复核章节边界、公式、表格和 OCR 存疑项。
3. 确认纳入范围每个小节都能定位到原 PDF 页码。
4. 最后才建立 `build/小节清单.tsv`。

未完成第 1 步时，不得进入小节清单、全书笔记或批量制卡。

## 已有 OCR Markdown 路由

已有 OCR Markdown 不重复做 OCR，原文件只读保存，直接交给 `clean_ocr.py` 写入 `cleaned/`。清洗记录必须说明删除的确定性噪声；图片、页码、公式和存疑标记不得在清洗中丢失。

## DOCX 路由

优先调用当前可用的文档读取工具，以结构化方式提取标题、段落、表格、脚注和图片。工具不可用时，可用 Pandoc 提取 Markdown 和媒体：

```bash
DOCX_SOURCE="$PROJECT_ROOT/source/讲义.docx"
DOCX_EXTRACT="$PROJECT_ROOT/source/extracted/讲义"
mkdir -p "$DOCX_EXTRACT"
pandoc "$DOCX_SOURCE" -t gfm --extract-media="$DOCX_EXTRACT/media" \
  -o "$DOCX_EXTRACT/讲义.md"
```

当前代理核对表格顺序、图片位置和标题层级，补充原文页码或段落定位。Pandoc 转换成功不代表图片和公式已经核验。

## PPTX 路由

优先调用当前可用的演示文稿读取工具，按幻灯片顺序提取标题、正文、表格、备注和图片。工具不可用时，只读解包 PPTX：

```bash
PPTX_SOURCE="$PROJECT_ROOT/source/课件.pptx"
PPTX_EXTRACT="$PROJECT_ROOT/source/extracted/课件"
mkdir -p "$PPTX_EXTRACT/unpacked"
unzip -q "$PPTX_SOURCE" -d "$PPTX_EXTRACT/unpacked"
```

当前代理按 `ppt/slides/slideN.xml`、对应关系文件、备注和 `ppt/media/` 解析，不直接拼接 XML 字符串。规范化 Markdown 必须保留幻灯片号，并把表格和图片映射回对应页。无法恢复顺序或关系时停止并报告。

## 旧 APKG 路由

旧 APKG 只读解压，不把新卡直接写回旧包。先查看成员并定位 `collection.anki2`、`collection.anki21` 或其他 collection 变体：

```bash
APKG_SOURCE="$PROJECT_ROOT/source/旧牌组.apkg"
APKG_EXTRACT="$PROJECT_ROOT/source/existing_tsv/旧牌组"
APKG_WORK="$(mktemp -d)"
mkdir -p "$APKG_EXTRACT"
unzip -l "$APKG_SOURCE" > "$APKG_EXTRACT/压缩成员清单.txt"
unzip -q "$APKG_SOURCE" -d "$APKG_WORK"
```

collection 是 SQLite 时，只读导出原始笔记、卡片和身份数据：

```bash
COLLECTION_PATH="$APKG_WORK/collection.anki2"
sqlite3 -readonly -header -tabs "$COLLECTION_PATH" \
  'SELECT n.id AS note_id,n.guid,n.mid,c.id AS card_id,c.did,n.flds,n.tags FROM notes n LEFT JOIN cards c ON c.nid=n.id ORDER BY n.id,c.id;' \
  > "$APKG_EXTRACT/原始笔记卡片.tsv"
sqlite3 -readonly -header -tabs "$COLLECTION_PATH" \
  'SELECT models,decks FROM col;' \
  > "$APKG_EXTRACT/deck_model身份.tsv"
```

当前代理读取 `models` 中的字段顺序，把 `flds` 按原字段名无损展开，保存到 `source/existing_tsv/`；同时保存原 GUID、note/model 标识、card/deck 标识和标签。collection 不是可直接读取的 SQLite 时，使用当前可用的兼容 APKG 读取工具；没有兼容工具就停止并报告，禁止把压缩数据当 SQLite 或生成虚假导出。

迁移时旧 GUID 和 deck/model 身份只作为来源身份记录。新包是否沿用身份必须按用户要求和卡片规范决定，禁止直接覆盖旧 APKG。

## 来源保护与冲突

- PDF 与 OCR 同时存在时互补使用：Markdown 负责检索，PDF 页图负责核对公式、表格和图片。
- 多章文件记录每章起止页；找不到边界时停止，不按字符比例猜测。
- 用户排除的章节不得进入笔记、卡片、报告或打印版。
- 不同来源冲突时保留各自说法和来源；能由原教材核实的，以原教材为准并记录修订。
- 无法判定的内容写入存疑报告，不把任一说法标为已验证事实。
