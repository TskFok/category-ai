from __future__ import annotations

import csv
import io
import json
import xml.etree.ElementTree as ET
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.errors import AppError
from app.domain.enums import ReviewStatus
from app.infrastructure.db.models import (
    Alias,
    Character,
    CharacterState,
    Event,
    ExportArtifact,
    Fact,
    Novel,
    Relationship,
)
from app.infrastructure.storage.local import LocalStorage


def create_export(
    db: Session,
    storage: LocalStorage,
    *,
    novel_id: str,
    fmt: str,
) -> ExportArtifact:
    novel = db.get(Novel, novel_id)
    if not novel:
        raise AppError(code="NOVEL_NOT_FOUND", message="Novel not found", status_code=404)

    fmt_lower = fmt.lower().strip()
    if fmt_lower not in ("json", "csv", "graphml"):
        raise AppError(
            code="UNSUPPORTED_EXPORT_FORMAT",
            message="Export format must be one of: json, csv, graphml",
            status_code=400,
        )

    characters = list(
        db.scalars(
            select(Character)
            .where(
                Character.novel_id == novel.id,
                Character.status != ReviewStatus.SUPERSEDED.value,
            )
            .order_by(Character.mention_count.desc())
        ).all()
    )
    aliases = list(db.scalars(select(Alias).where(Alias.novel_id == novel.id)).all())
    relationships = list(
        db.scalars(select(Relationship).where(Relationship.novel_id == novel.id)).all()
    )
    facts = list(db.scalars(select(Fact).where(Fact.novel_id == novel.id)).all())
    states = list(db.scalars(select(CharacterState).where(CharacterState.novel_id == novel.id)).all())
    events = list(
        db.scalars(
            select(Event).where(Event.novel_id == novel.id).order_by(Event.narrative_order.asc())
        ).all()
    )

    artifact = ExportArtifact(
        novel_id=novel.id,
        format=fmt_lower,
        status="ready",
        storage_ref="",
        data_revision=novel.data_revision,
        file_size_bytes=0,
    )
    db.add(artifact)
    db.flush()

    if fmt_lower == "json":
        payload = {
            "exported_at": datetime.now(timezone.utc).isoformat(),
            "novel": {
                "id": novel.id,
                "title": novel.title,
                "author": novel.author,
                "chapter_count": novel.chapter_count,
                "total_chars": novel.total_chars,
                "data_revision": novel.data_revision,
            },
            "characters": [
                {
                    "id": c.id,
                    "canonical_name": c.canonical_name,
                    "entity_type": c.entity_type,
                    "status": c.status,
                    "first_appearance_chapter": c.first_appearance_chapter,
                    "mention_count": c.mention_count,
                    "summary": c.summary,
                }
                for c in characters
            ],
            "aliases": [
                {
                    "id": a.id,
                    "character_id": a.character_id,
                    "alias_text": a.alias_text,
                    "valid_from_chapter": a.valid_from_chapter,
                    "valid_to_chapter": a.valid_to_chapter,
                    "review_status": a.review_status,
                }
                for a in aliases
            ],
            "states": [
                {
                    "id": s.id,
                    "character_id": s.character_id,
                    "state_type": s.state_type,
                    "value": s.value_json,
                    "valid_from": s.valid_from,
                    "valid_to": s.valid_to,
                }
                for s in states
            ],
            "facts": [
                {
                    "id": f.id,
                    "subject_id": f.subject_id,
                    "predicate": f.predicate,
                    "value": f.value_json,
                    "chapter_no": f.chapter_no,
                    "review_status": f.review_status,
                }
                for f in facts
            ],
            "relationships": [
                {
                    "id": r.id,
                    "source_character_id": r.source_character_id,
                    "target_character_id": r.target_character_id,
                    "relation_type": r.relation_type,
                    "direction": r.direction,
                    "start_chapter": r.start_chapter,
                    "end_chapter": r.end_chapter,
                    "review_status": r.review_status,
                }
                for r in relationships
            ],
            "events": [
                {
                    "id": e.id,
                    "title": e.title,
                    "event_type": e.event_type,
                    "chapter_no": e.chapter_no,
                    "narrative_order": e.narrative_order,
                    "story_time_text": e.story_time_text,
                    "review_status": e.review_status,
                }
                for e in events
            ],
        }
        data = json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")
    elif fmt_lower == "csv":
        buf = io.StringIO()
        writer = csv.writer(buf)
        writer.writerow(
            [
                "id",
                "canonical_name",
                "entity_type",
                "status",
                "first_appearance_chapter",
                "mention_count",
                "summary",
            ]
        )
        for c in characters:
            writer.writerow(
                [
                    c.id,
                    c.canonical_name,
                    c.entity_type,
                    c.status,
                    c.first_appearance_chapter or "",
                    c.mention_count,
                    c.summary or "",
                ]
            )
        data = buf.getvalue().encode("utf-8-sig")
    else:
        # GraphML
        root = ET.Element(
            "graphml",
            xmlns="http://graphml.graphdrawing.org/xmlns",
        )
        ET.SubElement(root, "key", id="d_name", **{"for": "node", "attr.name": "name", "attr.type": "string"})
        ET.SubElement(root, "key", id="d_type", **{"for": "node", "attr.name": "entity_type", "attr.type": "string"})
        ET.SubElement(root, "key", id="d_rel", **{"for": "edge", "attr.name": "relation_type", "attr.type": "string"})
        graph = ET.SubElement(root, "graph", id=novel.id, edgedefault="directed")

        char_ids = {c.id for c in characters}
        for c in characters:
            node = ET.SubElement(graph, "node", id=c.id)
            d1 = ET.SubElement(node, "data", key="d_name")
            d1.text = c.canonical_name
            d2 = ET.SubElement(node, "data", key="d_type")
            d2.text = c.entity_type

        for r in relationships:
            if r.source_character_id in char_ids and r.target_character_id in char_ids:
                edge = ET.SubElement(
                    graph,
                    "edge",
                    id=r.id,
                    source=r.source_character_id,
                    target=r.target_character_id,
                )
                d_rel = ET.SubElement(edge, "data", key="d_rel")
                d_rel.text = r.relation_type

        data = ET.tostring(root, encoding="utf-8", xml_declaration=True)

    rel_path, size = storage.save_export(novel.id, artifact.id, fmt_lower, data)
    artifact.storage_ref = rel_path
    artifact.file_size_bytes = size
    db.commit()
    db.refresh(artifact)
    return artifact


def list_exports(db: Session, novel_id: str) -> list[ExportArtifact]:
    return list(
        db.scalars(
            select(ExportArtifact)
            .where(ExportArtifact.novel_id == novel_id)
            .order_by(ExportArtifact.created_at.desc())
        ).all()
    )


def get_export(db: Session, export_id: str) -> ExportArtifact:
    artifact = db.get(ExportArtifact, export_id)
    if not artifact:
        raise AppError(code="EXPORT_NOT_FOUND", message="Export artifact not found", status_code=404)
    return artifact
