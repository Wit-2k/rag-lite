r"""分块逻辑的回归测试。

    cd backend && uv run pytest -q
    # 只跑合成用例（不读 D:\MinerU Outputs）
    uv run pytest -q -k "not real_corpus"
"""

from __future__ import annotations

import hashlib
import re
from pathlib import Path

import pytest

from app.core.chunking import (
    KIND_CODE,
    KIND_IMAGE,
    KIND_TABLE,
    KIND_TEXT,
    chunk_images,
    chunk_markdown,
    file_sha256,
)

CORPUS = Path(r"D:\MinerU Outputs")
REAL_DOCS = sorted(p / "full.md" for p in CORPUS.iterdir() if (p / "full.md").is_file()) if CORPUS.is_dir() else []

HEADING_LINE_RE = re.compile(r"^#{1,6}[ \t]+\S.*$", re.MULTILINE)
TABLE = "<table><tr><td>类型</td><td>金额</td></tr><tr><td>一等</td><td>100</td></tr></table>"
# 长于 min_chunk_size(120) 的正文，用于「不该触发小块归并」的结构用例
LONG = "本条规定了研究生的权利与义务，学校应当依法依规办事，具体办法由研究生院负责解释。" * 3


def write_doc(tmp_path: Path, text: str, images: dict[str, bytes] | None = None) -> Path:
    root = tmp_path / "doc"
    root.mkdir(exist_ok=True)
    for name, payload in (images or {}).items():
        target = root / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(payload)
    md = root / "full.md"
    md.write_text(text, encoding="utf-8")
    return md


def kinds(blocks) -> list[str]:
    return [b.kind for b in blocks]


# ---------------------------------------------------------------- 纯图片目录


def test_chunk_images_makes_one_empty_text_block_per_image(tmp_path: Path) -> None:
    root = tmp_path / "纯图片"
    (root / "images" / "sub").mkdir(parents=True)
    (root / "images" / "a.jpg").write_bytes(b"jpeg-a")
    (root / "images" / "sub" / "b.png").write_bytes(b"png-b")
    (root / "images" / "ignore.txt").write_text("not an image", encoding="utf-8")

    result = chunk_images(root)

    assert len(result.blocks) == 2
    assert all(block.kind == KIND_IMAGE for block in result.blocks)
    assert all(block.text == "" and block.body == "" and block.char_count == 0 for block in result.blocks)
    assert [block.images[0].name for block in result.blocks] == ["a.jpg", "b.png"]
    assert result.blocks[0].heading.startswith("图片：images/")


def test_chunk_images_scans_flat_directory_when_no_images_subdir(tmp_path: Path) -> None:
    """没有 images/ 子目录时，源目录本身就是图片目录。"""
    root = tmp_path / "扁平图片"
    root.mkdir()
    (root / "a.jpg").write_bytes(b"jpeg-a")
    (root / "b.webp").write_bytes(b"webp-b")

    result = chunk_images(root)

    assert len(result.blocks) == 2
    assert all(block.kind == KIND_IMAGE for block in result.blocks)
    assert [block.images[0].name for block in result.blocks] == ["a.jpg", "b.webp"]


def test_chunk_images_fingerprint_changes_with_content(tmp_path: Path) -> None:
    root = tmp_path / "纯图片"
    (root / "images").mkdir(parents=True)
    image = root / "images" / "a.jpg"
    image.write_bytes(b"first")
    before = chunk_images(root).sha256

    image.write_bytes(b"second")

    assert chunk_images(root).sha256 != before


def test_chunk_images_requires_at_least_one_image(tmp_path: Path) -> None:
    root = tmp_path / "空目录"
    root.mkdir()

    with pytest.raises(FileNotFoundError, match="full.md"):
        chunk_images(root)


# ---------------------------------------------------------------- 结构与标题


def test_preamble_and_heading_breadcrumb(tmp_path: Path) -> None:
    md = write_doc(tmp_path, f"封面文字\n\n# 甲\n\n{LONG}\n\n## 乙\n\n{LONG}后半段。\n")
    result = chunk_markdown(md)

    assert [b.level for b in result.blocks] == [0, 1, 2]
    assert [b.heading for b in result.blocks] == ["", "甲", "甲 > 乙"]
    assert result.blocks[0].body == "封面文字"
    assert "甲" not in result.blocks[1].body  # 标题行只进面包屑，不进正文
    assert result.blocks[1].text.startswith("甲\n\n")
    assert result.blocks[2].text.startswith("甲 > 乙\n\n")


def test_heading_inside_html_table_is_content(tmp_path: Path) -> None:
    table = "<table><tr><td># 不是标题</td><td>1</td></tr><tr><td>x</td><td>2</td></tr></table>"
    md = write_doc(tmp_path, f"# 甲\n\n{LONG}前文。\n\n{table}\n\n{LONG}后文。\n")
    result = chunk_markdown(md)

    assert [b.heading for b in result.blocks] == ["甲", "甲", "甲"]
    table_blocks = [b for b in result.blocks if b.kind == KIND_TABLE]
    assert len(table_blocks) == 1
    assert table_blocks[0].body == table


def test_pipe_table_and_fenced_code_are_atomic(tmp_path: Path) -> None:
    md = write_doc(
        tmp_path,
        f"# 甲\n\n{LONG}前文。\n\n| a | b |\n| --- | --- |\n| 1 | 2 |\n\n"
        f"```python\nprint(1)\n```\n\n{LONG}后文。\n",
    )
    result = chunk_markdown(md)

    assert kinds(result.blocks) == [KIND_TEXT, KIND_TABLE, KIND_CODE, KIND_TEXT]
    assert result.blocks[1].body.startswith("| a | b |")
    assert "print(1)" in result.blocks[2].body


# ---------------------------------------------------------------- 尺寸约束


def test_text_blocks_respect_chunk_size_and_sentence_boundary(tmp_path: Path) -> None:
    """短中文句子也必须按「。」切，而不是按字符数硬切。"""
    text = "".join(f"第{i}句内容。" for i in range(503))
    md = write_doc(tmp_path, f"# 甲\n\n{text}\n")
    result = chunk_markdown(md, chunk_size=300)

    text_blocks = [b for b in result.blocks if b.kind == KIND_TEXT]
    assert len(text_blocks) > 1
    assert all(len(b.body) <= 300 for b in text_blocks)
    assert all(b.body.endswith("。") for b in text_blocks), "块尾必须是句号"


def test_oversized_table_splits_by_rows_keeping_header(tmp_path: Path) -> None:
    rows = "".join(f"<tr><td>行{i}</td><td>{'值' * 60}</td></tr>" for i in range(40))
    table = f"<table><tr><td>标记表头</td><td>数值</td></tr>{rows}</table>"
    md = write_doc(tmp_path, f"# 甲\n\n{table}\n")
    result = chunk_markdown(md, max_table_chars=400)

    table_blocks = [b for b in result.blocks if b.kind == KIND_TABLE]
    assert len(table_blocks) > 1
    assert all("标记表头" in b.body for b in table_blocks), "每片都要带表头"
    assert all(b.body.startswith("<table>") for b in table_blocks)
    assert all(b.start == table_blocks[0].start for b in table_blocks), "分片共享父区间"
    assert any("按行拆" in w for w in result.warnings)
    # 行不丢：所有行文本都能在分片里找到
    joined = "".join(b.body for b in table_blocks)
    assert all(f"行{i}" in joined for i in range(40))


# ---------------------------------------------------------------- 小块归并


def test_tiny_caption_merges_into_following_table(tmp_path: Path) -> None:
    md = write_doc(tmp_path, f"# 甲\n\n## 乙\n\n第九条 助学金标准：\n\n{TABLE}\n")
    result = chunk_markdown(md)

    assert len(result.blocks) == 1
    block = result.blocks[0]
    assert block.kind == KIND_TABLE, "并入表格后类型不能退化成 text"
    assert block.body.startswith("第九条 助学金标准：")
    assert TABLE in block.body
    assert block.heading == "甲 > 乙"


def test_tiny_docnumber_merges_into_next_section(tmp_path: Path) -> None:
    md = write_doc(tmp_path, "# 甲\n\n广工大规字〔2017〕28 号\n\n## 乙\n\n第一条 为了规范管理，制定本规定。\n")
    result = chunk_markdown(md)

    assert len(result.blocks) == 1
    assert result.blocks[0].heading == "甲 > 乙"
    assert "广工大规字" in result.blocks[0].body
    assert "第一条" in result.blocks[0].body
    assert result.stats["blocks_merged"] == 1


def test_tiny_block_does_not_cross_document(tmp_path: Path) -> None:
    """不同一级标题下的孤立小块不互相合并。"""
    md = write_doc(tmp_path, "# 甲\n\n短。\n\n# 乙\n\n另一个短。\n")
    result = chunk_markdown(md)

    assert [b.heading for b in result.blocks] == ["甲", "乙"]
    assert len(result.blocks) == 2


def test_tiny_blocks_are_reported(tmp_path: Path) -> None:
    md = write_doc(tmp_path, "# 甲\n\n短。\n")
    result = chunk_markdown(md)

    assert result.stats["blocks_tiny_left"] == 1
    assert any("短于" in w for w in result.warnings)


# ---------------------------------------------------------------- 图片


def test_images_are_attached_to_covering_block(tmp_path: Path) -> None:
    md = write_doc(
        tmp_path,
        f"# 甲\n\n![](images/a.jpg)\n\n{LONG}正文一。\n\n## 乙\n\n{LONG}正文二。\n\n"
        "![](images/b.jpg)\n\n![缺失](images/缺.jpg)\n\n![远程](https://example.com/c.png)\n",
        images={"images/a.jpg": b"A" * 10, "images/b.jpg": b"B" * 10, "images/没用上.jpg": b"C" * 10},
    )
    result = chunk_markdown(md)

    attached = [p.name for b in result.blocks for p in b.images]
    assert attached == ["a.jpg", "b.jpg"], "只挂 full.md 真正引用的图，且各归各块"
    assert any("图片不存在" in w for w in result.warnings)
    assert not any("example.com" in w for w in result.warnings), "远程图直接忽略，不算异常"


def test_image_limits(tmp_path: Path) -> None:
    images = {"images/big.jpg": b"X" * 5000}
    refs = []
    for index in range(3):
        name = f"images/s{index}.jpg"
        images[name] = b"Y"
        refs.append(f"![]( {name} )")
    md = write_doc(tmp_path, f"# 甲\n\n![大图](images/big.jpg)\n\n" + "\n\n".join(refs) + f"\n\n{LONG}\n", images=images)
    result = chunk_markdown(md, max_images_per_block=2, image_max_bytes=1000)

    assert result.stats["images_referenced"] == 3, "超上限的图不参与统计"
    assert result.stats["images_attached"] == 2
    assert result.stats["images_dropped"] == 1
    assert any("超过上限" in w for w in result.warnings)
    assert any("附图已达上限" in w for w in result.warnings)


# ---------------------------------------------------------------- 文档级不变量


def _check_doc_invariants(md_path: Path, *, chunk_size: int = 1000) -> None:
    md = md_path.read_text(encoding="utf-8")
    result = chunk_markdown(md_path, chunk_size=chunk_size)

    assert result.sha256 == hashlib.sha256(md_path.read_bytes()).hexdigest()
    assert file_sha256(md_path) == result.sha256
    assert result.stats["blocks"] == len(result.blocks)
    assert not [b for b in result.blocks if not b.body.strip()]
    assert all(0 <= b.start < b.end <= len(md) for b in result.blocks), "区间必须落在原文内"

    # 顺序且不重叠
    for prev, cur in zip(result.blocks, result.blocks[1:]):
        assert cur.start >= prev.end, f"块区间重叠: {prev.start}-{prev.end} 与 {cur.start}-{cur.end}"

    for block in result.blocks:
        # 正文块不超上限（表格/代码原子块允许超）
        if block.kind == KIND_TEXT:
            assert len(block.body) <= chunk_size, f"正文块超限 {len(block.body)}: {block.body[:40]!r}"
        # 面包屑段数不超过层级（文档以 h2 开头时首段可能缺 h1）
        assert len(block.heading.split(" > ")) <= block.level or (block.level == 0 and not block.heading)
        if block.heading:
            assert block.text.startswith(f"{block.heading}\n\n")
            assert block.text.endswith(block.body)

    # 无内容丢失：块之间的空隙只允许是标题行和空白
    gaps: list[str] = []
    cursor = 0
    for block in result.blocks:
        gaps.append(md[cursor : block.start])
        cursor = block.end
    gaps.append(md[cursor:])
    for gap in gaps:
        leftover = HEADING_LINE_RE.sub("", gap).strip()
        assert leftover == "", f"块之外还有非标题内容: {leftover[:80]!r}"

    # 每个 HTML 表格完整落在某个表格块内
    for match in re.finditer(r"<table\b[^>]*>.*?</table\s*>", md, re.IGNORECASE | re.DOTALL):
        piece = match.group(0)
        assert any(b.kind == KIND_TABLE and piece in b.body for b in result.blocks), (
            f"表格未成块: [{match.start()},{match.end()})"
        )

    # full.md 引用的每张本地图片都被挂上（去重后各一次）
    refs = {
        (md_path.parent / m.group(1)).resolve()
        for m in re.finditer(r"!\[[^\]]*\]\(\s*<?([^)\s>]+)", md)
        if not m.group(1).startswith(("http://", "https://", "data:", "//"))
    }
    existing = {p for p in refs if p.is_file() and p.stat().st_size <= 4 * 1024 * 1024}
    attached = {p for b in result.blocks for p in b.images}
    # 挂上的图必须都真实存在（不会凭空多出来）
    assert attached <= existing
    # 一个都不能凭空消失：引用 = 挂上 + 因「每块附图上限」被丢弃（丢弃时逐张记 warning）
    assert result.stats["images_referenced"] == len(existing)
    assert result.stats["images_attached"] == len(attached)
    assert result.stats["images_attached"] + result.stats["images_dropped"] == result.stats["images_referenced"]


def test_synthetic_doc_invariants(tmp_path: Path) -> None:
    md = write_doc(
        tmp_path,
        "# 甲\n\n广工大规字〔2017〕28 号\n\n第一段正文。\n\n"
        f"{TABLE}\n\n## 乙\n\n![](images/a.jpg)\n\n第二段正文。\n\n| a | b |\n| --- | --- |\n| 1 | 2 |\n",
        images={"images/a.jpg": b"A" * 32},
    )
    _check_doc_invariants(md, chunk_size=1000)


@pytest.mark.parametrize("full_md", REAL_DOCS, ids=lambda p: p.parent.name)
def test_real_corpus_invariants(full_md: Path) -> None:
    _check_doc_invariants(full_md, chunk_size=1000)
