# rag-lite 后端

把 MinerU 手工解析出来的产物目录（`full.md` + `images/`）变成可检索的知识库，并提供本机 HTTP 接口。
整条链路：**分块 → 向量化 → 存 LanceDB → 余弦检索 → 重排 → 交给大模型流式回答**。

## 环境要求

- Windows 11（其它平台没试过，路径与脚本按 Win 写的）
- [uv](https://docs.astral.sh/uv/)（依赖管理）、Python 3.14（`.python-version` 已锁定，uv 会自动准备）
- 一个 OpenRouter API Key（嵌入、重排、默认回答模型都从它走）

## 起步

```bash
uv sync                                  # 装依赖
cp .env.example .env                     # 然后把 OPENROUTER_API_KEY 填进去（.env 已被 git 忽略）
uv run pytest -q                         # 96 条测试，默认只有 3 条会真打接口
uv run python main.py                    # 起服务，默认 127.0.0.1:17650
```

启动后 stdout 会打印一行握手，前端据此知道端口：

```
RAGLITE_READY {"host": "127.0.0.1", "port": 17650}
```

端口被占用会自动顺延；`--port 0` 表示自动挑；`--data-dir` 可换数据目录。

## HTTP 接口

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/api/health` | 就绪探测：块数、数据源数、密钥是否配好、维度、当前模型 |
| GET | `/api/sources` | 数据源列表（`missing=true` 表示产物目录已经不在了） |
| POST | `/api/sources` | 登记一个产物目录并开始导入，立刻返回 `202 {job_id}` |
| DELETE | `/api/sources/{id}?purge=true` | 移除数据源（默认连分块一起删） |
| GET | `/api/jobs/{id}` | 导入进度：`state/total/done/message/result` |
| POST | `/api/chat` | 提问，返回 SSE：`sources` → `token`… → `done`（或 `error`） |
| POST | `/api/chat/clear` | 清空对话上下文 |

对话历史默认每轮全带（由后端会话持有，前端不用上传）；`clear` 之后历史清空。
`/docs` 有 FastAPI 自动生成的接口页面，调试很方便。

## 目录结构

```
main.py                  入口：选端口 → uvicorn → 打印握手
app/config.py            配置：命令行 > 环境变量 > .env > 默认
app/api/                 HTTP 层：models（DTO）/ jobs（后台任务）/ routes（7 个端点）/ server（组装）
app/core/chunking.py     分块：标题切 section、表格/代码保持原子、图片按区间归属
app/core/openrouter.py   云端调用：嵌入、重排、流式对话（共用一套重试/退避）
app/core/store.py        LanceDB 读写：建表、写入、余弦检索、删除
app/core/ingest.py       导入管线：分块→向量化→入库，按 full.md 哈希幂等
app/core/query.py        问答管线：检索→重排→拼提示词→流式回答 + 历史管理
app/core/schema.py       两张表的 Arrow schema
data/lancedb/            向量库（git 忽略，运行期生成）
tests/                   测试 + 可人工复查的报告生成器（out/ 被 git 忽略）
```

## 端到端脚本（会真花钱，但很便宜）

```bash
uv run python tests/chunking_report.py   # 只分块，不花钱
uv run python tests/embedding_report.py  # 向量化 + 相似度自检（全量约 $0.015）
uv run python tests/ingest_report.py     # 导入 + 跨文档检索（已导入过的目录会跳过，$0）
uv run python tests/rerank_report.py     # 余弦 vs 重排对照（约 $0.002）
uv run python tests/chat_report.py       # 真实多轮对话（约 $0.002）
```

报告写在 `backend/tests/out/`，用编辑器直接看。

## 换算与花费（实测，2026-10）

| 动作 | 用量 | 费用 |
| --- | --- | --- |
| 全量导入 387 块（含 7 张图） | ~119K token + 4K 图片 token | ≈ $0.015 |
| 同一目录重复导入 | 0 | $0（哈希没变直接跳过） |
| 一次检索 + 重排（20 篇候选） | ~6K token | ≈ $0.0006 |
| 一轮回答（gemini-2.5-flash + 3 条片段） | ~1.4K prompt / 0.1K completion | ≈ $0.0007 |

## 打包

`zip/` 下留有打包脚本占位（PyInstaller 打后端 + `dotnet publish` 打前端 + 压缩成便携包），尚未实现。
