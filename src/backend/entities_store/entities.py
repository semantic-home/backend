from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Literal

EntitySource = Literal["agent", "seed"]

@dataclass
class EntityRecord:
    entity_id: str
    state: str
    attributes: Dict[str, Any] = field(default_factory=dict)
    source: EntitySource = "agent"
    source_id: str | None = None

class EntityStore:
    def __init__(self) -> None:
        self._entities: Dict[str, EntityRecord] = {}

    def clear(self) -> None:
        self._entities.clear()

    def upsert(
        self,
        entity_id: str,
        state: str,
        attributes: Dict[str, Any],
        *,
        source: EntitySource = "agent",
        source_id: str | None = None,
    ) -> None:
        self._entities[entity_id] = EntityRecord(
            entity_id=entity_id,
            state=state,
            attributes=attributes,
            source=source,
            source_id=source_id,
        )

    def list_all(self) -> List[EntityRecord]:
        return list(self._entities.values())

    def list_by_source(self, source: EntitySource) -> List[EntityRecord]:
        return [record for record in self._entities.values() if record.source == source]

    def get(self, entity_id: str) -> EntityRecord | None:
        return self._entities.get(entity_id)
