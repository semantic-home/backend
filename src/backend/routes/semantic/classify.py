from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, HTTPException, Query, Request

from backend.schemas.classification import ClassificationOverride, EntityClassificationView
from backend.semantic.classifier import EntityClassification, classify_entity, suggest_moisture_sensor_candidates
from backend.session_context import get_request_session_id

classify_router = APIRouter()

_classification_overrides: dict[str, dict[str, str]] = {}


def clear_classification_overrides() -> None:
    _classification_overrides.clear()


def _apply_overrides(
    base: EntityClassification,
    overrides: dict[str, dict[str, str]],
) -> EntityClassificationView:
    data = vars(base)
    override = overrides.get(base.entity_id, {})
    data.update(override)
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


def _global_overrides() -> dict[str, dict[str, str]]:
    return _classification_overrides


def _demo_overrides(request: Request) -> dict[str, dict[str, str]]:
    from backend.__main__ import demo_sessions

    session = demo_sessions.get(get_request_session_id(request))
    return session.classification_overrides


def _classify_records(
    records,
    *,
    overrides: dict[str, dict[str, str]],
) -> list[EntityClassificationView]:
    return [
        _apply_overrides(classify_entity(rec.entity_id, rec.state, rec.attributes), overrides)
        for rec in records
    ]


@classify_router.get("/semantic/classifications", response_model=list[EntityClassificationView])
async def list_classifications(
    request: Request,
    source: Literal["all", "agent", "seed"] = Query(default="all"),
) -> list[EntityClassificationView]:
    from backend.__main__ import demo_sessions, entity_store

    session = demo_sessions.get(get_request_session_id(request))

    if source == "agent":
        classifications = _classify_records(
            entity_store.list_by_source("agent"),
            overrides=_global_overrides(),
        )
    elif source == "seed":
        classifications = _classify_records(
            session.entity_store.list_by_source("seed"),
            overrides=_demo_overrides(request),
        )
    else:
        classifications = _classify_records(
            entity_store.list_by_source("agent"),
            overrides=_global_overrides(),
        ) + _classify_records(
            session.entity_store.list_by_source("seed"),
            overrides=_demo_overrides(request),
        )

    return _with_sensor_suggestions(classifications)


@classify_router.get("/semantic/classifications/{entity_id:path}", response_model=EntityClassificationView)
async def get_classification(request: Request, entity_id: str) -> EntityClassificationView:
    from backend.__main__ import demo_sessions, entity_store

    session = demo_sessions.get(get_request_session_id(request))

    rec = session.entity_store.get(entity_id)
    if rec is not None:
        return _with_sensor_suggestions([
            _apply_overrides(
                classify_entity(rec.entity_id, rec.state, rec.attributes),
                _demo_overrides(request),
            )
        ])[0]

    rec = entity_store.get(entity_id)
    if rec is None:
        raise HTTPException(status_code=404, detail="Unknown entity_id")

    return _with_sensor_suggestions([
        _apply_overrides(
            classify_entity(rec.entity_id, rec.state, rec.attributes),
            _global_overrides(),
        )
    ])[0]


@classify_router.put("/semantic/classifications/{entity_id:path}", response_model=EntityClassificationView)
async def update_classification(request: Request, entity_id: str, body: ClassificationOverride) -> EntityClassificationView:
    from backend.__main__ import demo_sessions, entity_store

    session = demo_sessions.get(get_request_session_id(request))

    rec = session.entity_store.get(entity_id)
    overrides = _demo_overrides(request)
    if rec is None:
        rec = entity_store.get(entity_id)
        overrides = _global_overrides()

    if rec is None:
        raise HTTPException(status_code=404, detail="Unknown entity_id")

    override = {k: v for k, v in body.model_dump().items() if v is not None}
    if override:
        overrides[entity_id] = {
            **overrides.get(entity_id, {}),
            **override,
        }

    return _with_sensor_suggestions([
        _apply_overrides(classify_entity(rec.entity_id, rec.state, rec.attributes), overrides)
    ])[0]
