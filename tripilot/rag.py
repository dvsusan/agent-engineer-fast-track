"""Build a deterministic, citation-first RAG pipeline for TripPilot."""

import hashlib
import json
import math
import re
import sys
from dataclasses import dataclass
from pathlib import Path


# DEFAULT_QUERY 是本课用于验证检索链路的问题。
DEFAULT_QUERY = "杭州下雨天想拍照，不想走太远，去哪里？"
# WEAK_QUERY 用于演示没有足够证据时的拒答。
WEAK_QUERY = "火星上的酒店怎么预订？"


# Document 是一份加载后的旅行资料。
@dataclass(frozen=True)
class Document:
    # doc_id 是文档的稳定编号。
    doc_id: str
    # title 是文档标题。
    title: str
    # text 是文档正文。
    text: str
    # path 是文档来源路径。
    path: str


# Chunk 是可以单独检索和引用的资料片段。
@dataclass(frozen=True)
class Chunk:
    # chunk_id 是片段唯一编号。
    chunk_id: str
    # doc_id 是片段所属文档。
    doc_id: str
    # title 是片段所属文档标题。
    title: str
    # content 是片段正文。
    content: str

    # 返回引用中显示的人类可读来源。
    def citation(self):
        return f"{self.title}#{self.chunk_id}"


# SearchHit 保存一次向量检索命中的片段和分数。
@dataclass(frozen=True)
class SearchHit:
    # chunk 是命中的资料片段。
    chunk: Chunk
    # score 是查询向量与片段向量的余弦相似度。
    score: float


# 加载 Markdown 文档，并读取标题作为元数据。
def load_documents(doc_dir):
    # document_dir 是资料目录的 Path 对象。
    document_dir = Path(doc_dir)
    # files 是目录下按名称排序的 Markdown 文件。
    files = sorted(document_dir.glob("*.md"))
    if not files:
        raise ValueError(f"资料目录没有 Markdown 文件：{document_dir}")

    # documents 保存加载后的全部资料。
    documents = []
    # file_path 是当前正在读取的 Markdown 文件。
    for file_path in files:
        # text 是文件完整文本。
        text = file_path.read_text(encoding="utf-8").strip()
        # first_line 用来提取一级标题。
        first_line = text.splitlines()[0] if text else ""
        # title 是没有 Markdown 标记的文档标题。
        title = first_line.removeprefix("#").strip() or file_path.stem
        documents.append(
            Document(
                doc_id=file_path.stem,
                title=title,
                text=text,
                path=str(file_path),
            )
        )
    return documents


# 按空行切分文档，再给每个片段补回文档标题。
def chunk_documents(documents, max_chars=260):
    # chunks 保存所有可检索片段。
    chunks = []
    # document 是当前需要切分的文档。
    for document in documents:
        # paragraphs 去掉一级标题和空段后保留原始段落边界。
        paragraphs = [
            paragraph.strip()
            for paragraph in re.split(r"\n\s*\n", document.text)
            if paragraph.strip() and not paragraph.lstrip().startswith("# ")
        ]
        # sections 把每个二级标题和它的正文合成一个片段。
        sections = []
        # section_lines 暂存当前二级标题及后续正文。
        section_lines = []
        # paragraph 是当前正在归组的 Markdown 段落。
        for paragraph in paragraphs:
            if paragraph.startswith("## ") and section_lines:
                sections.append("\n".join(section_lines))
                section_lines = []
            section_lines.append(paragraph)
        if section_lines:
            sections.append("\n".join(section_lines))

        # index 是当前文档中的片段序号。
        for index, section in enumerate(sections, start=1):
            # content 前补标题，让片段离开原文档后仍有上下文。
            content = f"{document.title}\n{section}"
            if len(content) > max_chars:
                raise ValueError(
                    f"片段 {document.doc_id}-{index} 超过 {max_chars} 字符"
                )
            chunks.append(
                Chunk(
                    chunk_id=f"{document.doc_id}-{index:02d}",
                    doc_id=document.doc_id,
                    title=document.title,
                    content=content,
                )
            )
    return chunks


# HashedEmbedding 用稳定哈希生成轻量教学向量。
class HashedEmbedding:
    # 初始化固定维度向量。
    def __init__(self, dimensions=128):
        if dimensions < 16:
            raise ValueError("向量维度不能小于 16")
        # dimensions 是每个文本向量的长度。
        self.dimensions = dimensions

    # 把中文连续文本和英文数字拆成可哈希词项。
    def tokenize(self, text):
        # segments 保存中文连续段和英文数字词。
        segments = re.findall(r"[\u4e00-\u9fff]+|[a-z0-9]+", text.lower())
        # tokens 保存最终词项。
        tokens = []
        # segment 是当前文本段。
        for segment in segments:
            if re.fullmatch(r"[\u4e00-\u9fff]+", segment):
                # 中文字符保留单字，并加入相邻二元组补充语序信息。
                tokens.extend(segment)
                tokens.extend(
                    segment[index : index + 2]
                    for index in range(len(segment) - 1)
                )
            else:
                tokens.append(segment)
        return tokens

    # 把文本转换为单位向量。
    def embed(self, text):
        # vector 是待填充的哈希向量。
        vector = [0.0] * self.dimensions
        # token 是当前参与哈希的文本词项。
        for token in self.tokenize(text):
            # digest 是跨进程稳定的摘要字节。
            digest = hashlib.blake2b(
                token.encode("utf-8"),
                digest_size=8,
            ).digest()
            # bucket 是词项落到向量中的位置。
            bucket = int.from_bytes(digest[:4], "big") % self.dimensions
            # sign 用摘要位决定正负，减少哈希碰撞累积。
            sign = 1.0 if digest[4] & 1 else -1.0
            vector[bucket] += sign

        # norm 是向量长度，用于归一化。
        norm = math.sqrt(sum(value * value for value in vector))
        if norm == 0:
            return vector
        return [value / norm for value in vector]


# 计算两个单位向量的余弦相似度。
def cosine_similarity(left, right):
    if len(left) != len(right):
        raise ValueError("向量维度不一致")
    return sum(a * b for a, b in zip(left, right))


# InMemoryVectorStore 保存片段向量，并执行 top-k 检索。
class InMemoryVectorStore:
    # 初始化空索引。
    def __init__(self, embedder):
        # embedder 负责文本向量化。
        self.embedder = embedder
        # items 保存片段和对应向量。
        self.items = []

    # 把文本片段加入索引。
    def add(self, chunks):
        # chunk 是当前要索引的片段。
        for chunk in chunks:
            self.items.append(
                {
                    "chunk": chunk,
                    "vector": self.embedder.embed(chunk.content),
                }
            )

    # 返回和查询最相似的 top_k 个片段。
    def search(self, query, top_k=3):
        # query_vector 是当前问题的归一化向量。
        query_vector = self.embedder.embed(query)
        # hits 收集所有片段的相似度。
        hits = [
            SearchHit(
                chunk=item["chunk"],
                score=cosine_similarity(query_vector, item["vector"]),
            )
            for item in self.items
        ]
        hits.sort(key=lambda hit: hit.score, reverse=True)
        return hits[:top_k]


# ExtractiveAnswerGenerator 用命中的证据拼出可追溯答案。
class ExtractiveAnswerGenerator:
    # 只有最低证据分数达标时才生成答案。
    def __init__(self, min_score=0.25):
        # min_score 是允许回答的最低相似度。
        self.min_score = min_score

    # 根据检索命中生成答案和引用。
    def generate(self, query, hits):
        # relevant_hits 只保留达到阈值的证据。
        relevant_hits = [hit for hit in hits if hit.score >= self.min_score]
        if not relevant_hits:
            return {
                "answer": "没有检索到足够可靠的旅行资料，暂时无法回答。",
                "citations": [],
            }

        # lines 保存答案正文。
        lines = ["根据检索到的资料，可以这样安排："]
        # hit 是当前准备引用的证据。
        for hit in relevant_hits:
            # evidence_lines 去掉文档标题和章节标题，只保留证据正文。
            evidence_lines = hit.chunk.content.splitlines()[1:]
            evidence_lines = [
                line.strip()
                for line in evidence_lines
                if line.strip() and not line.startswith("#")
            ]
            # evidence 是最终进入答案的正文。
            evidence = " ".join(evidence_lines)
            lines.append(
                f"- {evidence} [{hit.chunk.citation()}]"
            )
        return {
            "answer": "\n".join(lines),
            "citations": [
                hit.chunk.citation() for hit in relevant_hits
            ],
        }


# 运行完整 RAG 流程，并保存索引和回答。
def main(doc_dir, output_dir=None, scenario=None):
    # document_directory 是旅行 Markdown 资料目录。
    document_directory = Path(doc_dir)
    # output_directory 保存 RAG 索引和回答。
    output_directory = Path(output_dir) if output_dir else document_directory.parent
    output_directory.mkdir(parents=True, exist_ok=True)

    # documents 是加载后的四份旅行资料。
    documents = load_documents(document_directory)
    # chunks 是简单段落切分后的检索片段。
    chunks = chunk_documents(documents)
    # embedder 生成本课使用的稳定教学向量。
    embedder = HashedEmbedding(dimensions=128)
    # store 保存全部片段向量。
    store = InMemoryVectorStore(embedder)
    store.add(chunks)

    # query 根据演示模式选择正常问题或无证据问题。
    query = WEAK_QUERY if scenario == "--weak-query" else DEFAULT_QUERY
    # hits 是余弦检索得到的前三个片段。
    hits = store.search(query, top_k=3)
    # generator 根据证据生成答案或拒答。
    result = ExtractiveAnswerGenerator().generate(query, hits)

    # index_payload 保存可检查的索引元数据。
    index_payload = {
        "embedding": {
            "method": "signed_hashed_ngrams",
            "dimensions": embedder.dimensions,
        },
        "document_count": len(documents),
        "chunk_count": len(chunks),
        "chunks": [
            {
                "id": item["chunk"].chunk_id,
                "doc_id": item["chunk"].doc_id,
                "title": item["chunk"].title,
                "content": item["chunk"].content,
                "vector": item["vector"],
            }
            for item in store.items
        ],
    }
    # answer_payload 保存问题、检索路径和最终答案。
    answer_payload = {
        "query": query,
        "retrieved": [
            {
                "citation": hit.chunk.citation(),
                "score": round(hit.score, 4),
                "content": hit.chunk.content,
            }
            for hit in hits
        ],
        **result,
    }

    # index_path 和 answer_path 是本课最终可复核文件。
    index_path = output_directory / "rag-index.json"
    answer_path = output_directory / "rag-answer.json"
    index_path.write_text(
        json.dumps(index_payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    answer_path.write_text(
        json.dumps(answer_payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    print(f"加载文档：{len(documents)} 份")
    print(f"切分片段：{len(chunks)} 个")
    print(f"向量维度：{embedder.dimensions}")
    print(f"问题：{query}")
    print("检索结果：")
    # rank 是检索结果名次。
    for rank, hit in enumerate(hits, start=1):
        print(
            f"{rank}. score={hit.score:.4f} "
            f"source={hit.chunk.citation()}"
        )
    print("\n答案：")
    print(result["answer"])
    print("\n引用：", ", ".join(result["citations"]) or "无")
    print("已保存：rag-index.json, rag-answer.json")


if __name__ == "__main__":
    main(Path(__file__).parent / "docs")
