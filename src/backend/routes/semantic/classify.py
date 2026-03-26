from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, HTTPException, Query

from backend.schemas.classification import ClassificationOverride, EntityClassificationView
from backend.semantic.classifier import EntityClassification, classify_entity, suggest_moisture_sensor_candidates

classify_router = APIRouter()

_classification_overrides: dict[str, dict[str, str]] = {}


def clear_classification_overrides() -> None:
    _classification_overrides.clear()


def _apply_overrides(base: EntityClassification) -> EntityClassificationView:
    data = vars(base)
    overrides = _classification_overrides.get(base.entity_id, {})
    data.update(overrides)
    return EntityClassificationView(**data)


def _with_sensor_suggestions(
    classifications: list[EntityClassificationView],
) -> list[EntityClassificationView]:
    suggestions = suggest_moisture_sensor_candidates(classifications)
    return [
        classification.model_copy(
            update={
                "suggested_moisture_sensor_entity_ids": suggestions.get(classification.entity_id, []),
            }
        )
        for classification in classifications
    ]


@classify_router.get("/semantic/classifications", response_model=list[EntityClassificationView])
async def list_classifications(
    source: Literal["all", "agent", "seed"] = Query(default="all"),
) -> list[EntityClassificationView]:
    from backend.__main__ import entity_store

    if source == "agent":
        records = entity_store.list_by_source("agent")
    elif source == "seed":
        records = entity_store.list_by_source("seed")
    else:
        records = entity_store.list_all()

    return _with_sensor_suggestions([
        _apply_overrides(classify_entity(rec.entity_id, rec.state, rec.attributes))
        for rec in records
    ])


@classify_router.get("/semantic/classifications/{entity_id:path}", response_model=EntityClassificationView)
async def get_classification(entity_id: str) -> EntityClassificationView:
    from backend.__main__ import entity_store

    rec = entity_store.get(entity_id)
    if rec is None:
        raise HTTPException(status_code=404, detail="Unknown entity_id")

    return _with_sensor_suggestions([
        _apply_overrides(classify_entity(rec.entity_id, rec.state, rec.attributes))
    ])[0]


@classify_router.put("/semantic/classifications/{entity_id:path}", response_model=EntityClassificationView)
async def update_classification(entity_id: str, body: ClassificationOverride) -> EntityClassificationView:
    from backend.__main__ import entity_store

    rec = entity_store.get(entity_id)
    if rec is None:
        raise HTTPException(status_code=404, detail="Unknown entity_id")

    override = {k: v for k, v in body.model_dump().items() if v is not None}
    if override:
        _classification_overrides[entity_id] = {
            **_classification_overrides.get(entity_id, {}),
            **override,
        }

    return _with_sensor_suggestions([
        _apply_overrides(classify_entity(rec.entity_id, rec.state, rec.attributes))
    ])[0]
