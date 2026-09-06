from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from studyquip.config import Settings
from studyquip.context import ContextBuilder, estimate_tokens
from studyquip.db import Database, initialize
from studyquip.lexical import EvidenceError, canonical_text, locate_quote, normalize
from studyquip.retrieval import RetrievalService, records
from studyquip.textbook import TextbookService, new_id


@pytest.fixture
def library(tmp_path: Path) -> tuple[Database, TextbookService, dict[str, Any]]:
    settings = Settings(data_dir=tmp_path)
    initialize(settings)
    db = Database(settings)
    book = db.put("book", {"title": "物理 必修一", "subject_id": "physics"})
    service = TextbookService(db)
    service.ensure_root(book["id"])
    return db, service, book


def test_quote_normalization_offsets_and_rejection_rules() -> None:
    source = canonical_text("  😀e\u0301\u2003\n牛顿 定律；保留标点。\t")
    start, end = locate_quote(source, "😀é 牛顿   定律；保留标点。")
    assert source[start:end] == "😀é\u2003\n牛顿 定律；保留标点。"
    assert normalize(source) == "😀é 牛顿 定律；保留标点。"
    with pytest.raises(EvidenceError, match="ambiguous_quote"):
        locate_quote("力与力", "力")
    assert locate_quote("力与力", "力", (2, 3)) == (2, 3)
    with pytest.raises(EvidenceError, match="normalized_quote_mismatch"):
        locate_quote("定律，成立", "定律,成立")
    with pytest.raises(EvidenceError, match="normalized_quote_mismatch"):
        locate_quote("速度", "逮度")


def test_terminal_groups_protected_suggestions_and_atomic_rejection(
    library: tuple[Database, TextbookService, dict[str, Any]],
) -> None:
    db, service, book = library
    first = service.ingest_text(book["id"], "这是前半句")[0]
    original = first["revision"]
    rejected_id = new_id()
    rejected_ops = [
        {"op": "insert", "id": "should-not-exist", "block": {"text": "不能留下"}},
        {"op": "update", "id": first["id"], "base_revision": 999, "changes": {"text": "旧版本"}},
    ]
    rejected = service.apply_operations(book["id"], rejected_id, rejected_ops)
    assert rejected["status"] == "rejected"
    assert db.get("block", "should-not-exist") is None
    assert service.apply_operations(book["id"], rejected_id, rejected_ops) == rejected
    fixed_ops = [
        {
            "op": "update",
            "id": first["id"],
            "base_revision": original,
            "changes": {"text": "人工修正的前半句"},
        }
    ]
    assert service.apply_operations(book["id"], rejected_id, fixed_ops)["reason"] == "idempotency_conflict"
    assert service.apply_operations(book["id"], new_id(), fixed_ops, actor="human")["status"] == "accepted"
    first = db.get("block", first["id"])
    assert first is not None
    result = service.apply_operations(
        book["id"],
        new_id(),
        [
            {
                "op": "node",
                "node": {
                    "id": "next-section",
                    "title": "课题：新内容",
                    "parent_id": service.ensure_root(book["id"])["id"],
                },
            },
            {
                "op": "insert",
                "id": "next-page",
                "block": {"text": "及下一页后半句", "node_id": "next-section", "source_page_ids": ["p2"]},
            },
            {
                "op": "merge",
                "ids": [first["id"], "next-page"],
                "base_revisions": {first["id"]: first["revision"]},
                "text": "人工修正的前半句及下一页后半句",
            },
        ],
        actor="ai",
    )
    assert result["status"] == "accepted"
    suggestion = result["suggestions"][0]
    assert db.get("node", "next-section")["title"] == "课题：新内容"
    assert db.get("block", "next-page")["text"] == "及下一页后半句"
    assert db.get("block", first["id"])["text"] == "人工修正的前半句"
    # A later human edit makes the proposal stale; accepting it must preserve both blocks.
    service.apply_operations(
        book["id"],
        new_id(),
        [
            {
                "op": "update",
                "id": first["id"],
                "base_revision": first["revision"],
                "changes": {"text": "再次校正"},
            }
        ],
        actor="human",
    )
    assert service.accept_suggestion(book["id"], suggestion["id"])["status"] == "stale"
    assert len(service.list_blocks(book["id"])) == 2


def test_merge_redirects_evidence_and_historical_snapshots(
    library: tuple[Database, TextbookService, dict[str, Any]],
) -> None:
    db, service, book = library
    blocks = service.ingest_text(book["id"], "第一段 加速度\n\n第二段 牛顿定律\n\n第三段 速度")
    blocks.sort(key=lambda item: item["order"])
    evidence = service.current_evidence(book["id"], blocks[1]["id"], blocks[1]["revision"], "牛顿定律")
    concept = service.record_concept(
        book["id"], {"name": "牛顿定律", "aliases": ["运动规律"], "evidence": [evidence]}
    )
    historical = db.put("explanation_snapshot", {"book_id": book["id"], "evidence": [evidence]})
    one = service.apply_operations(
        book["id"],
        new_id(),
        [
            {
                "op": "merge",
                "ids": [blocks[1]["id"], blocks[2]["id"]],
                "base_revisions": {item["id"]: item["revision"] for item in blocks[1:]},
            }
        ],
    )
    assert one["status"] == "accepted"
    middle = db.get("block", blocks[1]["id"])
    two = service.apply_operations(
        book["id"],
        new_id(),
        [
            {
                "op": "merge",
                "ids": [blocks[0]["id"], middle["id"]],
                "base_revisions": {blocks[0]["id"]: blocks[0]["revision"], middle["id"]: middle["revision"]},
            }
        ],
    )
    assert two["status"] == "accepted"
    redirects = records(db, "redirect", {"book_id": book["id"]})
    assert all(item["current_id"] == blocks[0]["id"] for item in redirects)
    current = db.get("concept", concept["id"])
    assert current["evidence"][0]["block_id"] == blocks[0]["id"]
    assert current["evidence"][0]["valid"]
    assert db.get("explanation_snapshot", historical["id"])["evidence"] == [evidence]
    hits = RetrievalService(db).search("运动规律", book_ids=[book["id"]], mode="keyword")
    assert [item["id"] for item in hits] == [blocks[0]["id"]]
    survivor = db.get("block", blocks[0]["id"])
    split = service.apply_operations(
        book["id"],
        new_id(),
        [
            {
                "op": "split",
                "id": survivor["id"],
                "base_revision": survivor["revision"],
                "parts": ["第一段 加速度", "第二段 牛顿定律\n\n第三段 速度"],
            }
        ],
    )
    assert split["status"] == "accepted"
    updated = db.get("concept", concept["id"])
    assert updated["evidence"][0]["valid"]
    assert updated["evidence"][0]["block_id"] != survivor["id"]


def test_scoped_fts_aliases_escaping_and_multiple_vector_dimensions(
    library: tuple[Database, TextbookService, dict[str, Any]],
) -> None:
    db, service, book = library
    first, second = sorted(
        service.ingest_text(book["id"], "自由落体运动具有加速度。\n\n匀速运动速度不变。"),
        key=lambda item: item["order"],
    )
    other = db.put("book", {"title": "语文", "subject_id": "chinese"})
    foreign_block = service.ingest_text(other["id"], "自由落体")[0]
    rejected_insert = service.apply_operations(
        book["id"], new_id(), [{"op": "insert", "id": foreign_block["id"], "block": {"text": "不能覆盖别书"}}]
    )
    assert rejected_insert["reason"] == "object_id_out_of_scope"
    assert db.get("block", foreign_block["id"])["text"] == "自由落体"
    foreign_root = service.ensure_root(other["id"])
    assert (
        service.apply_operations(
            book["id"],
            new_id(),
            [{"op": "node", "node": {"id": foreign_root["id"], "title": "不能覆盖目录", "parent_id": None}}],
        )["reason"]
        == "object_id_out_of_scope"
    )
    retrieval = RetrievalService(db)
    assert retrieval.search('自由落体 OR " *', book_ids=[book["id"]], mode="keyword")
    assert retrieval.search("自由落体运动", book_ids=[book["id"]], mode="phrase")[0]["id"] == first["id"]
    retrieval.store_embedding(first["id"], book["id"], first["revision"], "two", [1.0, 0.0])
    retrieval.store_embedding(second["id"], book["id"], second["revision"], "three", [1.0, 0.0, 0.0])
    hits = retrieval.search(
        "自由落体", book_ids=[book["id"]], mode="semantic", query_vector=[1.0, 0.0], space_fingerprint="two"
    )
    assert [item["id"] for item in hits] == [first["id"]]
    db.put("book", {**other, "deleted": True}, id=other["id"])
    with db.read() as conn:
        assert other["id"] not in retrieval.scoped_books(None, None, conn)
    with pytest.raises(ValueError, match="科目范围"):
        retrieval.search("自由落体", book_ids=[other["id"]], subject_id="physics")
    rejected = service.record_relation(
        book["id"], {"source_id": first["id"], "target_id": second["id"], "type": "related", "evidence": []}
    )
    assert rejected["reason"] == "missing_evidence"
    anchor = service.current_evidence(book["id"], first["id"], first["revision"], "自由落体运动")
    for number in range(db.settings.block_relation_limit):
        assert service.record_relation(
            book["id"],
            {
                "source_id": first["id"],
                "target_id": second["id"],
                "type": f"fixture_{number}",
                "evidence": [anchor],
            },
        )["valid"]
    assert (
        service.record_relation(
            book["id"],
            {
                "source_id": first["id"],
                "target_id": second["id"],
                "type": "one_too_many",
                "evidence": [anchor],
            },
        )["reason"]
        == "block_relation_limit"
    )

    # The same concept limit applies to incoming relations, not only outgoing ones.
    db.settings.concept_relation_limit = 1
    concept_blocks = service.ingest_text(book["id"], "概念甲的原文。\n\n概念乙的原文。\n\n概念丙的原文。")
    concepts = [
        service.record_concept(
            book["id"],
            {
                "name": block["text"],
                "evidence": [
                    service.current_evidence(book["id"], block["id"], block["revision"], block["text"])
                ],
            },
        )
        for block in concept_blocks
    ]
    incoming = {
        "source_id": concepts[0]["id"],
        "target_id": concepts[1]["id"],
        "type": "fixture_incoming",
        "evidence": concepts[0]["evidence"],
    }
    assert service.record_relation(book["id"], incoming)["valid"]
    assert (
        service.record_relation(
            book["id"], {**incoming, "source_id": concepts[2]["id"], "evidence": concepts[2]["evidence"]}
        )["reason"]
        == "concept_relation_limit"
    )
    # Idempotent updates of an existing relation do not consume another slot.
    assert service.record_relation(book["id"], incoming)["valid"]


def test_longbook_budget_lossless_units_and_gap(
    library: tuple[Database, TextbookService, dict[str, Any]],
) -> None:
    db, service, book = library
    with db.write() as conn:
        for index in range(300):
            db.put(
                "page",
                {"book_id": book["id"], "page_index": index, "text": f"第{index}页内容", "status": "draft"},
                id=f"p{index}",
                conn=conn,
            )
        service.ingest_text(book["id"], "远处第三页的原文", "p3", conn)
        service.ingest_text(book["id"], "近邻正文" * 3000, "p298", conn)
    service.skip_page(book["id"], "p298")
    builder = ContextBuilder(db, token_budget=4096)
    context = builder.build(book["id"], "p299")
    assert context["token_estimate"] <= 4096
    assert context["current_page"]["gap_before"]
    assert context["previous_blocks"] == []
    block = next(item for item in service.list_blocks(book["id"]) if "p3" in item["source_page_ids"])
    assert builder.read_block(book["id"], block["id"])["text"] == "远处第三页的原文"
    long = "跨页段落😀" * 2000 + "\n\n最后一段\n"
    units = builder.units(long, budget=512)
    assert "".join(units) == long
    assert all(estimate_tokens(unit) <= 512 for unit in units)
    page = db.get("page", "p299")
    assert page
    db.put("page", {**page, "text": "分块边界😀" * 300}, id=page["id"])
    initial = builder.build(book["id"], page["id"])["current_page"]
    expanded = ContextBuilder(db, token_budget=8192)
    for index in range(initial["unit_count"]):
        original = builder.build(book["id"], page["id"], unit_index=index)["current_page"]
        resumed = expanded.build(
            book["id"], page["id"], tools=["新工具定义"], unit_index=index, unit_budget=initial["unit_budget"]
        )["current_page"]
        assert resumed["text"] == original["text"] and resumed["unit_count"] == initial["unit_count"]


def test_restore_topology_as_group_creates_new_versions(
    library: tuple[Database, TextbookService, dict[str, Any]],
) -> None:
    db, service, book = library
    original = sorted(
        service.ingest_text(book["id"], "甲：速度\n\n乙：加速度"), key=lambda item: item["order"]
    )
    a, b = original
    concept = service.record_concept(
        book["id"],
        {
            "name": "加速度",
            "aliases": ["速度变化率"],
            "evidence": [service.current_evidence(book["id"], b["id"], b["revision"], "加速度")],
        },
    )
    assert (
        service.apply_operations(
            book["id"],
            new_id(),
            [
                {
                    "op": "merge",
                    "ids": [a["id"], b["id"]],
                    "base_revisions": {item["id"]: item["revision"] for item in original},
                }
            ],
        )["status"]
        == "accepted"
    )
    merged_a, merged_b = db.get("block", a["id"]), db.get("block", b["id"])
    assert service.apply_operations(
        book["id"],
        new_id(),
        [
            {
                "op": "restore",
                "id": a["id"],
                "base_revision": merged_a["revision"],
                "restore_revision": a["revision"],
            }
        ],
        actor="human",
    )["reason"].startswith("restore_requires_topology_group")
    restored = service.apply_operations(
        book["id"],
        new_id(),
        [
            {
                "op": "restore",
                "id": a["id"],
                "base_revision": merged_a["revision"],
                "restore_revision": a["revision"],
            },
            {
                "op": "restore",
                "id": b["id"],
                "base_revision": merged_b["revision"],
                "restore_revision": b["revision"],
            },
        ],
        actor="human",
    )
    assert restored["status"] == "accepted"
    current_a, current_b = db.get("block", a["id"]), db.get("block", b["id"])
    assert current_a["revision"] > merged_a["revision"]
    assert current_b["revision"] > merged_b["revision"]
    assert service.resolve_id(book["id"], b["id"]) == b["id"]
    assert [item["text"] for item in service.list_blocks(book["id"])] == [item["text"] for item in original]
    assert db.history("block", a["id"])[0]["text"] == a["text"]
    assert db.get("concept", concept["id"])["evidence"][0]["block_id"] == b["id"]
    hits = RetrievalService(db).search("速度变化率", book_ids=[book["id"]], mode="keyword")
    assert hits[0]["id"] == b["id"]
    # Re-merging reactivates a materialized redirect without overwriting its history.
    assert (
        service.apply_operations(
            book["id"],
            new_id(),
            [
                {
                    "op": "merge",
                    "ids": [a["id"], b["id"]],
                    "base_revisions": {a["id"]: current_a["revision"], b["id"]: current_b["revision"]},
                }
            ],
            actor="human",
        )["status"]
        == "accepted"
    )
