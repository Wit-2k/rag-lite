# `chunking.py` 说明

`backend/app/core/chunking.py` 负责把数据源拆成可以送去嵌入的 `Block`。它有两条入口：

- **文本目录**：有非空 `full.md` 时调用 `chunk_markdown()`，按标题、表格、代码块和正文切分，并把 `full.md` 引用的本地图片挂到对应块上。
- **纯图片目录**：没有 `full.md`（或文件为空）时调用 `chunk_images()`，一张图片生成一个 `Block`，文本为空，只嵌入图片。

分块结果由 `ingest.py` 消费：它把每个 `Block` 交给 `openrouter.embed_blocks()` 向量化，再写入 LanceDB。分块质量会直接影响召回，所以这里没有调用现成的“通用文本切分器”一把梭，而是围绕 MinerU 产物做了几处专门处理。

## 快速流程

```text
full.md
  │
  ├─ MarkdownChef 找表格 / 代码块 ──→ 原子块（不参与正文切分）
  ├─ ATX 标题切 section ───────────→ 标题面包屑（一级 > 二级 > 三级）
  ├─ 正文空洞交给 RecursiveChunker ─→ 按中文标点压到 chunk_size 内
  ├─ 过小的正文块向前/向后合并 ─────→ 避免一句话一个块
  └─ 按字符区间挂图片 ─────────────→ 只保留 full.md 真正引用的本地图片
                                          │
                                          ▼
                                   ChunkResult(blocks, sha256, warnings, stats)
```

没有 `full.md` 时走另一条更短的路：

```text
images/（没有该子目录时扫描源目录本身）
  └─ 一张图片 → 一个 Block(text="", images=(path,)) → 图片集指纹
```

## 数据结构

### `Block`

`Block` 是一个待嵌入单元。关键字段：

| 字段 | 含义 |
| --- | --- |
| `kind` | `text` / `table` / `code` / `image` |
| `heading` | 标题面包屑，例如 `第一章 > 1.2 奖励标准`；纯图片块是 `图片：images/x.jpg` |
| `level` | 标题层级；纯图片块为 0 |
| `body` | 正文、表格或代码块本身 |
| `text` | 真正送去嵌入的文本，通常是 `heading + "\n\n" + body`；纯图片块为空 |
| `start` / `end` | 在 `full.md` 中的字符区间；纯图片块为 0 |
| `images` | 这个块携带的本地图片绝对路径 |

`char_count` 是 `len(text)`，用于本模块的块长控制；它不是模型 token 数。项目用字符级 tokenizer，中文通常接近“一字一 token”，但实际计费仍以嵌入/回答接口返回的 token 为准。

### `ChunkResult`

- `blocks`：结果列表。
- `sha256`：文本路径是 `full.md` 的哈希；纯图片路径是“图片相对路径 + 每张图内容哈希”的组合指纹。
- `warnings`：不阻断导入的降级信息，例如图片缺失、表格无法按行拆、短块无法合并。
- `stats`：块数、表格/代码块数、合并数、图片引用/挂载/丢弃数、字符长度分布，方便看分块是否合理。

## 文本路径：`chunk_markdown()`

### 1. 先找出原子块

MarkdownChef 能识别管道表格和代码块，但 MinerU 常输出 HTML `<table>`，所以模块自己补了 HTML 表格扫描：

```python
atomics = _dedupe_spans(
    _html_table_spans(md)
    + [(KIND_TABLE, t.start_index, t.end_index) for t in doc.tables]
    + [(KIND_CODE, c.start_index, c.end_index) for c in doc.code]
)
```

这些区间不会被正文切分器再次切开；`_dedupe_spans()` 会丢掉空区间和被外层区间完全包住的重复片段。落在原子块里的 `#` 也不会被当成标题。

### 2. 按标题切 section

`_sections()` 用 ATX 标题（`#` 到 `######`）切段，并用一个栈维护面包屑。标题行本身从正文中去掉，只留在 `heading` 里：

```python
while stack and stack[-1][0] >= level:
    stack.pop()
stack.append((level, title))
end = headings[index + 1].start() if index + 1 < len(headings) else len(md)
sections.append(_Section(level, " > ".join(t for _, t in stack), match.end(), end))
```

第一个标题之前的内容会成为一个 `level=0`、面包屑为空的前言 section。

### 3. 正文按中文标点递归切分

正文交给 `RecursiveChunker`，但规则是项目自定义的：

```python
CN_RULES = RecursiveRules(
    levels=[
        RecursiveLevel(delimiters=["\n\n", "\r\n", "\n", "\r"]),
        RecursiveLevel(delimiters=["。", "！", "？", "；", "…"]),
        RecursiveLevel(delimiters=["，", "、", "：", "（", "）", ...]),
        RecursiveLevel(whitespace=True),
        RecursiveLevel(),
    ],
)
```

这里有两个容易踩的点：

- chonkie 默认规则主要认英文句末标点，中文文本会一路退到按空格或字符硬切，所以显式加入中文标点。
- `MIN_CHARS_PER_CHUNK = 1`。chonkie 默认 `min_characters_per_chunk=24`，中文短句会被合并成一片，句子级分隔符直接失效；这里把下限降到 1，是否合并交给后面的 `min_chunk_size` 控制。

`_complement()` 找出 section 中不属于原子块的文本空洞，逐段调用 `_split_text()`。`_split_text()` 返回的是相对偏移，调用方会加回 `hole_start`，保证 `Block.start/end` 始终指向 `full.md` 的绝对字符位置。

同一段文本里相邻的过小片段会先合并：

```python
if (
    0 <= gap <= 40
    and prev_len + gap + cur_len <= chunk_size
    and (prev_len < min_chunk_size or cur_len < min_chunk_size)
):
    merged[-1] = (prev_start, end)
```

### 4. 超长表格按行拆，代码块整体保留

`max_table_chars` 默认 2000。超过时：

- HTML 表格优先按 `<tr>` 拆，并把首行当作表头在每片重复；这样 MinerU 没有 `<thead>` 的表格也不会从第二片开始丢表头。
- 如果按行拆不出来，退到 `TableChunker`。
- 代码块通常不能按行拆出语义，超长时整体保留并记 warning。

拆分出来的每一片都共享父表格的字符区间，方便图片归属和问题定位。

### 5. 小块向前/向后合并

正文块如果短于 `min_chunk_size`（默认 120 字符），会尝试并入同一文档的下一块；下一块不可并时再并入前一块：

```python
if (
    block.kind == KIND_TEXT
    and len(block.body) < min_chunk_size
    and following is not None
    and _mergeable(block, following, ...)
):
    forward.append(_join(block, following))
```

`_mergeable()` 还限制三点：同一顶层文档、块之间 gap 不超过 80、合并后不超过 `chunk_size`（表格则用 `max_table_chars`）。合并后保留层级更深的标题，块类型保留更具体的表格/代码。

仍然无法合并的短块不会被丢弃，会记进 `warnings`，同时出现在 `stats.blocks_tiny_left` 里。

### 6. 图片按字符区间归属

`_resolve_images()` 只处理 `full.md` 里 `![](...)` 引用的本地图片：

- 远程图片（`http://`、`https://`、`data:`、`//`）跳过；
- 去重；文件不存在、超过 `image_max_bytes`（默认 4MB）都会记 warning 并跳过。

图片按它在 Markdown 中出现的位置，挂到覆盖该位置的块上；位置落在两个块之间时归给前一个块。每个块最多 `max_images_per_block`（默认 12）张，超出部分丢弃并记 warning：

```python
bucket = attached.setdefault(index, [])
if len(bucket) >= max_images_per_block:
    dropped += 1
    warnings.append(f"{image.name}: 所在块附图已达上限 {max_images_per_block}，丢弃")
    continue
bucket.append(image)
```

12 张的上限来自实测：subfigure 多的论文单块最多见过 9 张；再大就会让单次嵌入请求的 data URI 体积变得很夸张。

## 纯图片路径：`chunk_images()`

### 1. 找图片

`image_files()` 优先扫描 `images/` 子目录；没有这个子目录时直接扫描源目录本身。只接受：

```python
IMAGE_SUFFIXES = {".jpg", ".jpeg", ".png", ".webp"}
```

这样 MinerU 产物和“把一批图片直接放进一个文件夹”两种形态都能用。

### 2. 一张图一个块

`chunk_images()` 不产生任何文本嵌入：

```python
Block(
    kind=KIND_IMAGE,
    heading=f"图片：{rel}",
    level=0,
    body="",
    text="",          # 纯图片块不产生文本嵌入
    start=0,
    end=0,
    images=(path,),
)
```

`heading` 里的路径只用于检索结果展示和排查，不会进入嵌入请求。真正送嵌入接口的是 `images` 里的图片。

### 3. 图片集指纹

幂等指纹不是某一张图的哈希，而是“相对路径 + 每张图内容哈希”依次喂给同一个 SHA-256：

```python
digest.update(rel.encode("utf-8"))
digest.update(file_sha256(path).encode("ascii"))
```

因此加图、删图、换图、改图片内容都会让指纹变化，触发重新导入；图片没变则直接跳过。

## 调用关系

`ingest.py` 的分支决定了走哪条路径：

```text
full.md 存在且非空  → chunk_markdown(full_md)
否则                → chunk_images(source_dir)
```

向量化侧不区分两种来源：`openrouter.embed_blocks()` 对有图的块发送 `image_url` 内容；纯图片块的 `text` 为空，因此输入里只有图片，没有空文本部分。

## 关键取舍

| 取舍 | 原因 / 代价 |
| --- | --- |
| 自己处理 HTML 表格 | MinerU 的 `<table>` 很常见，chonkie 的表格能力主要针对管道表格；代价是要自己维护按行拆和表头重复逻辑 |
| 字符级 tokenizer | 不依赖某个模型的分词器，中文按字数可控；代价是与真实 token 数不完全等价 |
| `chunk_size=1000`、`min_chunk_size=120` | 1000 字左右适合放进 2048 维嵌入和后续上下文；120 字以下通常是文件号、表题等噪声 |
| 小块向前/向后合并 | 减少“34 个字的块占满检索名额”；代价是块边界不再严格等于原始段落 |
| 只挂 `full.md` 引用的图片 | 不把产物目录里未被正文使用的图片带进来；代价是纯图片目录必须走 `chunk_images()` |
| 原子块拆片共享父区间 | 图片归属和问题定位仍指向父表格/代码块；代价是同一父区间可能对应多个块 |
| 图片集用内容指纹 | 换图能准确触发重导；代价是导入前要读一遍所有图片算哈希 |

## 测试覆盖

对应 `backend/tests/test_chunking.py`，当前 31 条，主要覆盖：

- Markdown 标题、前言、面包屑和层级；
- HTML 表格、管道表格、代码块作为原子块；
- 超长 HTML 表格按行拆且每片重复表头；
- 中文标点切分、短片段合并、小块向前/向后归并；
- 图片引用、远程图片跳过、缺失/超大图片 warning、单块图片上限；
- 纯图片目录、`images/` 子目录与扁平目录两种扫描方式、图片集指纹变化。

改分块参数或图片规则时，先跑：

```powershell
cd backend
uv run pytest -q tests/test_chunking.py
```

相关文档：[ingest.md](ingest.md)（导入管线如何消费这些块）。
