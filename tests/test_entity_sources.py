from __future__ import annotations

import unittest

from backend.entities_store.entities import EntityStore


class EntityStoreSourcesTest(unittest.TestCase):
    def test_can_filter_entities_by_source(self) -> None:
        store = EntityStore()

        store.upsert(
            "switch.demo_pump",
            "off",
            {"friendly_name": "Demo Pump"},
            source="seed",
        )
        store.upsert(
            "switch.real_pump",
            "off",
            {"friendly_name": "Real Pump"},
            source="agent",
            source_id="home1",
        )

        self.assertEqual([record.entity_id for record in store.list_by_source("seed")], ["switch.demo_pump"])
        self.assertEqual([record.entity_id for record in store.list_by_source("agent")], ["switch.real_pump"])
