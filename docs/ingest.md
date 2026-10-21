# `ingest.py` 说明

`backend/app/core/ingest.py` 是导入管线的编排层：

```text
数据源目录
  │
  ├─ 有非空 full.md → chunk_markdown()
  └─ 没有/空 full.md → chunk_images()
                         │
                         ▼
                embed_blocks()（在线嵌入）
                         │
                         ▼
        先删旧块 → add_chunks() → upsert_source()
```

它把 `chunking.py`（怎么切）、`openrouter.py`（怎么向量化）和 `store.py`（怎么落 LanceDB）串起来，并负责幂等、进度、结果统计和删除。

## 入口与返回值

```python
def ingest(
    source_dir: str | Path,
    store: Store,
    *,
    api_key: str,
    model: str = DEFAULT_MODEL,
    dim: int = DEFAULT_DIM,
    base_url: str = DEFAULT_EMBED_BASE_URL,
    batch: int = DEFAULT_BATCH,
    force: bool = False,
    on_progress: ProgressFn | None = None,
) -> IngestResult
```

- `source_dir` 会先 `resolve()`，后续路径、`source_id`、`rel_path` 都以解析后的目录为准。
- `source_id` 由 `source_id_for()` 根据目录路径生成，同一目录重复导入会得到同一个 id。
- `force=True` 跳过内容/模型比较，强制重新向量化。
- `on_progress(done, total, stage)` 是给后台任务用的进度回调；阶段是 `分块` / `扫描图片` / `向量化` / `写库`。

`IngestResult` 里几个字段需要注意：

| 字段 | 含义 |
| --- | --- |
| `chunk_count` | 这次写入的块数；跳过时是从库里查到的现有块数 |
| `image_count` | **包含图片的块数**，不是图片总张数；文本块挂多张图也只算一块 |
| `skipped` | 指纹和嵌入模型都没变，直接跳过 |
| `sha256` | 内容指纹：`full.md` 哈希或图片集指纹 |
| `reason` | 这次为什么导入/跳过 |
| `warnings` | 分块阶段的降级信息 |
| `usage` / `seconds` | 本次嵌入调用量与耗时 |

## 1. 选择文本模式还是纯图片模式

```python
# 0 字节或只有空白的 full.md 等于「没有文本」，走纯图片路径
text_mode = full_md.is_file() and bool(full_md.read_text(encoding="utf-8").strip())
if text_mode:
    report(0, 1, "分块")
    chunked = chunk_markdown(full_md)
else:
    report(0, 1, "扫描图片")
    chunked = chunk_images(source_dir)   # 没有图片时会抛 FileNotFoundError
```

- 有非空 `full.md`：走文本分块，图片只处理 `full.md` 真正引用的部分。
- `full.md` 缺失、0 字节或只有空白：走纯图片分块，一张图一个块，`text=""`。
- 两者都没有：`chunk_images()` 抛 `FileNotFoundError`，上层 API 会把它变成 400。

## 2. 幂等：内容和嵌入模型都要一致

```python
existing = store.get_source(source_id)
same_content = bool(existing) and existing.get("sha256") == chunked.sha256
same_embedder = (
    bool(existing)
    and existing.get("embed_model") == model
    and int(existing.get("embed_dim") or 0) == dim
)
if same_content and same_embedder and not force:
    return IngestResult(..., skipped=True, reason="内容与嵌入模型均未变化")
```

判断条件有两层：

1. **内容指纹**没变；
2. **嵌入模型和维度**也没变。

只比内容会出问题：换了嵌入模型后继续用旧向量，检索空间就对不上。所以模型或维度变化时会重新向量化，`reason` 会写成「嵌入模型/维度已变为 …」。

注意：`same_embedder` 只比较 `model + dim`，不比较 `base_url` 或 API Key。换了一个提供同样模型名但语义不同的端点时，程序仍会认为“没变”；需要 `force=True` 或清掉数据源重新导入。

其他 `reason`：

- `force=True` → `强制重新导入`；
- 文本内容变化 → `full.md 内容已变化`；
- 纯图片内容变化 → `图片集内容已变化`。

## 3. 向量化

```python
result = embed_blocks(
    chunked.blocks,
    api_key=api_key,
    model=model,
    dim=dim,
    base_url=base_url,
    batch=batch,
    on_progress=lambda done, total: report(done, total, "向量化"),
)
```

`embed_blocks()` 对纯文本块按批发送，对带图块一条一个请求，返回的向量顺序与 `chunked.blocks` 一致。图片块的 `text` 为空时，嵌入请求只包含图片内容。

这一步失败会直接抛异常，不会进入写库；因此不会出现“只写了一半新块”的情况（旧块也还没删）。

## 4. 构造 LanceDB 行

每个 `Block` 对应一行 `chunks`：

```python
chunk_row(
    chunk_id=f"{source_id}_{index:05d}",  # 稳定且可读的块 id：源 id + 序号
    source_id=source_id,
    vector=vector,
    blocking={
        "heading": block.heading,
        "text": block.text,
        "kind": block.kind,
        "start": block.start,
        "end": block.end,
        "char_count": block.char_count,
        "images": block.images,
    },
    rel_path=rel_path_for(block),
    embedded_at=embedded_at,
)
```

`rel_path` 的规则：

- 文本块统一记 `full.md`；
- 纯图片块记图片自己的相对路径，例如 `images/animal-03.jpeg`，方便前端回溯和以图搜图返回路径。

`chunk_id` 用「源 id + 5 位序号」，同一目录、同一顺序重新导入时 id 稳定，便于调试。

## 5. 先删旧块，再写新块

```python
if existing:
    # 先删旧的分块，再写新的：避免同一目录留下两代内容
    store.drop_source_chunks(source_id)
written = store.add_chunks(rows)
store.upsert_source({...})
```

- `added_at` 保留旧值；`embedded_at` 更新为本次时间。
- `chunk_count` 用实际写入数，`image_count` 统计带图块数。
- `sources` 行用 `merge_insert(on="source_id")` 更新，不会产生重复数据源。

### 一个需要知道的边界

LanceDB 的删除、写入和 `sources` 更新不是跨步骤事务。如果旧块已经删掉、`add_chunks()` 失败，库里会暂时留下“数据源行还在、分块为空”的状态；重新导入同一个目录可以恢复。当前实现优先保证“不出现新旧两代分块并存”，没有做 staging 表或回滚。

## 6. 进度与摘要

- 分块前：`(0, 1, "分块")` 或 `(0, 1, "扫描图片")`。
- 嵌入中：`(done, total, "向量化")`，`total` 是块数。
- 写库前：`(len(rows), len(rows), "写库")`。
- `IngestResult.summary()` 给后台任务/日志用：
  - 跳过：`xxx: 内容与嵌入模型均未变化，跳过导入（库里仍有 N 块）`
  - 写入：`xxx: 写入 N 块（含图 M 块），K 次调用，$费用，耗时`

## 7. `remove_source()`

```python
def remove_source(path_or_id: str | Path, store: Store) -> None:
    raw = str(path_or_id)
    source_id = raw if raw.startswith("src_") else source_id_for(Path(raw))
    store.delete_source(source_id, purge=True)
```

传目录路径或 `source_id` 都能删除数据源和它的全部分块。

注意：它只处理向量库。数据源和分组的成员关系由 HTTP 路由在删除后调用 `GroupStore.forget_source()` 清理，`ingest.py` 不直接依赖分组模块。

## 关键取舍

| 取舍 | 原因 / 代价 |
| --- | --- |
| 先算指纹再决定是否嵌入 | 重复导入不花钱；纯图片目录需要先读一遍所有图片算哈希 |
| 内容 + 模型/维度双重幂等 | 避免换模型后混用旧向量；换端点但模型名相同时需要手动 `force` |
| 先删旧块再写新块 | 避免新旧两代并存；失败时可能短暂出现空数据源，重跑可恢复 |
| 行 id 用源 id + 序号 | 稳定、可读；分块顺序变化时序号对应的内容可能变化 |
| 纯图片块 `text=""` | 只嵌入图片，不制造无意义文本；`heading` 只做展示 |

## 测试覆盖

对应 `backend/tests/test_ingest.py`，当前 16 条，主要覆盖：

- 首次导入、重复导入跳过、内容变化重导、模型/维度变化重导、`force`；
- 文件名/目录名、稳定 chunk id、进度回调；
- 文本 + 图片块、纯图片目录、空 `full.md` 回退到图片；
- 缺失 `full.md` 且无图片的报错；
- 图片集变化触发重导；
- 导入后的块能被 `Store.search()` 检索到。

相关文档：[chunking.md](chunking.md)（怎么切块）、[`openrouter.py`](../backend/app/core/openrouter.py)（怎么嵌入）、[`store.py`](../backend/app/core/store.py)（怎么落库）。
