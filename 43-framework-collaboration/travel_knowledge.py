"""LlamaIndex 检索层：只负责把旅行资料变成可引用的证据。

这一层不调用聊天模型，也不判断预算或审批。它的输出只有 EvidenceCard，
这样 LangGraph 可以替换成别的检索实现，而不用改状态图。
"""

from __future__ import annotations

import hashlib
import math
import re
from pathlib import Path

from llama_index.core import SimpleDirectoryReader, VectorStoreIndex
from llama_index.core.base.base_retriever import BaseRetriever
from llama_index.core.embeddings import BaseEmbedding
from llama_index.core.indices.vector_store.retrievers import VectorIndexRetriever
from llama_index.core.node_parser import MarkdownNodeParser
from llama_index.core.schema import BaseNode, MetadataMode, NodeWithScore

from models import EvidenceCard


# 资料目录目前只覆盖杭州；检索层必须知道自己的覆盖范围，不能只靠相似度分数。
DOC_DESTINATIONS = {
    "hangzhou-guide": "杭州",
    "rainy-day": "杭州",
    "transport": "杭州",
    "west-lake-map": "杭州",
}


def document_title(document) -> str:
    """读取 Markdown 的一级标题；没有标题时退回文件名。"""
    for line in document.text.splitlines():
        if line.startswith("# "):
            return line.removeprefix("#").strip()
    return Path(document.metadata["file_path"]).stem


def build_nodes(documents, parser: MarkdownNodeParser) -> list[BaseNode]:
    """按 Markdown 标题切分，并给每个节点补上稳定引用编号。"""
    titles = {
        Path(document.metadata["file_path"]).stem: document_title(document)
        for document in documents
    }
    parsed_nodes = parser.get_nodes_from_documents(documents)
    counters: dict[str, int] = {}
    nodes: list[BaseNode] = []

    for node in parsed_nodes:
        doc_id = Path(node.metadata["file_path"]).stem
        title = titles[doc_id]
        body = node.get_content(metadata_mode=MetadataMode.NONE).strip()

        # MarkdownNodeParser 会为一级标题生成只有标题的节点；它没有证据正文。
        if node.metadata.get("header_path") == "/" and body == f"# {title}":
            continue

        counters[doc_id] = counters.get(doc_id, 0) + 1
        node_id = f"{doc_id}-{counters[doc_id]:02d}"
        node.id_ = node_id
        node.metadata.update(
            {
                "doc_id": doc_id,
                "title": title,
                "citation": f"{title}#{node_id}",
            }
        )
        # 把标题补回正文，让片段离开原文件后仍然保留上下文。
        node.text = f"{title}\n{body}"
        # 引用编号和文件路径不参与相似度计算，否则元数据会污染检索分数。
        node.excluded_embed_metadata_keys = list(node.metadata.keys())
        node.excluded_llm_metadata_keys = list(node.metadata.keys())
        nodes.append(node)

    return nodes


class LocalHashEmbedding(BaseEmbedding):
    """确定性本地向量器，避免第 43 课依赖外部 Embedding 服务。"""

    model_name: str = "signed-hash-ngram-128"
    dimensions: int = 128

    def _embed(self, text: str) -> list[float]:
        """把中英文混合文本映射为固定长度向量。"""
        if self.dimensions < 16:
            raise ValueError("向量维度不能小于 16")

        segments = re.findall(r"[\u4e00-\u9fff]+|[a-z0-9]+", text.lower())
        tokens: list[str] = []
        for segment in segments:
            if re.fullmatch(r"[\u4e00-\u9fff]+", segment):
                # 中文保留单字，并加入相邻二元组补足语序信息。
                tokens.extend(segment)
                tokens.extend(
                    segment[index : index + 2]
                    for index in range(len(segment) - 1)
                )
            else:
                tokens.append(segment)

        vector = [0.0] * self.dimensions
        for token in tokens:
            digest = hashlib.blake2b(token.encode("utf-8"), digest_size=8).digest()
            bucket = int.from_bytes(digest[:4], "big") % self.dimensions
            sign = 1.0 if digest[4] & 1 else -1.0
            vector[bucket] += sign

        norm = math.sqrt(sum(value * value for value in vector))
        if norm == 0:
            return vector
        return [value / norm for value in vector]

    def _get_text_embedding(self, text: str) -> list[float]:
        return self._embed(text)

    def _get_query_embedding(self, query: str) -> list[float]:
        return self._embed(query)

    async def _aget_text_embedding(self, text: str) -> list[float]:
        return self._embed(text)

    async def _aget_query_embedding(self, query: str) -> list[float]:
        return self._embed(query)


def evidence_text(node: BaseNode) -> str:
    """去掉标题和 Markdown 标题行，只保留可引用的正文。"""
    lines = node.get_content(metadata_mode=MetadataMode.NONE).splitlines()[1:]
    return " ".join(
        line.strip()
        for line in lines
        if line.strip() and not line.startswith("#")
    )


class TravelKnowledgeBase:
    """封装 LlamaIndex 的加载、索引和检索，向上只暴露 retrieve()。"""

    def __init__(self, docs_dir: Path, top_k: int = 3, min_score: float = 0.15):
        self.docs_dir = docs_dir
        self.top_k = top_k
        # 阈值用于过滤“看起来有分数但没有实际证据”的节点；本课可在检索结果中观察。
        self.min_score = min_score
        self.retriever = self._build_retriever()

    def _build_retriever(self) -> BaseRetriever:
        """加载 docs/、切分 Markdown 并建立本地向量索引。"""
        if not self.docs_dir.exists():
            raise FileNotFoundError(f"资料目录不存在：{self.docs_dir}")

        documents = SimpleDirectoryReader(input_dir=str(self.docs_dir)).load_data()
        if not documents:
            raise ValueError(f"资料目录为空：{self.docs_dir}")

        parser = MarkdownNodeParser(include_metadata=True, include_prev_next_rel=False)
        nodes = build_nodes(documents, parser)
        if not nodes:
            raise ValueError("资料切分后没有可用节点")

        index = VectorStoreIndex(
            nodes,
            embed_model=LocalHashEmbedding(),
            show_progress=False,
        )
        return VectorIndexRetriever(index=index, similarity_top_k=self.top_k)

    def retrieve(self, query: str, destination: str | None = None) -> list[EvidenceCard]:
        """检索与问题最相关的证据，并按引用编号返回稳定结构。"""
        if destination and destination not in set(DOC_DESTINATIONS.values()):
            # “火星”不在资料覆盖范围内；直接返回空，避免向量相似度把杭州资料硬塞给用户。
            return []

        hits: list[NodeWithScore] = self.retriever.retrieve(query)
        cards: list[EvidenceCard] = []

        for hit in hits:
            score = float(hit.score or 0.0)
            # 低于阈值的命中不进入模型上下文，避免把弱相关内容写成事实。
            if score < self.min_score:
                continue

            node = hit.node
            doc_id = node.metadata.get("doc_id", "")
            if destination and DOC_DESTINATIONS.get(doc_id) != destination:
                continue
            cards.append(
                EvidenceCard(
                    citation=node.metadata.get("citation", node.node_id),
                    title=node.metadata.get("title", "未知资料"),
                    content=evidence_text(node),
                    score=round(score, 4),
                )
            )

        return cards
