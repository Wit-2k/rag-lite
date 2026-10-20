r"""向量化模块的回归测试。

    cd backend && uv run pytest -q
    # 需要联网 + 密钥的那条会被自动跳过（没配 OPENROUTER_API_KEY 时）

除了最后一条「真调一次 API」的冒烟测试，其余都用假的 HTTP 会话，不花钱、不依赖网络。
"""

from __future__ import annotations

import json
import threading
from pathlib import Path

import pytest
import requests

from app.core.chunking import Block
from app.core.openrouter import (
    DEFAULT_DIM,
    EmbedUsage,
    OpenRouterError,
    embed_blocks,
    embed_texts,
    read_api_key,
    rerank,
)


def make_block(text: str, *, images: tuple[Path, ...] = (), heading: str = "测试") -> Block:
    return Block(
        kind="text",
        heading=heading,
        level=1,
        body=text,
        text=f"{heading}\n\n{text}",
        start=0,
        end=len(text),
        images=images,
    )


class FakeResponse:
    """模仿 requests.Response 里本模块用到的三个属性。"""

    def __init__(self, status_code: int, payload: dict | str = "", headers: dict | None = None) -> None:
        self.status_code = status_code
        self._payload = payload
        self.headers = headers or {}
        self.text = payload if isinstance(payload, str) else json.dumps(payload, ensure_ascii=False)

    def json(self) -> dict:
        if isinstance(self._payload, str):
            raise ValueError("不是 JSON")
        return self._payload


def embedding_response(count: int, *, dim: int = DEFAULT_DIM, reverse: bool = False, cost: float = 0.0001) -> dict:
    """造一个和真实接口同形状的响应；reverse=True 时故意乱序，用来验证按 index 归位。"""
    indexes = list(range(count))
    if reverse:
        indexes.reverse()
    return {
        "data": [{"index": i, "object": "embedding", "embedding": [float(i), 1.0] + [0.0] * (dim - 2)} for i in indexes],
        "usage": {"prompt_tokens": count * 10, "total_tokens": count * 10, "cost": cost},
    }


class FakeSession:
    """记录每次请求，按顺序吐出预设响应（可以是异常，用来模拟网络故障）。

    「按顺序吐」决定了它**只能配合 workers=1**：并发时响应落到哪个请求是不确定的。
    要验证并发行为，用下面的 EchoSession。
    """

    def __init__(self, responses: list) -> None:
        self.responses = list(responses)
        self.calls: list[dict] = []

    def post(self, url, headers=None, json=None, timeout=None):  # noqa: A002 - 与 requests 参数名保持一致
        self.calls.append({"url": url, "headers": headers, "payload": json, "timeout": timeout})
        item = self.responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


class EchoSession:
    """**线程安全**的回声会话：按请求体本身生成响应，不依赖调用顺序。

    纯文本条目是数字字符串，向量首元素就回这个数字；图文条目统一回 99。
    这样并发时无论谁先完成，都能校验「结果是否按输入顺序归位」。
    """

    def __init__(self) -> None:
        self.sizes: list[int] = []
        self._lock = threading.Lock()

    def post(self, url, headers=None, json=None, timeout=None):  # noqa: A002
        items = json["input"]
        with self._lock:
            self.sizes.append(len(items))
        data = []
        for position, item in enumerate(items):
            # make_block 的 text 是「标题 + 空行 + 正文」，取最后一段就是正文里的数字
            value = 99.0 if isinstance(item, dict) else float(item.rsplit(chr(10) * 2, 1)[-1])
            data.append({"index": position, "embedding": [value] + [0.0] * (DEFAULT_DIM - 1)})
        return FakeResponse(200, {"data": data, "usage": {"prompt_tokens": len(items) * 10, "cost": 0.0001}})


def no_sleep(_seconds: float) -> None:
    """重试测试里别真的等。"""


# ---------------------------------------------------------------- 基本流程


def test_batches_by_count_and_maps_by_index() -> None:
    """40 条、批大小 16 → 3 次请求（16/16/8），且乱序返回也要归位。"""
    texts = [f"第{i}段文本" for i in range(40)]
    session = FakeSession(
        [
            FakeResponse(200, embedding_response(16, reverse=True)),
            FakeResponse(200, embedding_response(16, reverse=True)),
            FakeResponse(200, embedding_response(8, reverse=True)),
        ]
    )

    result = embed_texts(texts, api_key="k", batch=16, session=session, sleep=no_sleep, workers=1)

    assert [len(call["payload"]["input"]) for call in session.calls] == [16, 16, 8]
    assert len(result.vectors) == 40
    # 假响应里第 i 条的向量首元素就是 i，所以能直接检查归位是否正确
    assert [v[0] for v in result.vectors] == [float(i % 16) for i in range(40)]
    assert result.usage.requests == 3
    assert result.usage.prompt_tokens == 400
    assert result.usage.cost_usd == pytest.approx(0.0003)


def test_batches_also_respect_char_limit() -> None:
    """一批的字符总量超过上限时要提前切批，避免请求体过大。"""
    texts = ["啊" * 600 for _ in range(5)]
    session = FakeSession(
        [
            FakeResponse(200, embedding_response(2)),
            FakeResponse(200, embedding_response(2)),
            FakeResponse(200, embedding_response(1)),
        ]
    )

    embed_texts(texts, api_key="k", batch=16, max_chars_per_request=1200, session=session, sleep=no_sleep, workers=1)

    assert [len(call["payload"]["input"]) for call in session.calls] == [2, 2, 1]


def test_parallel_batches_keep_input_order() -> None:
    """200 条、每批 64 → 4 个请求并发，但向量必须仍按输入顺序返回。"""
    texts = [str(index) for index in range(200)]
    session = EchoSession()
    seen: list[tuple[int, int]] = []

    result = embed_texts(
        texts,
        api_key="k",
        session=session,
        sleep=no_sleep,
        on_progress=lambda done, total: seen.append((done, total)),
    )

    assert sorted(session.sizes) == [8, 64, 64, 64]          # 64/64/64/8，顺序不定
    assert [int(vector[0]) for vector in result.vectors] == list(range(200))   # 归位正确
    assert result.usage.requests == 4
    assert result.usage.cost_usd == pytest.approx(0.0004)
    # 进度回调天然单调，最后一定是 (总数, 总数)
    assert seen[-1] == (200, 200)
    assert all(later[0] > earlier[0] for earlier, later in zip(seen, seen[1:]))


def test_parallel_pool_includes_image_blocks(tmp_path: Path) -> None:
    """带图的块一条一个请求，但和纯文本的批**进同一个并发池**。"""
    image = tmp_path / "images" / "b.jpg"
    image.parent.mkdir(parents=True)
    image.write_bytes(b"\xff\xd8\xff\xe0jpeg-bytes")
    blocks = [make_block(str(index)) for index in range(3)]
    blocks.append(make_block("带图块", images=(image,)))
    blocks.append(make_block("另一张图", images=(image,)))
    session = EchoSession()

    result = embed_blocks(blocks, api_key="k", session=session, sleep=no_sleep)

    assert sorted(session.sizes) == [1, 1, 3]               # 1 个文本批 + 2 个图片请求
    assert [int(vector[0]) for vector in result.vectors[:3]] == [0, 1, 2]   # 纯文本按序归位
    assert [vector[0] for vector in result.vectors[3:]] == [99.0, 99.0]     # 图片块的向量也在自己的位置
    assert result.usage.requests == 3


def test_payload_shape_and_query_input_type() -> None:
    session = FakeSession([FakeResponse(200, embedding_response(1, dim=1024))])

    embed_texts(["问题"], api_key="secret", dim=1024, input_type="search_query", session=session, sleep=no_sleep)

    payload = session.calls[0]["payload"]
    assert payload["model"] == "voyageai/voyage-multimodal-3.5"
    assert payload["dimensions"] == 1024
    assert payload["input_type"] == "search_query"
    assert payload["encoding_format"] == "float"
    assert session.calls[0]["headers"]["Authorization"] == "Bearer secret"


def test_empty_input_makes_no_request() -> None:
    session = FakeSession([])
    result = embed_texts([], api_key="k", session=session, sleep=no_sleep)

    assert result.vectors == []
    assert session.calls == []


def test_bad_dimension_rejected_before_request() -> None:
    session = FakeSession([])
    with pytest.raises(OpenRouterError, match="dimensions"):
        embed_texts(["x"], api_key="k", dim=999, session=session, sleep=no_sleep)
    assert session.calls == []


# ---------------------------------------------------------------- 错误处理


def test_dimension_mismatch_raises() -> None:
    """上游换了维度（比如忽略 dimensions 参数）时要立刻报错，不能默默存下错维度的向量。"""
    session = FakeSession([FakeResponse(200, embedding_response(1, dim=512))])
    with pytest.raises(OpenRouterError, match="向量维度不符"):
        embed_texts(["x"], api_key="k", dim=2048, session=session, sleep=no_sleep)


def test_count_mismatch_raises() -> None:
    session = FakeSession([FakeResponse(200, embedding_response(1))])
    with pytest.raises(OpenRouterError, match="条数不符"):
        embed_texts(["a", "b"], api_key="k", session=session, sleep=no_sleep)


def test_retries_on_429_then_succeeds() -> None:
    session = FakeSession(
        [
            FakeResponse(429, {"error": {"message": "rate limited"}}, headers={"Retry-After": "0"}),
            FakeResponse(200, embedding_response(1)),
        ]
    )
    result = embed_texts(["x"], api_key="k", session=session, sleep=no_sleep)

    assert len(session.calls) == 2
    assert result.usage.requests == 1, "只统计成功那次的用量"


def test_gives_up_after_max_retries() -> None:
    session = FakeSession([FakeResponse(503, "upstream down") for _ in range(3)])
    with pytest.raises(OpenRouterError, match="503"):
        embed_texts(["x"], api_key="k", max_retries=3, session=session, sleep=no_sleep)
    assert len(session.calls) == 3


def test_bad_request_is_not_retried() -> None:
    session = FakeSession([FakeResponse(400, {"error": {"message": "bad input"}})])
    with pytest.raises(OpenRouterError, match="400"):
        embed_texts(["x"], api_key="k", session=session, sleep=no_sleep)
    assert len(session.calls) == 1


def test_network_error_is_retried() -> None:
    session = FakeSession([requests.ConnectionError("boom"), FakeResponse(200, embedding_response(1))])
    embed_texts(["x"], api_key="k", session=session, sleep=no_sleep)
    assert len(session.calls) == 2


def test_error_inside_200_response_raises() -> None:
    session = FakeSession([FakeResponse(200, {"error": {"message": "model not found"}})])
    with pytest.raises(OpenRouterError, match="model not found"):
        embed_texts(["x"], api_key="k", session=session, sleep=no_sleep)


# ---------------------------------------------------------------- 图文混排


def test_image_block_goes_alone_as_data_uri(tmp_path: Path) -> None:
    """带图的块单独发一条请求，图片以 data URI 内联（接口不收本地路径）。"""
    image = tmp_path / "images" / "a.jpg"
    image.parent.mkdir(parents=True)
    image.write_bytes(b"\xff\xd8\xff\xe0jpeg-bytes")
    blocks = [make_block("纯文本块"), make_block("带图块", images=(image,))]

    session = FakeSession(
        [
            FakeResponse(200, embedding_response(1)),  # 纯文本那一批
            FakeResponse(200, {**embedding_response(1), "usage": {"prompt_tokens": 155, "cost": 0.00004, "prompt_tokens_details": {"text_tokens": 20, "image_tokens": 135}}}),
        ]
    )
    result = embed_blocks(blocks, api_key="k", session=session, sleep=no_sleep, workers=1)

    assert len(session.calls) == 2
    assert session.calls[0]["payload"]["input"] == [blocks[0].text]
    rich_item = session.calls[1]["payload"]["input"][0]
    assert rich_item["content"][0]["type"] == "text"
    assert rich_item["content"][1]["image_url"]["url"].startswith("data:image/jpeg;base64,")
    assert len(result.vectors) == 2
    assert result.usage.image_tokens == 135
    assert result.usage.cost_usd == pytest.approx(0.00014)


def test_image_only_block_omits_empty_text_part(tmp_path: Path) -> None:
    """纯图片块不能带空的 text 部分，只发 image_url。"""
    image = tmp_path / "images" / "only.jpg"
    image.parent.mkdir(parents=True)
    image.write_bytes(b"\xff\xd8\xff\xe0jpeg-bytes")
    block = Block(
        kind="image",
        heading="图片：images/only.jpg",
        level=0,
        body="",
        text="",
        start=0,
        end=0,
        images=(image,),
    )
    session = FakeSession([FakeResponse(200, embedding_response(1))])

    embed_blocks([block], api_key="k", session=session, sleep=no_sleep, workers=1)

    item = session.calls[0]["payload"]["input"][0]
    assert [part["type"] for part in item["content"]] == ["image_url"]


def test_progress_callback_sees_every_block() -> None:
    blocks = [make_block(f"块{i}") for i in range(5)]
    session = FakeSession([FakeResponse(200, embedding_response(5))])
    seen: list[tuple[int, int]] = []

    embed_blocks(blocks, api_key="k", session=session, sleep=no_sleep, on_progress=lambda d, t: seen.append((d, t)))

    assert seen == [(5, 5)]


# ---------------------------------------------------------------- 密钥


def test_api_key_prefers_environment(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text("OPENROUTER_API_KEY=from-file\n", encoding="utf-8")
    monkeypatch.setenv("OPENROUTER_API_KEY", "from-env")

    assert read_api_key(env_file=env_file) == "from-env"


def test_api_key_missing_raises(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    with pytest.raises(OpenRouterError, match="OPENROUTER_API_KEY"):
        read_api_key(env_file=tmp_path / "不存在.env")


def test_usage_addition() -> None:
    total = EmbedUsage(requests=1, prompt_tokens=10, cost_usd=0.5) + EmbedUsage(requests=2, image_tokens=3, cost_usd=0.25)
    assert (total.requests, total.prompt_tokens, total.image_tokens) == (3, 10, 3)
    assert total.cost_usd == pytest.approx(0.75)


# ---------------------------------------------------------------- 真调一次


def _live_key() -> str | None:
    try:
        return read_api_key()
    except OpenRouterError:
        return None


def rerank_response(scores: list[float], *, order: list[int] | None = None, usage: dict | None = None) -> dict:
    """造一个重排响应：分数按返回顺序给，`order` 可指定返回哪些文档下标（用来模拟乱序/越界）。"""
    indexes = order if order is not None else list(range(len(scores)))
    return {
        "id": "gen-rerank-test",
        "model": "voyageai/rerank-3",
        "results": [
            {"document": {"text": f"doc-{index}"}, "index": index, "relevance_score": scores[position]}
            for position, index in enumerate(indexes)
        ],
        "usage": usage if usage is not None else {"total_tokens": 150, "cost": 0.0000075},
    }


# ---------------------------------------------------------------- 重排


def test_rerank_sorts_by_relevance_and_maps_indexes() -> None:
    """上游乱序返回时，也要按分数从高到低排好，并保留原下标。"""
    session = FakeSession([FakeResponse(200, rerank_response([0.2, 0.9, 0.5], order=[0, 1, 2]))])

    result = rerank("问题", ["甲", "乙", "丙"], api_key="k", session=session, sleep=no_sleep)

    assert result.order == [1, 2, 0]
    assert result.scores == [0.9, 0.5, 0.2]
    assert result.top(3) == [1, 2, 0]
    assert result.requests == 1
    assert result.tokens == 150
    assert result.cost_usd == pytest.approx(0.0000075)


def test_rerank_payload_shape() -> None:
    session = FakeSession([FakeResponse(200, rerank_response([0.5, 0.4]))])

    rerank("什么是校徽？", ["文档一", "文档二"], api_key="secret", session=session, sleep=no_sleep)

    payload = session.calls[0]["payload"]
    assert payload["model"] == "voyageai/rerank-3"
    assert payload["query"] == "什么是校徽？"
    assert payload["documents"] == ["文档一", "文档二"], "纯文本文档直接传字符串"
    assert "top_n" not in payload, "不指定 top_n 时不要凭空塞参数"
    assert session.calls[0]["url"].endswith("/rerank")
    assert session.calls[0]["headers"]["Authorization"] == "Bearer secret"


def test_rerank_top_n_is_forwarded() -> None:
    session = FakeSession([FakeResponse(200, rerank_response([0.5, 0.4, 0.3]))])

    rerank("问题", ["甲", "乙", "丙"], api_key="k", top_n=2, session=session, sleep=no_sleep)

    assert session.calls[0]["payload"]["top_n"] == 2


def test_rerank_truncates_long_documents() -> None:
    session = FakeSession([FakeResponse(200, rerank_response([0.5]))])

    rerank("问题", ["很长的文档" * 1000], api_key="k", truncate_chars=50, session=session, sleep=no_sleep)

    sent = session.calls[0]["payload"]["documents"][0]
    assert len(sent) == 50


def test_rerank_empty_documents_makes_no_request() -> None:
    session = FakeSession([])

    result = rerank("问题", [], api_key="k", session=session, sleep=no_sleep)

    assert result.order == []
    assert result.requests == 0
    assert session.calls == []


def test_rerank_retries_on_429() -> None:
    session = FakeSession(
        [
            FakeResponse(429, {"error": {"message": "slow down"}}, headers={"Retry-After": "0"}),
            FakeResponse(200, rerank_response([0.7, 0.3])),
        ]
    )

    result = rerank("问题", ["甲", "乙"], api_key="k", session=session, sleep=no_sleep)

    assert len(session.calls) == 2
    assert result.order == [0, 1]


def test_rerank_rejects_bad_index() -> None:
    session = FakeSession([FakeResponse(200, rerank_response([0.5], order=[7]))])
    with pytest.raises(OpenRouterError, match="下标越界"):
        rerank("问题", ["甲"], api_key="k", session=session, sleep=no_sleep)


def test_rerank_rejects_duplicate_index() -> None:
    session = FakeSession(
        [FakeResponse(200, {"results": [{"index": 0, "relevance_score": 0.5}, {"index": 0, "relevance_score": 0.4}]})]
    )
    with pytest.raises(OpenRouterError, match="重复下标"):
        rerank("问题", ["甲", "乙"], api_key="k", session=session, sleep=no_sleep)


def test_rerank_empty_results_raise() -> None:
    session = FakeSession([FakeResponse(200, {"results": []})])
    with pytest.raises(OpenRouterError, match="没有返回任何结果"):
        rerank("问题", ["甲"], api_key="k", session=session, sleep=no_sleep)


def test_rerank_cost_falls_back_to_unit_price() -> None:
    """上游没回 cost 时，用 $0.05/M token 估算，保证报表里的费用列不会空着。"""
    session = FakeSession([FakeResponse(200, rerank_response([0.5], usage={"total_tokens": 200_000}))])

    result = rerank("问题", ["甲"], api_key="k", session=session, sleep=no_sleep)

    assert result.cost_usd == pytest.approx(0.01)


@pytest.mark.skipif(_live_key() is None, reason="未配置 OPENROUTER_API_KEY，跳过真实调用")
def test_live_rerank_puts_relevant_document_first() -> None:
    """真的花一次钱：确认重排能把明显相关的文档顶到第 1。"""
    documents = [
        "学校图书馆开放时间为每日 8:00 至 22:00。",
        "研究生国家奖学金奖励标准为博士研究生每生每年 3 万元，硕士研究生每生每年 2 万元。",
        "校徽为圆形，以五个相同单元的人字形图案围合成红棉花的虚形。",
    ]

    result = rerank("研究生国家奖学金的金额是多少？", documents, api_key=_live_key())

    assert result.order[0] == 1
    assert result.scores == sorted(result.scores, reverse=True)
    assert result.tokens > 0


@pytest.mark.skipif(_live_key() is None, reason="未配置 OPENROUTER_API_KEY，跳过真实调用")
def test_live_embed_single_text() -> None:
    """真的花一次钱：确认密钥有效、返回维度符合预期。"""
    result = embed_texts(["第八十九条 学校校庆日暨校友返校日为每年 11 月第二个星期六。"], api_key=_live_key())

    assert len(result.vectors) == 1
    assert len(result.vectors[0]) == DEFAULT_DIM
    assert result.usage.prompt_tokens > 0
    assert result.usage.cost_usd >= 0


# ---------------------------------------------------------------- 可配置接入点


def test_embed_uses_configured_base_url() -> None:
    session = FakeSession([FakeResponse(200, embedding_response(1))])

    embed_texts(["x"], api_key="k", base_url="https://embed.example.com/v1", session=session, sleep=no_sleep)

    assert session.calls[0]["url"] == "https://embed.example.com/v1/embeddings"


def test_rerank_uses_configured_base_url_and_tolerates_trailing_slash() -> None:
    session = FakeSession([FakeResponse(200, rerank_response([0.5]))])

    rerank("问题", ["甲"], api_key="k", base_url="https://rank.example.com/v1/", session=session, sleep=no_sleep)

    assert session.calls[0]["url"] == "https://rank.example.com/v1/rerank"


def test_image_block_also_uses_configured_base_url(tmp_path: Path) -> None:
    image = tmp_path / "a.jpg"
    image.write_bytes(b"\xff\xd8\xff\xe0jpeg")
    blocks = [make_block("带图", images=(image,))]
    session = FakeSession([FakeResponse(200, embedding_response(1))])

    embed_blocks(blocks, api_key="k", base_url="https://embed.example.com/v1", session=session, sleep=no_sleep)

    assert session.calls[0]["url"] == "https://embed.example.com/v1/embeddings"
