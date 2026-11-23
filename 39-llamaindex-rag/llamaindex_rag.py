"""Rebuild TripPilot RAG with LlamaIndex components and deterministic models."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import platform
import re
from pathlib import Path
from typing import Any

import llama_index.core
from llama_index.core import SimpleDirectoryReader, VectorStoreIndex
from llama_index.core.base.base_retriever import BaseRetriever
from llama_index.core.base.response.schema import Response
from llama_index.core.embeddings import BaseEmbedding
from llama_index.core.indices.vector_store.retrievers import VectorIndexRetriever
from llama_index.core.node_parser import MarkdownNodeParser
from llama_index.core.query_engine import CustomQueryEngine
from llama_index.core.schema import BaseNode, MetadataMode, NodeWithScore


PROJECT_DIR = Path(__file__).resolve().parent
DOC_DIR = PROJECT_DIR / "docs"
INDEX_REPORT_FILE = PROJECT_DIR / "llamaindex-index.json"
ANSWER_FILE = PROJECT_DIR / "llamaindex-answer.json"
WEAK_ANSWER_FILE = PROJECT_DIR / "llamaindex-weak-answer.json"
COMPARISON_FILE = PROJECT_DIR / "llamaindex-comparison.json"

DEFAULT_QUERY = "杭州下雨天想拍照，不想走太远，去哪里？"
WEAK_QUERY = "火星上的酒店怎么预订？"
MIN_SCORE = 0.25
TOP_K = 3


def write_json(path: Path, payload: dict[str, Any]) -> None:
    """Write readable JSON that can be compared with the chapter 28 output."""
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def document_title(document) -> str:
    """Read the first Markdown H1 as the document title."""
    for line in document.text.splitlines():
        if line.startswith("# "):
            return line.removeprefix("#").strip()
    return Path(document.metadata["file_path"]).stem


def build_nodes(documents) -> list[BaseNode]:
    """Parse Markdown sections and restore chapter 28 citation identifiers."""
    titles = {
        Path(document.metadata["file_path"]).stem: document_title(document)
        for document in documents
    }
    parser = MarkdownNodeParser(
        include_metadata=True,
        include_prev_next_rel=False,
    )
    parsed_nodes = parser.get_nodes_from_documents(documents)
    counters: dict[str, int] = {}
    nodes: list[BaseNode] = []

    for node in parsed_nodes:
        doc_id = Path(node.metadata["file_path"]).stem
        title = titles[doc_id]
        body = node.get_content(metadata_mode=MetadataMode.NONE).strip()

        # MarkdownNodeParser 会为一级标题生成只有标题的节点；它不包含答案证据。
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
        # 把标题补回正文，使片段离开原文件后仍然保留上下文。
        node.text = f"{title}\n{body}"
        # 本课希望 Embedding 只比较文本正文，不让文件路径和日期改变相似度。
        node.excluded_embed_metadata_keys = list(node.metadata.keys())
        node.excluded_llm_metadata_keys = list(node.metadata.keys())
        nodes.append(node)
    return nodes


class LocalHashEmbedding(BaseEmbedding):
    """Adapt the chapter 28 deterministic vectorizer to LlamaIndex."""

    model_name: str = "signed-hash-ngram-128"
    dimensions: int = 128

    def _embed(self, text: str) -> list[float]:
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
    """Remove title and heading lines so the answer only quotes evidence."""
    lines = node.get_content(metadata_mode=MetadataMode.NONE).splitlines()[1:]
    return " ".join(
        line.strip()
        for line in lines
        if line.strip() and not line.startswith("#")
    )


def hit_payload(hit: NodeWithScore) -> dict[str, Any]:
    """Convert a LlamaIndex hit into a stable, inspectable report row."""
    node = hit.node
    return {
        "citation": node.metadata.get("citation", node.node_id),
        "node_id": node.node_id,
        "score": round(float(hit.score or 0.0), 4),
        "content": node.get_content(metadata_mode=MetadataMode.NONE),
    }


class ExtractiveCitationQueryEngine(CustomQueryEngine):
    """A LlamaIndex query engine that answers only from retrieved nodes."""

    retriever: BaseRetriever
    min_score: float = MIN_SCORE

    def custom_query(self, query_str: str) -> Response:
        hits = self.retriever.retrieve(query_str)
        retrieved = [hit_payload(hit) for hit in hits]
        relevant_hits = [
            hit for hit in hits if float(hit.score or 0.0) >= self.min_score
        ]
        citations = [
            hit.node.metadata.get("citation", hit.node.node_id)
            for hit in relevant_hits
        ]

        if not relevant_hits:
            answer = "没有检索到足够可靠的旅行资料，暂时无法回答。"
        else:
            lines = ["根据检索到的资料，可以这样安排："]
            for hit in relevant_hits:
                citation = hit.node.metadata.get("citation", hit.node.node_id)
                lines.append(f"- {evidence_text(hit.node)} [{citation}]")
            answer = "\n".join(lines)

        return Response(
            response=answer,
            source_nodes=relevant_hits,
            metadata={
                "citations": citations,
                "retrieved": retrieved,
                "threshold": self.min_score,
                "passed_threshold": bool(relevant_hits),
            },
        )


def manual_baseline_file() -> Path | None:
    """Find the chapter 28 answer in repository and learner layouts."""
    candidates = (
        PROJECT_DIR.parent / "28-rag-pipeline" / "rag-answer.json",
        PROJECT_DIR.parents[1]
        / "stage-03"
        / "28-rag-pipeline"
        / "rag-answer.json",
    )
    for path in candidates:
        if path.exists():
            return path
    return None


def build_comparison(
    llama_answer: dict[str, Any], baseline_file: Path | None
) -> dict[str, Any]:
    """Compare top citations and scores with the hand-written chapter 28 run."""
    llama_citations = llama_answer["citations"]
    llama_scores = [row["score"] for row in llama_answer["retrieved"]]
    if baseline_file is None:
        return {
            "manual_available": False,
            "llamaindex_citations": llama_citations,
            "llamaindex_scores": llama_scores,
        }

    baseline = json.loads(baseline_file.read_text(encoding="utf-8"))
    manual_citations = baseline["citations"]
    manual_scores = [row["score"] for row in baseline["retrieved"]]
    return {
        "manual_available": True,
        "manual_file": f"{baseline_file.parent.name}/{baseline_file.name}",
        "manual_citations": manual_citations,
        "llamaindex_citations": llama_citations,
        "manual_scores": manual_scores,
        "llamaindex_scores": llama_scores,
        "same_citations": manual_citations == llama_citations,
        "same_scores": manual_scores == llama_scores,
    }


def build_index_report(
    documents,
    nodes: list[BaseNode],
    embed_model: LocalHashEmbedding,
) -> dict[str, Any]:
    """Record the framework, parser, embedding, and node metadata."""
    return {
        "framework": {
            "name": "llama-index-core",
            "version": llama_index.core.__version__,
            "python": platform.python_version(),
        },
        "pipeline": {
            "reader": "SimpleDirectoryReader",
            "parser": "MarkdownNodeParser",
            "index": "VectorStoreIndex",
            "retriever": "VectorIndexRetriever",
            "query_engine": "ExtractiveCitationQueryEngine",
        },
        "embedding": {
            "class": type(embed_model).__name__,
            "model_name": embed_model.model_name,
            "method": "signed_hashed_ngrams",
            "dimensions": embed_model.dimensions,
            "similarity": "cosine",
        },
        "document_count": len(documents),
        "node_count": len(nodes),
        "nodes": [
            {
                "node_id": node.node_id,
                "citation": node.metadata["citation"],
                "doc_id": node.metadata["doc_id"],
                "title": node.metadata["title"],
                "header_path": node.metadata.get("header_path"),
                "content": node.get_content(metadata_mode=MetadataMode.NONE),
                "embedding_preview": [
                    round(value, 6)
                    for value in embed_model._get_text_embedding(
                        node.get_content(metadata_mode=MetadataMode.NONE)
                    )[:4]
                ],
            }
            for node in nodes
        ],
    }


def run(weak_query: bool = False) -> dict[str, Any]:
    """Load docs, build the index, query it, and save every check result."""
    documents = SimpleDirectoryReader(
        DOC_DIR,
        required_exts=[".md"],
        filename_as_id=True,
    ).load_data()
    if len(documents) != 4:
        raise RuntimeError(f"预期加载 4 份资料，实际为 {len(documents)} 份")

    nodes = build_nodes(documents)
    embed_model = LocalHashEmbedding(dimensions=128)
    index = VectorStoreIndex(
        nodes,
        embed_model=embed_model,
        show_progress=False,
    )
    retriever = VectorIndexRetriever(index=index, similarity_top_k=TOP_K)
    query_engine = ExtractiveCitationQueryEngine(
        retriever=retriever,
        min_score=MIN_SCORE,
    )

    query = WEAK_QUERY if weak_query else DEFAULT_QUERY
    response = query_engine.query(query)
    answer_payload = {
        "query": query,
        "framework_version": llama_index.core.__version__,
        "answer": str(response),
        "citations": response.metadata["citations"],
        "retrieved": response.metadata["retrieved"],
        "threshold": response.metadata["threshold"],
        "passed_threshold": response.metadata["passed_threshold"],
    }

    index_report = build_index_report(documents, nodes, embed_model)
    write_json(INDEX_REPORT_FILE, index_report)
    if weak_query:
        write_json(WEAK_ANSWER_FILE, answer_payload)
        if answer_payload["citations"]:
            raise RuntimeError("无证据问题不应返回引用")
        saved_answer = WEAK_ANSWER_FILE
    else:
        write_json(ANSWER_FILE, answer_payload)
        if not answer_payload["citations"]:
            raise RuntimeError("正常问题应返回至少一条引用")
        baseline_file = manual_baseline_file()
        comparison = build_comparison(answer_payload, baseline_file)
        write_json(COMPARISON_FILE, comparison)
        saved_answer = ANSWER_FILE

    print(f"LlamaIndex 版本：{llama_index.core.__version__}")
    print(f"加载文档：{len(documents)} 份")
    print(f"Markdown 节点：{len(nodes)} 个")
    print(f"向量维度：{embed_model.dimensions}")
    print(f"问题：{query}")
    print("检索结果：")
    for rank, row in enumerate(answer_payload["retrieved"], start=1):
        print(f"{rank}. score={row['score']:.4f} source={row['citation']}")
    print("\n答案：")
    print(answer_payload["answer"])
    print("\n引用：", ", ".join(answer_payload["citations"]) or "无")
    if not weak_query:
        print(
            "与手写版对照："
            f"citations={comparison['same_citations']} "
            f"scores={comparison.get('same_scores')}"
        )
    print(f"已保存：{INDEX_REPORT_FILE.name}, {saved_answer.name}")
    return answer_payload


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the chapter 39 LlamaIndex RAG demo")
    parser.add_argument(
        "--weak-query",
        action="store_true",
        help="使用资料之外的问题验证拒答边界",
    )
    args = parser.parse_args()
    run(weak_query=args.weak_query)


if __name__ == "__main__":
    main()
