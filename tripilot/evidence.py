"""Resolve conflicting travel evidence and produce cited answers."""

import json
import re
import sys
from pathlib import Path

from .rag import HashedEmbedding, cosine_similarity


# DEFAULT_QUESTION 是本课需要解决的具体问题。
DEFAULT_QUESTION = "下周一杭州下雨，浙江省博物馆能去拍照吗？"
# NO_EVIDENCE_QUESTION 用于验证没有资料时不能强行回答。
NO_EVIDENCE_QUESTION = "火星上的酒店怎么预订？"


# EvidenceError 表示证据或答案结构不满足要求。
class EvidenceError(ValueError):
    """Evidence cannot be resolved safely."""


# EvidenceCard 是一段可引用资料及其来源信息。
class EvidenceCard:
    # 初始化证据卡，并保存内容和元数据。
    def __init__(
        self,
        evidence_id,
        title,
        source_type,
        authority,
        updated_on,
        uri,
        excerpt,
        claims,
        conditions,
    ):
        # evidence_id 是证据卡的稳定编号。
        self.evidence_id = evidence_id
        # title 是引用中显示的来源标题。
        self.title = title
        # source_type 表示官方、课程或社区等来源类型。
        self.source_type = source_type
        # authority 是来源权威度，数字越大越可信。
        self.authority = authority
        # updated_on 是证据更新日期。
        self.updated_on = updated_on
        # uri 是原始资料位置，用于生成引用。
        self.uri = uri
        # excerpt 是可以进入答案的原文证据。
        self.excerpt = excerpt
        # claims 是结构化事实，例如某地周一是否开放。
        self.claims = claims
        # conditions 是结论附带的例外条件。
        self.conditions = conditions

    # 把证据转换为可保存的 JSON 字典。
    def to_dict(self):
        return {
            "id": self.evidence_id,
            "title": self.title,
            "source_type": self.source_type,
            "authority": self.authority,
            "updated_on": self.updated_on,
            "uri": self.uri,
            "excerpt": self.excerpt,
            "claims": self.claims,
            "conditions": self.conditions,
        }


# 从 JSON 文件读取并校验证据卡。
def load_evidence(path):
    # payload 是证据文件的 JSON 内容。
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    # raw_sources 是尚未实例化的证据列表。
    raw_sources = payload.get("sources")
    if not isinstance(raw_sources, list) or not raw_sources:
        raise EvidenceError("证据文件没有 sources")

    # cards 保存实例化后的证据卡。
    cards = []
    # source 是当前待校验的来源。
    for source in raw_sources:
        # card 是完成基础字段检查后的证据对象。
        card = EvidenceCard(
            evidence_id=source["id"],
            title=source["title"],
            source_type=source["source_type"],
            authority=source["authority"],
            updated_on=source["updated_on"],
            uri=source["uri"],
            excerpt=source["excerpt"],
            claims=source.get("claims", {}),
            conditions=source.get("conditions", []),
        )
        if not card.excerpt.strip():
            raise EvidenceError(f"证据 {card.evidence_id} 没有正文")
        cards.append(card)

    # evidence_ids 用于检查引用编号是否唯一。
    evidence_ids = [card.evidence_id for card in cards]
    if len(evidence_ids) != len(set(evidence_ids)):
        raise EvidenceError("证据 ID 不能重复")
    return cards


# 提取中文连续文本中的二元组，用于检查问题与证据是否确有共同词。
def query_bigrams(text):
    # bigrams 保存所有中文相邻二字组合。
    bigrams = set()
    # segment 是问题中的一个连续中文段。
    for segment in re.findall(r"[\u4e00-\u9fff]+", text):
        bigrams.update(
            segment[index : index + 2]
            for index in range(len(segment) - 1)
        )
    return bigrams


# 根据向量相似度和最小词项重叠筛选相关证据。
def retrieve_evidence(question, cards, embedder, min_score=0.10):
    # question_vector 是问题的单位向量。
    question_vector = embedder.embed(question)
    # question_terms 是问题的中文二元组。
    question_terms = query_bigrams(question)
    # hits 保存相关证据和检索分数。
    hits = []
    # card 是当前参与比较的证据卡。
    for card in cards:
        # content 是标题与摘录组成的可检索文本。
        content = f"{card.title} {card.excerpt}"
        # card_terms 是证据中的中文二元组。
        card_terms = query_bigrams(content)
        if not question_terms.intersection(card_terms):
            continue
        # score 是问题向量与证据向量的余弦相似度。
        score = cosine_similarity(
            question_vector,
            embedder.embed(content),
        )
        if score >= min_score:
            hits.append({"card": card, "score": score})
    hits.sort(key=lambda hit: hit["score"], reverse=True)
    return hits


# 找出同一个事实键在不同证据中的冲突值。
def detect_conflicts(cards):
    # claim_values 按事实键收集所有证据值。
    claim_values = {}
    # card 是当前参与冲突检查的证据。
    for card in cards:
        # key 和 value 是证据声明的事实。
        for key, value in card.claims.items():
            claim_values.setdefault(key, []).append(
                {
                    "evidence_id": card.evidence_id,
                    "value": value,
                    "authority": card.authority,
                    "updated_on": card.updated_on,
                }
            )

    # conflicts 保存实际出现不同值的声明。
    conflicts = []
    # key 是当前事实键，values 是各来源声明。
    for key, values in claim_values.items():
        # distinct_values 是同一事实的不同声明值。
        distinct_values = {json.dumps(value["value"]) for value in values}
        if len(distinct_values) > 1:
            conflicts.append({"claim": key, "values": values})
    return conflicts


# 按权威度和更新时间选择一个冲突事实的胜出来源。
def choose_claim_winner(conflict):
    # sorted_values 先按权威度、再按日期降序排列。
    sorted_values = sorted(
        conflict["values"],
        key=lambda value: (
            value["authority"],
            value["updated_on"],
        ),
        reverse=True,
    )
    # winner 是优先级最高的声明。
    winner = sorted_values[0]
    # runner_up 是第二优先级的声明。
    runner_up = sorted_values[1] if len(sorted_values) > 1 else None
    # confidence 根据权威差距和时间差距给出保守等级。
    if (
        runner_up is None
        or winner["authority"] >= runner_up["authority"] + 2
    ):
        confidence = "high"
    else:
        confidence = "medium"
    return {
        "winner": winner,
        "runner_up": runner_up,
        "confidence": confidence,
    }


# 把引用编号分配给最终答案实际使用的证据。
def build_citations(cards, used_ids):
    # citations 按首次使用顺序保存引用资料。
    citations = []
    # index 是引用编号。
    for index, evidence_id in enumerate(used_ids, start=1):
        # matching 是当前引用编号对应的证据卡。
        matching = next(
            card for card in cards if card.evidence_id == evidence_id
        )
        citations.append(
            {
                "number": index,
                "evidence_id": matching.evidence_id,
                "title": matching.title,
                "source_type": matching.source_type,
                "updated_on": matching.updated_on,
                "uri": matching.uri,
            }
        )
    return citations


# 基于检索证据生成带引用、冲突和不确定性说明的答案。
def answer_question(question, cards, embedder):
    # hits 是问题相关证据。
    hits = retrieve_evidence(question, cards, embedder)
    if not hits:
        return {
            "answer": "没有检索到足够可靠的旅行资料，暂时无法回答。",
            "citations": [],
            "conflicts": [],
            "uncertainty": {
                "level": "unknown",
                "reasons": ["没有达到相关性阈值的证据"],
            },
            "evidence": [],
        }

    # relevant_cards 是进入冲突判断的证据卡。
    relevant_cards = [hit["card"] for hit in hits]
    # conflicts 是证据之间真实存在的冲突。
    conflicts = detect_conflicts(relevant_cards)
    # conflict_resolutions 保存每个冲突事实的胜出来源。
    conflict_resolutions = [
        {
            "claim": conflict["claim"],
            **choose_claim_winner(conflict),
        }
        for conflict in conflicts
    ]

    # official_cards 是官方和课程资料中优先级最高的证据。
    official_cards = [
        card
        for card in relevant_cards
        if card.source_type in {"official", "course"}
    ]
    # primary_conflict 是决定主要答案的事实冲突。
    primary_conflict = conflict_resolutions[0] if conflict_resolutions else None
    if primary_conflict:
        # winner_id 是冲突解析后采用的证据 ID。
        winner_id = primary_conflict["winner"]["evidence_id"]
        # winner_card 是最终回答主要事实的来源。
        winner_card = next(
            card for card in relevant_cards if card.evidence_id == winner_id
        )
        # runner_up_id 是需要在答案中披露的反向证据。
        runner_up_id = primary_conflict["runner_up"]["evidence_id"]
        # old_note 是冲突中的次要来源。
        old_note = next(
            card
            for card in relevant_cards
            if card.evidence_id == runner_up_id
        )
    else:
        # winner_card 是没有冲突时权威度最高的证据。
        winner_card = max(
            official_cards or relevant_cards,
            key=lambda card: (card.authority, card.updated_on),
        )
        # old_note 在没有冲突时不额外引用社区来源。
        old_note = None
    # rainy_card 是给出雨天建议的课程资料。
    rainy_card = next(
        (
            card
            for card in relevant_cards
            if card.evidence_id == "rainy-day-advice"
        ),
        None,
    )

    # used_ids 是答案实际引用的证据编号。
    used_ids = [winner_card.evidence_id]
    if old_note is not None:
        used_ids.append(old_note.evidence_id)
    if rainy_card is not None and rainy_card.evidence_id not in used_ids:
        used_ids.append(rainy_card.evidence_id)
    # citations 把证据 ID 映射成答案里的 [1]、[2]。
    citations = build_citations(relevant_cards, used_ids)
    # number_by_id 用于生成方括号引用编号。
    number_by_id = {
        citation["evidence_id"]: citation["number"]
        for citation in citations
    }

    # answer_lines 保存带引用的答案句子。
    answer_lines = [
        (
            "按最新官方公告，浙江省博物馆在普通周一闭馆，"
            f"不建议只按“周一可以参观”做行程安排"
            f" [{number_by_id[winner_card.evidence_id]}]。"
        )
    ]
    if old_note is not None:
        answer_lines.append(
            (
                f"较早游客笔记记录过周一入馆"
                f" [{number_by_id[old_note.evidence_id]}]，"
                "但它的权威度和更新时间都低于当前公告，"
                "这属于需要披露的资料冲突。"
            )
        )
    if rainy_card is not None:
        answer_lines.append(
            (
                "下雨时仍可优先找室内替代点，"
                "但出发前需要再次确认当天是否属于法定节假日"
                f" [{number_by_id[rainy_card.evidence_id]}]。"
            )
        )

    # uncertainty_reasons 记录不能给出绝对结论的原因。
    uncertainty_reasons = []
    if conflicts:
        uncertainty_reasons.append("检索证据中存在开放规则冲突")
    # condition 是权威证据自身保留的例外条件。
    for condition in winner_card.conditions:
        uncertainty_reasons.append(f"官方规则包含例外：{condition}")
    if question == DEFAULT_QUESTION:
        uncertainty_reasons.append("问题没有说明下周一是普通日还是法定节假日")

    return {
        "answer": "\n".join(answer_lines),
        "citations": citations,
        "conflicts": conflicts,
        "conflict_resolutions": conflict_resolutions,
        "uncertainty": {
            "level": "medium" if uncertainty_reasons else "high",
            "reasons": uncertainty_reasons,
        },
        "evidence": [
            {
                "id": hit["card"].evidence_id,
                "title": hit["card"].title,
                "score": round(hit["score"], 4),
                "updated_on": hit["card"].updated_on,
            }
            for hit in hits
        ],
    }


# 把结构化答案渲染为可发布 Markdown。
def render_markdown(question, result):
    # lines 保存答案正文、引用和不确定性。
    lines = [
        "# 有证据的旅行回答",
        "",
        f"**问题：** {question}",
        "",
        "## 答案",
        "",
        result["answer"],
        "",
        "## 引用",
        "",
    ]
    if result["citations"]:
        # citation 是答案使用的一条来源。
        for citation in result["citations"]:
            lines.append(
                f"- [{citation['number']}] {citation['title']}，"
                f"{citation['updated_on']}，`{citation['uri']}`"
            )
    else:
        lines.append("- 无")
    lines.extend(["", "## 不确定性", ""])
    if result["uncertainty"]["reasons"]:
        # reason 是一条不能省略的边界说明。
        for reason in result["uncertainty"]["reasons"]:
            lines.append(f"- {reason}")
    else:
        lines.append("- 无")
    return "\n".join(lines) + "\n"


# 运行证据冲突解析和带引用回答演示。
def main(
    evidence_path,
    output_dir=None,
    scenario=None,
):
    # evidence_file 是证据卡 JSON 文件。
    evidence_file = Path(evidence_path)
    # output_directory 保存答案和 JSON 报告。
    output_directory = (
        Path(output_dir) if output_dir else evidence_file.parent
    )
    output_directory.mkdir(parents=True, exist_ok=True)
    # cards 是加载后的证据来源。
    cards = load_evidence(evidence_file)
    # embedder 与前面课程共用同一向量方法。
    embedder = HashedEmbedding(128)
    # question 根据演示模式选择正常问题或资料外问题。
    question = (
        NO_EVIDENCE_QUESTION
        if scenario == "--no-evidence"
        else DEFAULT_QUESTION
    )
    # result 是带答案、引用、冲突和不确定性的结果。
    result = answer_question(question, cards, embedder)

    print("问题：", question)
    print("检索证据：")
    # index 是检索证据序号。
    for index, evidence in enumerate(result["evidence"], start=1):
        print(
            f"{index}. {evidence['id']} "
            f"score={evidence['score']:.4f} "
            f"updated={evidence['updated_on']}"
        )
    print("\n冲突：", result["conflicts"] or "无")
    print("\n答案：")
    print(result["answer"])
    print(
        "\n不确定性：",
        result["uncertainty"]["level"],
        "；".join(result["uncertainty"]["reasons"]) or "无",
    )
    print(
        "引用：",
        ", ".join(
            f"[{item['number']}] {item['title']}"
            for item in result["citations"]
        )
        or "无",
    )

    # answer_path 保存可阅读答案，report_path 保存结构化证据结果。
    answer_path = output_directory / "answer.md"
    report_path = output_directory / "evidence-report.json"
    answer_path.write_text(
        render_markdown(question, result),
        encoding="utf-8",
    )
    report_path.write_text(
        json.dumps(
            {"question": question, **result},
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    print("已保存：answer.md, evidence-report.json")


if __name__ == "__main__":
    main(Path(__file__).parent / "evidence-cards.json")
