# Sunrise and sunset schedules

Lighting and power controller rules can choose a fixed time, sunrise or sunset
independently for switch-on and switch-off. Solar offsets are whole minutes from
-180 to +180. Negative values run before the event; positive values run after it.
Existing fixed clock windows and humidity rules keep their existing behavior.

## Location and preview

The source is the selected controller's Home Assistant connection, not the
browser's location, browser time zone or the backend server's location. The agent
publishes HA's configured home name, latitude, longitude and elevation alongside
its time zone. It refreshes that metadata on `core_config_updated` as well as on
reconnection. The backend validates and scopes it to that agent's household.

The editor automatically previews today's sunrise/sunset and upcoming on/off
events. Astral 3.2 uses the same observer inputs as Home Assistant. Calculations
run locally without a weather/geocoding API. Responses include explicit UTC
offsets; the UI formats them in the household's IANA time zone, including DST.
During a repeated autumn clock hour, fixed-time triggers appear twice with their
different UTC offsets. A nonexistent spring clock time is skipped, matching HA.
No solar event on a polar date is represented by a missing time and a warning.

Missing location metadata produces an explicit unavailable preview. Offline
connections show that their last reported location is being used. A time-zone
mismatch prevents the preview and existing deployment checks prevent activation.
The agent update is needed for location-aware previews; older agents can still
handle the generated native HA configuration.

## Execution semantics

When either endpoint uses the sun, the compiler emits two native HA event
triggers (`sun` with a seconds offset or `time`), with separate on/off IDs.
They recalculate sun times in HA and follow HA location changes without requiring
the app/backend to stay connected. No public ports or additional credentials are
needed. The existing device identity guards, brightness support and ownership
markers are preserved. Queued mode avoids dropping an off event while a preceding
on action is still running.

Selected weekdays apply to the unshifted solar event's local date (or the fixed
switch-on date). The off trigger runs every day, so it also closes overnight
schedules after the selected starting day. An on/off event acts at that moment;
it does not continuously enforce a desired state or replay events missed while
HA was stopped. The editor states this distinction. Fixed-only rules continue
using the established continuously reconciled time-window behavior.

Saving, automatic local-time previews and the explicit effect preview never
switch devices. A saved draft still needs activation. Existing stored rules
require no database migration: new fields have fixed-time defaults.

## Validation

`test_solar.py` covers native trigger output, offsets, brightness/identity guards,
weekday ownership across midnight, DST, ordering, missing metadata, offline
warnings, time-zone mismatch and polar events. The PostgreSQL integration tests
also exercise saving/reloading solar JSON, previewing the correct connection and
refusing access across households. Agent tests cover configuration metadata and
the HA configuration-change subscription.
