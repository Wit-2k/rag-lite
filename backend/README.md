# rag-lite 后端

把 MinerU 产物目录（`full.md` + `images/`）或纯图片目录变成可检索的知识库，并提供本机 HTTP 接口。
整条链路：**分块 → 向量化 → 存 LanceDB → 余弦检索 → 重排 → 交给大模型流式回答**。

## 环境要求

- Windows 11（其它平台没试过，路径与脚本按 Win 写的）
- [uv](https://docs.astral.sh/uv/)（依赖管理）、Python 3.14（`.python-version` 已锁定，uv 会自动准备）
- 一个 OpenRouter API Key（嵌入、重排、默认回答模型都从它走）

## 起步

```bash
uv sync                                  # 装依赖
cp .env.example .env                     # 然后把 OPENROUTER_API_KEY 填进去（.env 已被 git 忽略）
uv run pytest -q                         # 199 条测试，默认只有 3 条会真打接口
uv run python main.py                    # 起服务，默认 127.0.0.1:17650
```

启动后 stdout 会打印一行握手，前端据此知道端口：

```
RAGLITE_READY {"host": "127.0.0.1", "port": 17650}
```

端口被占用会自动顺延；`--port 0` 表示自动挑；`--data-dir` 可换数据目录。

## 配置

配置优先级：**命令行参数 > 环境变量 > `backend/.env` > 代码默认值**。完整模板见 `.env.example`。

三个模型角色各自的**接入点 + 模型 ID + 密钥**都能单独配，密钥留空时回落到共享的 `OPENROUTER_API_KEY`：

| 角色 | 接入点 | 模型 | 密钥 | 维度 |
| --- | --- | --- | --- | --- |
| 嵌入 | `EMBED_BASE_URL` | `EMBED_MODEL` | `EMBED_API_KEY` | `EMBED_DIM`（256/512/1024/2048） |
| 重排 | `RERANK_BASE_URL` | `RERANK_MODEL` | `RERANK_API_KEY` | – |
| 回答 | `CHAT_BASE_URL` | `CHAT_MODEL` | `CHAT_API_KEY` | – |

嵌入只填 `base_url`，程序自己接 `/embeddings`；重排同理接 `/rerank`；回答直接走 chat completions。

**换嵌入模型时注意**：维度写进了向量库的 schema。

- 只换模型、维度不变 → 重新导入即可，`ingest` 发现「入库时的模型」与当前配置不同就会自动重新向量化；
  `/api/sources` 会把旧模型入库的数据源标成 `stale=true`，`/api/health` 汇总成 `sources_stale`。
- 维度也变了 → 必须重建库：换 `RAGLITE_DB_URI` 指向新目录，或删掉 `data/lancedb` 重新导入。
  忘了这一步不会静默出错——`Store` 启动时会比对现有维度并提示：现在几维、配置几维、该删库还是该改回配置。

其它可调项：`RETRIEVE_TOP_K`（默认 20）、`CONTEXT_N`（默认 3）、`RAGLITE_HOST` / `RAGLITE_PORT`、`RAGLITE_DB_URI`（向量库位置；会话库同步到它的上一级）。

注意：`root` / `data_dir` / `db_uri` / `host` / `port` **不能**从界面改（它们决定库在哪），其余都能改。
界面改的值会写进 `.env`，所以手工编辑 `.env` 与界面改配置是同一份数据，不冲突。
起服务后 `GET /api/health` 会回传三个角色**当前实际在用**的接入点与模型，改完配置可直接核对。

## HTTP 接口

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/api/health` | 就绪探测：块数、数据源数、密钥是否配好、三个角色的接入点/模型/维度、`sources_stale` |
| GET | `/api/sources` | 数据源列表（`missing` = 产物目录不在了；`stale` = 需要重新导入） |
| POST | `/api/sources` | 登记一个产物目录或纯图片目录并开始导入，立刻返回 `202 {job_id}` |
| DELETE | `/api/sources/{id}?purge=true` | 移除数据源（默认连分块一起删） |
| PUT | `/api/sources/{id}/groups` | 整体替换数据源所属分组（空列表 = 移出所有分组） |
| GET | `/api/jobs/{id}` | 导入进度：`state/total/done/message/result` |
| GET | `/api/sessions` | 会话列表（最近使用倒序，带消息数） |
| POST | `/api/sessions` | 新建会话（可同时绑定检索范围分组） |
| PUT | `/api/sessions/{id}` | 重命名会话 |
| DELETE | `/api/sessions/{id}` | 删除会话及其聊天记录 |
| GET | `/api/sessions/{id}/messages` | 某会话的全部消息（含助手消息附带的来源 JSON） |
| PUT | `/api/sessions/{id}/group` | 设置会话检索范围；`group_id=null` 表示全部数据源 |
| GET | `/api/groups` | 分组列表（带成员数） |
| POST | `/api/groups` | 新建分组（重名或保留名返回 409） |
| PUT | `/api/groups/{id}` | 重命名分组 |
| DELETE | `/api/groups/{id}` | 删除分组；还有会话在用时返回 409 并列出会话 |
| POST | `/api/chat` | 提问，返回 SSE：`sources` → `token`… → `done`（或 `error`） |
| POST | `/api/chat/clear` | 清空某个会话的对话上下文 |
| GET | `/api/settings` | 当前生效配置（密钥只回固定圆点，不露任何片段；附 `.env` 路径与库维度） |
| PUT | `/api/settings` | 改配置：写入 `.env` **并立即生效**；留空的字段不动；维度对不上库或参数非法返回 400 |

对话历史默认每轮全带（由后端会话持有，前端不用上传）；`clear` 之后历史清空。
`/docs` 有 FastAPI 自动生成的接口页面，调试很方便。

## 目录结构

```
main.py                  入口：选端口 → uvicorn → 打印握手
app/config.py            配置：命令行 > 环境变量 > .env > 默认
app/api/                 HTTP 层：models（DTO）/ jobs（后台任务）/ routes（20 个端点）/ server（组装）
app/core/chunking.py     分块：标题切 section、表格/代码保持原子、图片按区间归属
app/core/openrouter.py   云端调用：嵌入、重排、流式对话（共用一套重试/退避）
app/core/store.py        LanceDB 读写：建表、写入、余弦检索、删除
app/core/ingest.py       导入管线：分块→向量化→入库，按 full.md 或图片集哈希幂等
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

```powershell
powershell -File zip\package.ps1        # 后端 PyInstaller + 前端 dotnet publish + 组装 + 压缩
```

产物在 `release/rag-lite-<版本>.zip`（同级的 `release/rag-lite-<版本>/` 解包目录可直接运行）。
便携版里 `.env` 与 `data/` 都在**便携根**（与 `RagLite.exe` 同级）——代码被打进 `_internal/` 后，
后端靠 `sys.frozen` 反推路径，密钥不用塞进 `_internal/`。
注意 `-SkipBackend` 只在后端源码没改过时使用，否则会把旧后端打进包。
