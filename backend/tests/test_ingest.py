r"""导入管线的测试（MinerU 产物目录 → 分块 → 向量化 → LanceDB）。

    cd backend && uv run pytest -q tests/test_ingest.py

分块是真的、LanceDB 是真的，只有「调用嵌入接口」这一步被换成假的：
否则每跑一次测试就要花一次钱、还要等网络。真实调用由 tests/embedding_report.py 覆盖。
"""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from app.core import ingest as ingest_module
from app.core.chunking import chunk_markdown
from app.core.ingest import ingest, remove_source
from app.core.openrouter import EmbedResult, EmbedUsage
from app.core.store import Store

DIM = 8
# 每个章节的正文要长于 min_chunk_size(120)，否则会被小块归并逻辑并成一个块，测不出分块数量
FAKE_TEXT = (
    "第一条 为了规范管理，结合学校实际，制定本规定。第二条 本规定适用于学校全体研究生及其指导教师，"
    "各单位应当遵照执行。第三条 研究生院负责本规定的解释与实施，并根据上级文件变化适时修订，"
    "修订后的文本自发布之日起生效。第四条 本规定未尽事宜，按照国家和学校有关规定办理。"
)


def make_source(tmp_path: Path, *, name: str = "part1.pdf", extra: str = "", images: int = 1) -> Path:
    """造一个长得像 MinerU 产物的目录。"""
    root = tmp_path / name
    (root / "images").mkdir(parents=True, exist_ok=True)
    for index in range(images):
        (root / "images" / f"pic{index}.jpg").write_bytes(b"\xff\xd8\xff\xe0fake")
    refs = "".join(f"![](images/pic{index}.jpg)\n\n" for index in range(images))
    (root / "full.md").write_text(
        f"# 测试制度汇编\n\n{FAKE_TEXT}\n\n{refs}## 第一章 总 则\n\n{FAKE_TEXT}\n\n## 第二章 附 则\n\n{FAKE_TEXT}{extra}\n",
        encoding="utf-8",
    )
    return root


@pytest.fixture
def fake_embedder(monkeypatch: pytest.MonkeyPatch) -> list[dict]:
    """把 embed_blocks 换成假实现，并记录调用次数，方便断言「跳过时没有再花钱」。"""
    calls: list[dict] = []

    def fake_embed_blocks(blocks, *, api_key, model, dim, batch, on_progress=None, **kwargs) -> EmbedResult:
        calls.append({"count": len(blocks), "api_key": api_key})
        if on_progress:
            on_progress(len(blocks), len(blocks))
        # 每条都用同一个方向，检索时必然命中，便于断言链路通了
        vectors = [[1.0] + [0.0] * (dim - 1) for _ in blocks]
        return EmbedResult(
            vectors=vectors,
            usage=EmbedUsage(requests=1, prompt_tokens=123, cost_usd=0.0004, seconds=0.01),
            dim=dim,
        )

    monkeypatch.setattr(ingest_module, "embed_blocks", fake_embed_blocks)
    return calls


def make_store(tmp_path: Path) -> Store:
    return Store(tmp_path / "lancedb", dim=DIM)


# ---------------------------------------------------------------- 正常导入


def test_ingest_writes_chunks_and_source_row(tmp_path: Path, fake_embedder: list[dict]) -> None:
    source = make_source(tmp_path)
    store = make_store(tmp_path)
    expected_blocks = chunk_markdown(source / "full.md").blocks

    result = ingest(source, store, api_key="fake", dim=DIM)

    assert not result.skipped
    assert result.chunk_count == len(expected_blocks)
    assert store.count_chunks() == len(expected_blocks)
    assert result.image_count == 1
    assert result.usage.cost_usd == pytest.approx(0.0004)
    assert len(fake_embedder) == 1

    row = store.get_source(result.source_id)
    assert row is not None
    assert row["name"] == "part1.pdf"
    assert row["chunk_count"] == len(expected_blocks)
    assert row["embed_dim"] == DIM
    assert row["sha256"] == hashlib.sha256((source / "full.md").read_bytes()).hexdigest()


def test_ingested_chunks_are_searchable(tmp_path: Path, fake_embedder: list[dict]) -> None:
    source = make_source(tmp_path)
    store = make_store(tmp_path)
    ingest(source, store, api_key="fake", dim=DIM)

    hits = store.search([1.0] + [0.0] * (DIM - 1), top_k=5)

    assert hits, "导入后应该能检索到内容"
    assert all(hit["source_id"] == hits[0]["source_id"] for hit in hits)
    assert all(hit["rel_path"] == "full.md" for hit in hits)
    assert any("测试制度汇编" in hit["heading"] for hit in hits)


def test_chunk_ids_are_stable_across_reingest(tmp_path: Path, fake_embedder: list[dict]) -> None:
    source = make_source(tmp_path)
    store = make_store(tmp_path)
    first = ingest(source, store, api_key="fake", dim=DIM)
    before = {hit["chunk_id"] for hit in store.search([1.0] + [0.0] * (DIM - 1), top_k=99)}

    ingest(source, store, api_key="fake", dim=DIM, force=True)
    after = {hit["chunk_id"] for hit in store.search([1.0] + [0.0] * (DIM - 1), top_k=99)}

    assert before == after, "同样的内容重导后块 id 应该一致（源 id + 序号）"
    assert store.count_chunks(first.source_id) == len(after)


# ---------------------------------------------------------------- 幂等


def test_second_ingest_skips_without_calling_api(tmp_path: Path, fake_embedder: list[dict]) -> None:
    source = make_source(tmp_path)
    store = make_store(tmp_path)
    first = ingest(source, store, api_key="fake", dim=DIM)

    again = ingest(source, store, api_key="fake", dim=DIM)

    assert again.skipped
    assert again.chunk_count == first.chunk_count == store.count_chunks()
    assert len(fake_embedder) == 1, "内容没变就不该再调一次接口"


def test_force_reingest_replaces_without_duplicates(tmp_path: Path, fake_embedder: list[dict]) -> None:
    source = make_source(tmp_path)
    store = make_store(tmp_path)
    first = ingest(source, store, api_key="fake", dim=DIM)

    forced = ingest(source, store, api_key="fake", dim=DIM, force=True)

    assert not forced.skipped
    assert forced.chunk_count == first.chunk_count
    assert store.count_chunks(first.source_id) == first.chunk_count
    assert len(fake_embedder) == 2


def test_changed_full_md_is_reingested(tmp_path: Path, fake_embedder: list[dict]) -> None:
    source = make_source(tmp_path)
    store = make_store(tmp_path)
    first = ingest(source, store, api_key="fake", dim=DIM)

    with (source / "full.md").open("a", encoding="utf-8") as handle:
        handle.write(f"\n\n## 第三章 新增章节\n\n{FAKE_TEXT}\n")

    second = ingest(source, store, api_key="fake", dim=DIM)

    assert not second.skipped
    assert second.sha256 != first.sha256
    assert second.chunk_count > first.chunk_count
    assert store.count_chunks(first.source_id) == second.chunk_count, "旧块必须先删掉，不能新旧并存"
    assert store.stats()[1] == 1


# ---------------------------------------------------------------- 异常与边界


def test_missing_full_md_raises(tmp_path: Path, fake_embedder: list[dict]) -> None:
    empty = tmp_path / "empty.pdf"
    empty.mkdir()
    store = make_store(tmp_path)

    with pytest.raises(FileNotFoundError, match="full.md"):
        ingest(empty, store, api_key="fake", dim=DIM)


def test_progress_reports_stages(tmp_path: Path, fake_embedder: list[dict]) -> None:
    source = make_source(tmp_path)
    store = make_store(tmp_path)
    seen: list[tuple[int, int, str]] = []

    ingest(source, store, api_key="fake", dim=DIM, on_progress=lambda d, t, s: seen.append((d, t, s)))

    assert ("向量化" in [stage for _, _, stage in seen])
    assert ("写库" in [stage for _, _, stage in seen])
    assert seen[-1][0] == seen[-1][1], "最后的进度应该是完成态"


def test_chunking_warnings_are_passed_through(tmp_path: Path, fake_embedder: list[dict]) -> None:
    source = make_source(tmp_path, extra="\n\n![](images/缺失的图.jpg)\n")
    store = make_store(tmp_path)

    result = ingest(source, store, api_key="fake", dim=DIM)

    assert any("图片不存在" in warning for warning in result.warnings)


def test_remove_source_clears_rows(tmp_path: Path, fake_embedder: list[dict]) -> None:
    source = make_source(tmp_path)
    store = make_store(tmp_path)
    result = ingest(source, store, api_key="fake", dim=DIM)

    remove_source(source, store)

    assert store.count_chunks() == 0
    assert store.get_source(result.source_id) is None
    assert store.stats() == (0, 0)


def test_two_sources_live_side_by_side(tmp_path: Path, fake_embedder: list[dict]) -> None:
    first = ingest(make_source(tmp_path, name="part1.pdf"), make_store(tmp_path), api_key="fake", dim=DIM)
    store = make_store(tmp_path)
    second = ingest(make_source(tmp_path, name="part2.pdf"), store, api_key="fake", dim=DIM)

    assert first.source_id != second.source_id
    assert store.stats() == (first.chunk_count + second.chunk_count, 2)
    assert len(store.search([1.0] + [0.0] * (DIM - 1), top_k=99)) == first.chunk_count + second.chunk_count
