"""把 MinerU 产物里的 `full.md` 切成可嵌入的 Block。

分层
1. 标题切分：ATX 标题（``#``..``######``）把整篇切成 section，section 内维护标题面包屑；
   落在原子块里的 ``#`` 视为内容，不算标题。
2. 原子块：HTML 表格（chonkie 的 MarkdownChef 只认管道表格，不认 ``<table>``，本模块自己处理）、
   Markdown 管道表格、围栏代码块整体保留，不参与正文切分。
3. 正文收敛：其余文本交给 ``chonkie.RecursiveChunker``（中文标点分隔规则）压到 ``chunk_size`` 字符内，
   同一段文本里相邻的过小片段会先并起来。
4. 小块归并：正文不足 ``min_chunk_size`` 的孤立块（文件号、表题、表单残行）向前并入同文档的下一块，
   下一块不可并时再并入前一块，避免「34 个字的块」占满检索名额。
5. 图片归属：只保留 ``full.md`` 真正引用到的图片，按字符区间归入覆盖它的块。

偏移约定：``Block.start``/``end`` 是 ``full.md`` 中的字符区间（已去掉正文首尾空白）。原子块被拆成多块时
（超长表格按行拆），每一块都保留父区间。
"""

from __future__ import annotations

import hashlib
import re
import statistics
from dataclasses import dataclass, field, replace
from pathlib import Path

from chonkie import MarkdownChef, RecursiveChunker, TableChunker
from chonkie.types import RecursiveLevel, RecursiveRules

__all__ = ["Block", "ChunkResult", "chunk_markdown", "file_sha256", "KIND_TEXT", "KIND_TABLE", "KIND_CODE"]

KIND_TEXT = "text"
KIND_TABLE = "table"
KIND_CODE = "code"

HEADING_RE = re.compile(r"^(#{1,6})[ \t]+(\S.*?)[ \t]*$", re.MULTILINE)
HTML_TABLE_RE = re.compile(r"<table\b[^>]*>.*?</table\s*>", re.IGNORECASE | re.DOTALL)
MD_IMAGE_RE = re.compile(r"!\[[^\]]*\]\(\s*<?([^)\s>]+)")
TR_COUNT_RE = re.compile(r"<tr\b", re.IGNORECASE)

TR_RE = re.compile(r"<tr\b[^>]*>.*?</tr\s*>", re.IGNORECASE | re.DOTALL)

REMOTE_PREFIXES = ("http://", "https://", "data:", "//")

# chonkie 默认 min_characters_per_chunk=24：中文句子（甚至整条「（一）警告；」）短于 24 字时，
# 分隔符会被并成一片，句子级规则直接失效，最后退化成按字符硬切、句子被劈开。
# 这里设为 1 让「。」真正生效；碎片该不该并起来由本模块的 min_chunk_size 决定。
MIN_CHARS_PER_CHUNK = 1

# chonkie 的默认规则只认英文句末标点（". " / "! " / "? "），中文会一路跌到「按空格切 → 按字符硬切」。
CN_RULES = RecursiveRules(
    levels=[
        RecursiveLevel(delimiters=["\n\n", "\r\n", "\n", "\r"]),
        RecursiveLevel(delimiters=["。", "！", "？", "；", "…"]),
        RecursiveLevel(
            delimiters=[
                "，", "、", "：", "（", "）", "《", "》", "“", "”", "—",
                "(", ")", ",", ";", ":", "|",
            ],
        ),
        RecursiveLevel(whitespace=True),
        RecursiveLevel(),
    ],
)


@dataclass(frozen=True, slots=True)
class Block:
    """一个待嵌入单元。"""

    kind: str
    heading: str
    level: int
    body: str
    text: str
    start: int
    end: int
    images: tuple[Path, ...] = ()

    @property
    def char_count(self) -> int:
        return len(self.text)


@dataclass(slots=True)
class ChunkResult:
    blocks: list[Block]
    sha256: str
    warnings: list[str] = field(default_factory=list)
    stats: dict[str, float] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class _Section:
    level: int
    breadcrumb: str
    start: int
    end: int


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for piece in iter(lambda: handle.read(1 << 20), b""):
            digest.update(piece)
    return digest.hexdigest()


def _inside(spans: list[tuple[str, int, int]], pos: int) -> bool:
    return any(start <= pos < end for _, start, end in spans)


def _dedupe_spans(spans: list[tuple[str, int, int]]) -> list[tuple[str, int, int]]:
    """按起点排序，丢掉空区间与被更外层 span 完全包住的碎片。"""
    kept: list[tuple[str, int, int]] = []
    for kind, start, end in sorted(spans, key=lambda s: (s[1], -s[2])):
        if end <= start:
            continue
        if any(other_start <= start and end <= other_end for _, other_start, other_end in kept):
            continue
        kept.append((kind, start, end))
    return kept


def _sections(md: str, atomics: list[tuple[str, int, int]]) -> list[_Section]:
    """按标题切 section；标题行本身不计入正文（它已成为面包屑）。"""
    headings = [m for m in HEADING_RE.finditer(md) if not _inside(atomics, m.start())]
    sections: list[_Section] = []
    stack: list[tuple[int, str]] = []
    if headings and headings[0].start() > 0:
        sections.append(_Section(0, "", 0, headings[0].start()))
    for index, match in enumerate(headings):
        level = len(match.group(1))
        title = match.group(2).strip()
        while stack and stack[-1][0] >= level:
            stack.pop()
        stack.append((level, title))
        end = headings[index + 1].start() if index + 1 < len(headings) else len(md)
        sections.append(_Section(level, " > ".join(t for _, t in stack), match.end(), end))
    return sections


def _complement(start: int, end: int, spans: list[tuple[int, int]]) -> list[tuple[int, int]]:
    """[start, end) 里不属于任何 span 的区间。"""
    holes: list[tuple[int, int]] = []
    cursor = start
    for span_start, span_end in sorted(spans):
        if span_start > cursor:
            holes.append((cursor, min(span_start, end)))
        cursor = max(cursor, span_end)
    if cursor < end:
        holes.append((cursor, end))
    return [(s, e) for s, e in holes if e > s]


def _merge_small(
    spans: list[tuple[int, int]], *, chunk_size: int, min_chunk_size: int
) -> list[tuple[int, int]]:
    """把同一段文本里相邻的过小片段并起来，避免产出「一句话一个块」的噪声。"""
    merged: list[tuple[int, int]] = []
    for start, end in spans:
        if merged:
            prev_start, prev_end = merged[-1]
            prev_len, cur_len = prev_end - prev_start, end - start
            gap = start - prev_end
            if (
                0 <= gap <= 40
                and prev_len + gap + cur_len <= chunk_size
                and (prev_len < min_chunk_size or cur_len < min_chunk_size)
            ):
                merged[-1] = (prev_start, end)
                continue
        merged.append((start, end))
    return merged


def _split_text(
    text: str, *, chunker: RecursiveChunker, chunk_size: int, min_chunk_size: int
) -> list[tuple[int, int]]:
    """返回相对 ``text`` 的字符区间。"""
    if not text.strip():
        return []
    spans = [(c.start_index, c.end_index) for c in chunker.chunk(text)]
    spans = [(s, e) for s, e in spans if text[s:e].strip()]
    return _merge_small(spans, chunk_size=chunk_size, min_chunk_size=min_chunk_size)


def _html_table_spans(md: str) -> list[tuple[str, int, int]]:
    return [(KIND_TABLE, m.start(), m.end()) for m in HTML_TABLE_RE.finditer(md)]


def _split_html_table_rows(text: str, *, max_chars: int) -> list[str]:
    """按 ``<tr>`` 拆 HTML 表格，首行当作表头在每片重复。

    chonkie 的 TableChunker 走 HTML 分支时把「第一个 ``<tr>`` 之前的内容」当表头，
    MinerU 产出的 ``<table><tr>…``（无 ``<thead>``/``<tbody>``）会被判成「无表头」，
    于是第 2 片起丢掉表头行。这里自己按行打包，保证每片都带表头。
    """
    rows = [m.group(0) for m in TR_RE.finditer(text)]
    if len(rows) < 2:
        return []
    first_at = text.find(rows[0])
    prefix = text[:first_at]
    suffix = text[text.rfind(rows[-1]) + len(rows[-1]) :]
    header, body_rows = rows[0], rows[1:]

    base_len = len(prefix) + len(header) + len(suffix)
    parts: list[str] = []
    current: list[str] = []
    current_len = base_len
    for row in body_rows:
        if current and current_len + len(row) > max_chars:
            parts.append(prefix + header + "".join(current) + suffix)
            current, current_len = [], base_len
        current.append(row)
        current_len += len(row)
    if current:
        parts.append(prefix + header + "".join(current) + suffix)
    return parts if len(parts) > 1 else []


def _split_atomic(
    span_text: str, kind: str, *, max_chars: int, label: str, warnings: list[str]
) -> list[str]:
    """原子块超长时才拆：表格按行拆（表头在每片重复），其余整体保留。"""
    if len(span_text) <= max_chars:
        return [span_text]

    if kind == KIND_TABLE:
        parts = _split_html_table_rows(span_text, max_chars=max_chars)
        if not parts:
            row_count = max(1, len(TR_COUNT_RE.findall(span_text)) or span_text.count("\n"))
            rows_per_chunk = max(1, int(max_chars / max(1.0, len(span_text) / row_count)))
            parts = [c.text for c in TableChunker(tokenizer="row", chunk_size=rows_per_chunk).chunk(span_text)]
        if parts:
            warnings.append(
                f"{label}: 表格 {len(span_text)} 字符 > {max_chars}，按行拆成 {len(parts)} 片"
                f"（每片重复表头，共享父区间）"
            )
            return parts
        warnings.append(f"{label}: 表格 {len(span_text)} 字符无法按行拆分，整体保留")
    else:
        warnings.append(f"{label}: {kind} 块 {len(span_text)} 字符 > {max_chars}，整体保留")
    return [span_text]


def _resolve_images(
    md: str, full_md: Path, *, image_max_bytes: int, warnings: list[str]
) -> list[tuple[int, Path]]:
    """``full.md`` 里引用到的本地图片（去重，按首次出现位置）。"""
    resolved: list[tuple[int, Path]] = []
    seen: set[Path] = set()
    for match in MD_IMAGE_RE.finditer(md):
        src = match.group(1).split("#", 1)[0].split("?", 1)[0]
        if src.startswith(REMOTE_PREFIXES):
            continue
        target = (full_md.parent / src).resolve()
        if target in seen:
            continue
        if not target.is_file():
            warnings.append(f"图片不存在，跳过: {src}")
            continue
        size = target.stat().st_size
        if size > image_max_bytes:
            warnings.append(f"图片 {size / 1048576:.1f}MB 超过上限 {image_max_bytes / 1048576:.0f}MB，跳过: {src}")
            continue
        seen.add(target)
        resolved.append((match.start(), target))
    return resolved


def _block_index_for(blocks: list[Block], offset: int) -> int | None:
    """覆盖 ``offset`` 的块；落在块之间时归给前一个块。"""
    if not blocks:
        return None
    for index, block in enumerate(blocks):
        if block.start <= offset < block.end:
            return index
    for index in range(len(blocks) - 1, -1, -1):
        if blocks[index].end <= offset:
            return index
    return 0


def _doc_of(heading: str) -> str:
    return heading.split(" > ", 1)[0]


def _join(first: Block, second: Block) -> Block:
    """把 ``second`` 并进 ``first``：区间取并集，标题取层级更深的一方，块类型取更具体的一方。"""
    heading, level = (
        (second.heading, second.level) if second.level >= first.level else (first.heading, first.level)
    )
    body = f"{first.body}\n\n{second.body}"
    return Block(
        kind=_kind_of(first.kind, second.kind),
        heading=heading,
        level=level,
        body=body,
        text=f"{heading}\n\n{body}" if heading else body,
        start=first.start,
        end=second.end,
        images=first.images + second.images,
    )


def _kind_of(first: str, second: str) -> str:
    """合并后保留更具体的类型：text < table/code。"""
    if first == second or second == KIND_TEXT:
        return first
    if first == KIND_TEXT:
        return second
    return second


def _mergeable(
    first: Block, second: Block, *, chunk_size: int, max_table_chars: int, max_gap: int
) -> bool:
    limit = max_table_chars if KIND_TABLE in (first.kind, second.kind) else chunk_size
    return (
        _doc_of(first.heading) == _doc_of(second.heading)
        and 0 <= second.start - first.end <= max_gap
        and first.char_count + second.char_count <= limit
    )


def _merge_tiny_blocks(
    blocks: list[Block],
    *,
    chunk_size: int,
    min_chunk_size: int,
    max_table_chars: int,
    max_gap: int = 80,
) -> tuple[list[Block], int]:
    """正文过短的孤立块先并入同文档的下一块，仍孤立则并入前一块。"""
    merged_count = 0

    forward: list[Block] = []
    index = 0
    while index < len(blocks):
        block = blocks[index]
        following = blocks[index + 1] if index + 1 < len(blocks) else None
        if (
            block.kind == KIND_TEXT
            and len(block.body) < min_chunk_size
            and following is not None
            and _mergeable(block, following, chunk_size=chunk_size, max_table_chars=max_table_chars, max_gap=max_gap)
        ):
            forward.append(_join(block, following))
            merged_count += 1
            index += 2
            continue
        forward.append(block)
        index += 1

    backward: list[Block] = []
    for block in forward:
        if (
            backward
            and block.kind == KIND_TEXT
            and len(block.body) < min_chunk_size
            and _mergeable(
                backward[-1], block, chunk_size=chunk_size, max_table_chars=max_table_chars, max_gap=max_gap
            )
        ):
            backward[-1] = _join(backward[-1], block)
            merged_count += 1
            continue
        backward.append(block)
    return backward, merged_count


def chunk_markdown(
    full_md: Path,
    *,
    chunk_size: int = 1000,
    min_chunk_size: int = 120,
    max_table_chars: int = 2000,
    max_images_per_block: int = 12,
    image_max_bytes: int = 4 * 1024 * 1024,
) -> ChunkResult:
    """把 ``full_md`` 切成分块结果。

    Args:
        full_md: MinerU 产物目录里的 ``full.md``。
        chunk_size: 正文块的字符上限（character tokenizer，中文即字数）。
        min_chunk_size: 正文短于它的块会被并入相邻块；它也是同一段文本内片段的合并下限。
        max_table_chars: 表格/代码块超过它才拆（表格按行拆）。
        max_images_per_block: 单块附图上限，超出的丢弃并记警告。
            取 12 是因为实测语料里 subfigure 多的论文单块最多 9 张；太小会静默丢图，
            太大会让单次嵌入请求体变肥（每张图都要内联成 data URI）。
        image_max_bytes: 单图体积上限。
    """
    full_md = Path(full_md)
    md = full_md.read_text(encoding="utf-8")
    warnings: list[str] = []

    doc = MarkdownChef(tokenizer="character").parse(md)
    atomics = _dedupe_spans(
        _html_table_spans(md)
        + [(KIND_TABLE, t.start_index, t.end_index) for t in doc.tables]
        + [(KIND_CODE, c.start_index, c.end_index) for c in doc.code]
    )
    sections = _sections(md, atomics)
    images = _resolve_images(md, full_md, image_max_bytes=image_max_bytes, warnings=warnings)
    chunker = RecursiveChunker(
        tokenizer="character",
        chunk_size=chunk_size,
        rules=CN_RULES,
        min_characters_per_chunk=MIN_CHARS_PER_CHUNK,
    )

    blocks: list[Block] = []
    for section in sections:
        local = [(k, s, e) for k, s, e in atomics if s >= section.start and e <= section.end]
        pieces: list[tuple[str, int, int]] = list(local)
        for hole_start, hole_end in _complement(section.start, section.end, [(s, e) for _, s, e in local]):
            for start, end in _split_text(
                md[hole_start:hole_end], chunker=chunker, chunk_size=chunk_size, min_chunk_size=min_chunk_size
            ):
                pieces.append((KIND_TEXT, hole_start + start, hole_start + end))
        pieces.sort(key=lambda p: (p[1], p[2]))

        label = section.breadcrumb or "(前言)"
        for kind, span_start, span_end in pieces:
            raw = md[span_start:span_end]
            if kind == KIND_TEXT:
                if not raw.strip():
                    continue
                leading = len(raw) - len(raw.lstrip())
                trailing = len(raw) - len(raw.rstrip())
                start, end = span_start + leading, span_end - trailing
                parts = [md[start:end]]
            else:
                start, end = span_start, span_end
                parts = _split_atomic(raw, kind, max_chars=max_table_chars, label=label, warnings=warnings)
            for part in parts:
                if not part.strip():
                    continue
                blocks.append(
                    Block(
                        kind=kind,
                        heading=section.breadcrumb,
                        level=section.level,
                        body=part,
                        text=f"{section.breadcrumb}\n\n{part}" if section.breadcrumb else part,
                        start=start,
                        end=end,
                    )
                )

    blocks, merged_count = _merge_tiny_blocks(
        blocks,
        chunk_size=chunk_size,
        min_chunk_size=min_chunk_size,
        max_table_chars=max_table_chars,
    )
    left_tiny = [b for b in blocks if b.kind == KIND_TEXT and len(b.body) < min_chunk_size]
    if left_tiny:
        warnings.append(
            f"仍有 {len(left_tiny)} 个正文块短于 {min_chunk_size} 字符（无同文档相邻块可并）："
            + "、".join(f"[{b.start},{b.end}){b.body[:18]!r}" for b in left_tiny[:5])
        )

    # 图片归属：按字符区间落到覆盖它的块（区间之外归前一个块）
    attached: dict[int, list[Path]] = {}
    dropped = 0
    for offset, image in images:
        index = _block_index_for(blocks, offset)
        if index is None:
            continue
        bucket = attached.setdefault(index, [])
        if len(bucket) >= max_images_per_block:
            dropped += 1
            warnings.append(f"{image.name}: 所在块附图已达上限 {max_images_per_block}，丢弃")
            continue
        bucket.append(image)
    for index, bucket in attached.items():
        blocks[index] = replace(blocks[index], images=tuple(bucket))

    chars = sorted(b.char_count for b in blocks)
    stats: dict[str, float] = {
        "sections": float(len(sections)),
        "atomic_spans": float(len(atomics)),
        "blocks": float(len(blocks)),
        "blocks_text": float(sum(1 for b in blocks if b.kind == KIND_TEXT)),
        "blocks_table": float(sum(1 for b in blocks if b.kind == KIND_TABLE)),
        "blocks_code": float(sum(1 for b in blocks if b.kind == KIND_CODE)),
        "blocks_merged": float(merged_count),
        "blocks_tiny_left": float(len(left_tiny)),
        "images_referenced": float(len(images)),
        "images_attached": float(sum(len(b.images) for b in blocks)),
        "images_dropped": float(dropped),
        "warnings": float(len(warnings)),
    }
    if chars:
        stats.update(
            chars_min=float(chars[0]),
            chars_median=float(statistics.median(chars)),
            chars_p90=float(chars[int(0.9 * (len(chars) - 1))]),
            chars_max=float(chars[-1]),
            chars_total=float(sum(chars)),
        )

    return ChunkResult(blocks=blocks, sha256=file_sha256(full_md), warnings=warnings, stats=stats)
