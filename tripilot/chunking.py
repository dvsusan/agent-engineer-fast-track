"""Choose document-aware chunking strategies for TripPilot sources."""

import json
import re
from dataclasses import dataclass
from pathlib import Path

from .rag import Document, load_documents


# MIN_CHUNK_SIZE 防止演示把文档切得过碎。
MIN_CHUNK_SIZE = 80


# ChunkingError 表示文档结构或切分参数无法安全处理。
class ChunkingError(ValueError):
    """The document cannot be chunked with the requested settings."""


# SourceChunk 保存一个带来源、策略和边界信息的片段。
@dataclass(frozen=True)
class SourceChunk:
    # chunk_id 是资料片段唯一编号。
    chunk_id: str
    # doc_id 是片段所属文档。
    doc_id: str
    # strategy 是实际采用的切分策略。
    strategy: str
    # content 是片段正文。
    content: str
    # metadata 保存章节、地标或字符范围等结构信息。
    metadata: dict

    # 把片段转换为可写入 JSON 的字典。
    def to_dict(self):
        return {
            "id": self.chunk_id,
            "doc_id": self.doc_id,
            "strategy": self.strategy,
            "content": self.content,
            "metadata": self.metadata,
        }


# 按 Markdown 二级标题切分攻略类文档。
def chunk_by_sections(document, max_chars=260):
    # sections 保存“二级标题 + 正文”的完整段落。
    sections = []
    # heading 是当前二级标题。
    heading = ""
    # body_lines 保存当前章节正文。
    body_lines = []
    # line 是当前 Markdown 行。
    for line in document.text.splitlines():
        if line.startswith("## "):
            if heading:
                sections.append((heading, "\n".join(body_lines).strip()))
            heading = line.removeprefix("## ").strip()
            body_lines = []
        elif line.startswith("# "):
            continue
        elif heading:
            body_lines.append(line)
    if heading:
        sections.append((heading, "\n".join(body_lines).strip()))

    # chunks 保存按章节生成的结构化片段。
    chunks = []
    # index 是当前文档内的片段序号。
    for index, (section_heading, body) in enumerate(sections, start=1):
        content = f"{document.title}\n## {section_heading}\n{body}".strip()
        if len(content) > max_chars:
            raise ChunkingError(
                f"{document.doc_id} 的章节“{section_heading}”"
                f"超过 {max_chars} 字符"
            )
        chunks.append(
            SourceChunk(
                chunk_id=f"{document.doc_id}-{index:02d}",
                doc_id=document.doc_id,
                strategy="markdown_section",
                content=content,
                metadata={"section": section_heading},
            )
        )
    return chunks


# 地图说明按一个地标一个片段切分。
def chunk_map_notes(document, max_chars=260):
    # section_chunks 先按标题和正文边界切分。
    section_chunks = chunk_by_sections(document, max_chars=max_chars)
    # chunks 把每个章节转换成地标级片段。
    chunks = []
    # chunk 是当前地标章节。
    for chunk in section_chunks:
        chunks.append(
            SourceChunk(
                chunk_id=chunk.chunk_id,
                doc_id=chunk.doc_id,
                strategy="map_landmark",
                content=chunk.content,
                metadata={"landmark": chunk.metadata["section"]},
            )
        )
    return chunks


# 把 Markdown 表格逐行切成片段，并让每块重复表头。
def chunk_table_rows(document):
    # table_lines 只保留 Markdown 表格行。
    table_lines = [
        line.strip()
        for line in document.text.splitlines()
        if line.strip().startswith("|")
    ]
    if len(table_lines) < 3:
        raise ChunkingError(f"表格内容不足：{document.doc_id}")

    # header 是表格列名，separator 是 Markdown 对齐行。
    header = table_lines[0]
    separator = table_lines[1]
    # rows 是真正需要分别检索的数据行。
    rows = table_lines[2:]
    # chunks 保存每个表格行对应的完整上下文。
    chunks = []
    # index 是表格数据行序号。
    for index, row in enumerate(rows, start=1):
        content = f"{document.title}\n{header}\n{row}"
        chunks.append(
            SourceChunk(
                chunk_id=f"{document.doc_id}-{index:02d}",
                doc_id=document.doc_id,
                strategy="table_row",
                content=content,
                metadata={
                    "row": index,
                    "header_preserved": True,
                },
            )
        )
    return chunks, header, separator, len(rows)


# 对长文档使用带重叠字符窗口，避免边界信息完全丢失。
def chunk_long_text(document, max_chars=180, overlap_chars=30):
    if max_chars < MIN_CHUNK_SIZE:
        raise ChunkingError(f"片段长度不能小于 {MIN_CHUNK_SIZE}")
    if not 0 <= overlap_chars < max_chars:
        raise ChunkingError("重叠长度必须小于片段长度")

    # text 是准备切分的长文档正文。
    text = document.text.strip()
    # chunks 保存滑动窗口片段。
    chunks = []
    # start 是当前窗口起点。
    start = 0
    # index 是窗口序号。
    index = 1
    while start < len(text):
        # end 是当前窗口的理论终点。
        end = min(start + max_chars, len(text))
        # window 是当前窗口正文。
        window = text[start:end].strip()
        chunks.append(
            SourceChunk(
                chunk_id=f"{document.doc_id}-{index:02d}",
                doc_id=document.doc_id,
                strategy="overlap_window",
                content=window,
                metadata={
                    "start": start,
                    "end": end,
                    "overlap_chars": overlap_chars,
                },
            )
        )
        if end == len(text):
            break
        start = end - overlap_chars
        index += 1
    return chunks


# 根据文档编号选择适合其结构的切分策略。
def chunk_document(document):
    if document.doc_id in {"west-lake-map", "hangzhou-guide"}:
        if document.doc_id == "west-lake-map":
            return chunk_map_notes(document), None
        return chunk_by_sections(document), None
    if document.doc_id == "transport":
        chunks, header, separator, _ = chunk_table_rows(document)
        return chunks, (header, separator)
    if document.doc_id == "rainy-day":
        return chunk_long_text(document), None
    return chunk_by_sections(document), None


# 生成一份按文档汇总的 Markdown 切分报告。
def build_report(documents, chunks):
    # lines 保存报告标题和表头。
    lines = [
        "# TripPilot 文档切分报告",
        "",
        "| 文档 | 策略 | 片段数 | 平均字符数 |",
        "|---|---|---:|---:|",
    ]
    # document 是当前要汇总的源文档。
    for document in documents:
        # document_chunks 只保留当前文档产生的片段。
        document_chunks = [
            chunk for chunk in chunks if chunk.doc_id == document.doc_id
        ]
        # average_size 是当前文档片段的平均字符数。
        average_size = (
            sum(len(chunk.content) for chunk in document_chunks)
            // len(document_chunks)
            if document_chunks
            else 0
        )
        lines.append(
            f"| {document.doc_id} | {document_chunks[0].strategy} | "
            f"{len(document_chunks)} | {average_size} |"
        )
    return "\n".join(lines) + "\n"


# 运行四类文档的切分实验，并保存报告。
def main(doc_dir, output_dir=None, scenario=None):
    if scenario == "--bad-size":
        try:
            chunk_long_text(
                Document(
                    doc_id="demo",
                    title="演示文档",
                    text="内容",
                    path="demo.md",
                ),
                max_chars=20,
                overlap_chars=5,
            )
        except ChunkingError as error:
            print(f"切分失败：{error}")
            raise SystemExit(1) from error

    # document_directory 是第 28 课创建的四份旅行资料。
    document_directory = Path(doc_dir)
    # output_directory 保存切分报告和完整片段。
    output_directory = Path(output_dir) if output_dir else document_directory.parent
    output_directory.mkdir(parents=True, exist_ok=True)
    # documents 是加载后的资料列表。
    documents = load_documents(document_directory)

    # all_chunks 保存全部结构感知片段。
    all_chunks = []
    # table_headers 保存交通表表头，用于验证每行片段都带列名。
    table_headers = {}
    # document 是当前需要切分的资料。
    for document in documents:
        chunks, table_header = chunk_document(document)
        all_chunks.extend(chunks)
        if table_header:
            table_headers[document.doc_id] = table_header

    # report_text 是按文档汇总的可读报告。
    report_text = build_report(documents, all_chunks)
    # report_path 和 chunks_path 是最终交付文件。
    report_path = output_directory / "chunk-report.md"
    chunks_path = output_directory / "chunks.json"
    report_path.write_text(report_text, encoding="utf-8")
    chunks_path.write_text(
        json.dumps(
            {
                "chunk_count": len(all_chunks),
                "chunks": [chunk.to_dict() for chunk in all_chunks],
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )

    print(f"加载文档：{len(documents)} 份")
    print(f"生成片段：{len(all_chunks)} 个")
    # document 是当前打印统计的文档。
    for document in documents:
        # document_chunks 是当前文档产生的片段。
        document_chunks = [
            chunk for chunk in all_chunks if chunk.doc_id == document.doc_id
        ]
        print(
            f"- {document.doc_id}: "
            f"{document_chunks[0].strategy} / {len(document_chunks)} 个"
        )

    # table_header 是交通表带列名的表头。
    table_header, _ = table_headers["transport"]
    # table_rows 是交通表行片段。
    table_rows = [
        chunk for chunk in all_chunks if chunk.doc_id == "transport"
    ]
    print("交通表每行保留列名：", all(table_header in chunk.content for chunk in table_rows))
    print("已保存：chunk-report.md, chunks.json")


if __name__ == "__main__":
    main(Path(__file__).parent / "28-rag-pipeline" / "docs")
