"""Small deterministic ablation fixture; no model calls or production data writes."""

from __future__ import annotations

import json
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any

from studyquip.config import Settings
from studyquip.db import Database, initialize
from studyquip.retrieval import RetrievalService
from studyquip.textbook import TextbookService, new_id


def evaluate() -> dict[str, Any]:
    with TemporaryDirectory(prefix="studyquip-retrieval-") as temporary:
        settings = Settings(data_dir=Path(temporary))
        initialize(settings)
        db = Database(settings)
        book = db.put("book", {"title": "物理对照样本", "subject_id": "physics"})
        service = TextbookService(db)
        root = service.ensure_root(book["id"])
        node = service.set_node(book["id"], {"parent_id": root["id"], "title": "课题：天体运动"})
        texts = [
            "自由落体是物体仅受重力作用、从静止开始的下落运动。",
            "实验测量需要记录多次读数并比较结果。",
            "表达式为 F=Gm₁m₂/r²，其中 r 是两物体间的距离。",
            "惯性大小以质量衡量，与物体是否运动无关。",
            "实验器材使用后应分类整理。",
            "质量的国际单位是千克，测量可使用天平。",
        ]
        blocks = []
        for index, source in enumerate(texts):
            receipt = service.apply_operations(
                book["id"],
                new_id(),
                [
                    {
                        "op": "insert",
                        "block": {
                            "text": source,
                            "node_id": node["id"] if index == 2 else root["id"],
                            "order": index,
                        },
                    }
                ],
                actor="import",
            )
            blocks.append(receipt["blocks"][0])
        current_node = db.get("node", node["id"])
        db.put(
            "node",
            {
                **current_node,
                "summary": "行星轨道的形状与万有引力提供的向心作用有关。",
                "summary_stale": False,
            },
            id=node["id"],
        )
        inertia = service.record_concept(
            book["id"],
            {
                "name": "惯性",
                "evidence": [
                    service.current_evidence(book["id"], blocks[3]["id"], blocks[3]["revision"], texts[3])
                ],
            },
        )
        mass = service.record_concept(
            book["id"],
            {
                "name": "质量",
                "evidence": [
                    service.current_evidence(book["id"], blocks[5]["id"], blocks[5]["revision"], texts[5])
                ],
            },
        )
        service.record_relation(
            book["id"],
            {
                "source_id": inertia["id"],
                "target_id": mass["id"],
                "type": "measured_by",
                "evidence": [
                    service.current_evidence(
                        book["id"], blocks[3]["id"], blocks[3]["revision"], "惯性大小以质量衡量"
                    )
                ],
            },
        )
        retrieval = RetrievalService(db)
        for index, block in enumerate(blocks):
            retrieval.store_embedding(
                block["id"],
                book["id"],
                block["revision"],
                "deterministic-fixture",
                [1.0, index / len(blocks)],
            )
        cases = [
            ("直接正文", "自由落体", {blocks[0]["id"]}),
            ("目录概述路由", "行星轨道", {blocks[2]["id"]}),
            ("概念关系补充", "惯性", {blocks[3]["id"], blocks[5]["id"]}),
        ]
        rows = []
        for name, query, relevant in cases:
            variants = {}
            for variant, structure, relations in [
                ("flat_mixed", False, False),
                ("directory", True, False),
                ("directory_relations", True, True),
            ]:
                results = retrieval.search(
                    query,
                    book_ids=[book["id"]],
                    mode="hybrid",
                    limit=2,
                    query_vector=[1.0, 0.0],
                    space_fingerprint="deterministic-fixture",
                    include_structure=structure,
                    include_relations=relations,
                )
                found = {item["id"] for item in results}
                variants[variant] = {
                    "hit_count": len(found & relevant),
                    "relevant_count": len(relevant),
                    "noise_count": len(found - relevant),
                    "returned_count": len(found),
                    "recall": round(len(found & relevant) / len(relevant), 3),
                    "block_numbers": [
                        next(index + 1 for index, block in enumerate(blocks) if block["id"] == item["id"])
                        for item in results
                    ],
                }
            rows.append({"case": name, "query": query, "variants": variants})
        db.close()
        return {
            "fixture": "6 synthetic Chinese blocks; deterministic two-dimensional vectors; top 2",
            "model_calls": 0,
            "cases": rows,
        }


if __name__ == "__main__":
    print(json.dumps(evaluate(), ensure_ascii=False, indent=2))
