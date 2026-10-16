r"""LanceDB 读写层的测试。

    cd backend && uv run pytest -q tests/test_store.py

用真实的 LanceDB（本地文件库，免费且很快），但向量是手写的假向量，所以不联网、不花钱。
测试用 8 维向量（而不是 2048 维），只为跑得快，逻辑完全一样。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from app.core.schema import chunk_row, source_id_for, utc_now
from app.core.store import Store, StoreError

DIM = 8


def make_store(tmp_path: Path, *, dim: int = DIM) -> Store:
    return Store(tmp_path / "lancedb", dim=dim)


def vec(*values: float) -> list[float]:
    """把给出的几个数补零到 DIM 维，方便手写「指向某个方向」的向量。"""
    assert len(values) <= DIM
    return [float(value) for value in values] + [0.0] * (DIM - len(values))


def row(index: int, vector: list[float], *, source_id: str = "src_a", text: str = "内容") -> dict:
    return chunk_row(
        chunk_id=f"{source_id}_{index:05d}",
        source_id=source_id,
        vector=vector,
        blocking={
            "heading": "第一章",
            "text": text,
            "kind": "text",
            "start": index * 100,
            "end": index * 100 + 50,
            "char_count": len(text),
            "images": [],
        },
        rel_path="full.md",
        embedded_at=utc_now(),
    )


def source_row(source_id: str = "src_a", *, sha256: str = "abc", chunk_count: int = 3) -> dict:
    return {
        "source_id": source_id,
        "name": "part1.pdf",
        "path": r"D:\MinerU Outputs\part1.pdf",
        "sha256": sha256,
        "chunk_count": chunk_count,
        "image_count": 1,
        "embed_model": "voyageai/voyage-multimodal-3.5",
        "embed_dim": DIM,
        "added_at": utc_now(),
        "embedded_at": utc_now(),
    }


# ---------------------------------------------------------------- 基本读写


def test_fresh_store_is_empty(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    assert store.stats() == (0, 0)
    assert store.list_sources() == []


def test_add_chunks_and_count(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    store.add_chunks([row(1, vec(1, 0)), row(2, vec(0, 1))])

    assert store.count_chunks() == 2
    assert store.count_chunks("src_a") == 2
    assert store.count_chunks("src_b") == 0
    assert store.stats() == (2, 0)


def test_add_chunks_empty_is_noop(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    assert store.add_chunks([]) == 0
    assert store.count_chunks() == 0


# ---------------------------------------------------------------- 检索


def test_search_returns_nearest_first_with_distance(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    store.add_chunks(
        [
            row(1, vec(1, 0), text="甲"),
            row(2, vec(0, 1), text="乙"),
            row(3, vec(-1, 0), text="丙"),
        ]
    )

    hits = store.search(vec(1, 0), top_k=3)

    assert [hit["text"] for hit in hits] == ["甲", "乙", "丙"]
    assert hits[0]["_distance"] < hits[1]["_distance"] < hits[2]["_distance"]
    assert hits[0]["_distance"] == pytest.approx(0.0, abs=1e-6)  # 与查询向量同向 → 距离 0
    assert "vector" not in hits[0], "检索结果不该把 2048 维向量搬回来"


def test_search_limit_and_fields(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    store.add_chunks([row(i, vec(1, i / 10)) for i in range(5)])

    hits = store.search(vec(1, 0), top_k=2)

    assert len(hits) == 2
    assert set(hits[0]) >= {"chunk_id", "source_id", "heading", "text", "kind", "start", "end", "images"}


def test_search_can_filter_by_source(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    store.add_chunks([row(1, vec(1, 0), source_id="src_a"), row(1, vec(1, 0.1), source_id="src_b")])

    only_b = store.search(vec(1, 0), top_k=5, source_ids=["src_b"])

    assert [hit["source_id"] for hit in only_b] == ["src_b"]


def test_search_rejects_wrong_dimension(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    with pytest.raises(StoreError, match="维度不符"):
        store.search([0.1] * 3, top_k=1)


# ---------------------------------------------------------------- 数据源与替换


def test_upsert_source_keeps_single_row(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    store.upsert_source(source_row(sha256="v1", chunk_count=3))
    store.upsert_source(source_row(sha256="v2", chunk_count=5))

    sources = store.list_sources()
    assert len(sources) == 1, "同一个 source_id 反复写入只会更新那一行"
    assert sources[0]["sha256"] == "v2"
    assert sources[0]["chunk_count"] == 5


def test_delete_source_purge_removes_chunks(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    store.add_chunks([row(1, vec(1, 0)), row(2, vec(0, 1))])
    store.upsert_source(source_row())

    store.delete_source("src_a", purge=True)

    assert store.count_chunks() == 0
    assert store.get_source("src_a") is None


def test_delete_source_without_purge_keeps_chunks(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    store.add_chunks([row(1, vec(1, 0))])
    store.upsert_source(source_row())

    store.delete_source("src_a", purge=False)

    assert store.count_chunks() == 1
    assert store.get_source("src_a") is None


def test_drop_source_chunks_only_touches_that_source(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    store.add_chunks([row(1, vec(1, 0), source_id="src_a"), row(1, vec(0, 1), source_id="src_b")])

    store.drop_source_chunks("src_a")

    assert store.count_chunks("src_a") == 0
    assert store.count_chunks("src_b") == 1


# ---------------------------------------------------------------- schema 保护


def test_reopening_with_other_dimension_is_rejected(tmp_path: Path) -> None:
    """换个嵌入维度重开同一目录必须报错，绝不能让两种维度的向量混在一张表里。"""
    make_store(tmp_path, dim=8)
    with pytest.raises(Exception) as error:
        make_store(tmp_path, dim=4)
    assert "schema" in str(error.value).lower() or "dimension" in str(error.value).lower()


def test_wrong_dimension_row_is_rejected(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    with pytest.raises(Exception):
        store.add_chunks([row(1, [0.1] * 3)])


def test_source_id_is_stable_and_path_based(tmp_path: Path) -> None:
    assert source_id_for(tmp_path) == source_id_for(tmp_path)
    assert source_id_for(tmp_path) != source_id_for(tmp_path / "other")
    assert source_id_for(tmp_path).startswith("src_")
