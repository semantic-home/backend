from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from backend.semantic.classifier import classify_entity, suggest_moisture_sensor_candidates


class ClassifierTests(unittest.TestCase):
    def test_uses_friendly_name_as_label_for_watering_entities(self) -> None:
        result = classify_entity(
            "switch.living_room_window_sill_monstera_pump",
            "off",
            {
                "friendly_name": "Living Room Window Sill Monstera Pump",
                "device_class": "switch",
                "area_id": "living_room",
            },
        )

        self.assertEqual(result.label, "Living Room Window Sill Monstera Pump")
        self.assertEqual(result.semantic_type, "water_pump")
        self.assertEqual(result.domain_hint, "watering")
        self.assertEqual(result.zone_hint, "living_room")

    def test_falls_back_to_generic_label_without_friendly_name(self) -> None:
        result = classify_entity(
            "switch.irrigation_valve",
            "off",
            {},
        )

        self.assertEqual(result.label, "Water Pump")

    def test_classifies_switch_water_pump(self) -> None:
        result = classify_entity(
            "switch.water_pump_1",
            "off",
            {
                "friendly_name": "Water Pump 1",
            },
        )

        self.assertEqual(result.semantic_type, "water_pump")
        self.assertEqual(result.domain_hint, "watering")
        self.assertEqual(result.role, "actuator")

    def test_suggests_moisture_sensor_candidates_for_watering_pumps(self) -> None:
        pump = classify_entity(
            "switch.living_room_window_sill_monstera_pump",
            "off",
            {
                "friendly_name": "Living Room Window Sill Monstera Pump",
                "device_class": "switch",
                "area_id": "living_room",
            },
        )
        exact_sensor = classify_entity(
            "sensor.living_room_window_sill_monstera_soil",
            "31",
            {
                "friendly_name": "Living Room Window Sill Monstera Soil Sensor",
                "unit_of_measurement": "%",
                "device_class": "moisture",
                "area_id": "living_room",
            },
        )
        same_room_sensor = classify_entity(
            "sensor.living_room_bookshelf_ficus_soil",
            "44",
            {
                "friendly_name": "Living Room Bookshelf Ficus Soil Sensor",
                "unit_of_measurement": "%",
                "device_class": "moisture",
                "area_id": "living_room",
            },
        )

        suggestions = suggest_moisture_sensor_candidates([pump, exact_sensor, same_room_sensor])

        self.assertEqual(
            suggestions[pump.entity_id],
            [exact_sensor.entity_id, same_room_sensor.entity_id],
        )

    def test_classifies_moisture_named_sensor_without_device_class(self) -> None:
        result = classify_entity(
            "sensor.moisture_left",
            "31",
            {
                "friendly_name": "Moisture Left",
            },
        )

        self.assertEqual(result.semantic_type, "moisture_sensor")
        self.assertEqual(result.domain_hint, "watering")
        self.assertEqual(result.confidence, "low")

    def test_does_not_classify_battery_companion_sensor_as_moisture_sensor(self) -> None:
        result = classify_entity(
            "sensor.soil_moisture_sensor_middle_battery",
            "92",
            {
                "friendly_name": "Soil Moisture Sensor Middle Batterie",
                "unit_of_measurement": "%",
                "device_class": "battery",
            },
        )

        self.assertNotEqual(result.semantic_type, "moisture_sensor")
        self.assertNotEqual(result.domain_hint, "watering")

    def test_falls_back_to_available_moisture_sensors_without_exact_match(self) -> None:
        pump = classify_entity(
            "switch.watering_pump",
            "off",
            {
                "friendly_name": "Watering Pump",
            },
        )
        left_sensor = classify_entity(
            "sensor.moisture_left",
            "31",
            {
                "friendly_name": "Moisture Left",
            },
        )
        right_sensor = classify_entity(
            "sensor.moisture_right",
            "44",
            {
                "friendly_name": "Moisture Right",
            },
        )

        suggestions = suggest_moisture_sensor_candidates([pump, left_sensor, right_sensor])

        self.assertEqual(
            suggestions[pump.entity_id],
            [left_sensor.entity_id, right_sensor.entity_id],
        )

    def test_generic_pump_returns_all_non_battery_moisture_candidates(self) -> None:
        pump = classify_entity(
            "switch.water_pump_1",
            "off",
            {
                "friendly_name": "Water Pump 1",
            },
        )
        left_sensor = classify_entity(
            "sensor.soil_moisture_sensor_left_soil_moisture",
            "31",
            {
                "friendly_name": "Soil Moisture Sensor Left Feuchtigkeit",
                "unit_of_measurement": "%",
            },
        )
        middle_battery = classify_entity(
            "sensor.soil_moisture_sensor_middle_battery",
            "92",
            {
                "friendly_name": "Soil Moisture Sensor Middle Batterie",
                "unit_of_measurement": "%",
                "device_class": "battery",
            },
        )
        middle_sensor = classify_entity(
            "sensor.soil_moisture_sensor_middle_soil_moisture",
            "38",
            {
                "friendly_name": "Soil Moisture Sensor Middle Feuchtigkeit",
                "unit_of_measurement": "%",
            },
        )
        right_sensor = classify_entity(
            "sensor.soil_moisture_sensor_right_soil_moisture",
            "29",
            {
                "friendly_name": "Soil Moisture Sensor Right Feuchtigkeit",
                "unit_of_measurement": "%",
            },
        )

        suggestions = suggest_moisture_sensor_candidates([
            pump,
            left_sensor,
            middle_battery,
            middle_sensor,
            right_sensor,
        ])

        self.assertEqual(
            suggestions[pump.entity_id],
            [
                left_sensor.entity_id,
                middle_sensor.entity_id,
                right_sensor.entity_id,
            ],
        )
