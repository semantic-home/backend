# Reusable automations

Controllers continue to own outputs inside a household and zone. Their rule editor composes validated building blocks, instead of using a different bespoke form for each use case. Programs are stored as `AutomationSpec.kind=program`, `program.version=2`. Legacy rules keep their previous compiler and stored settings until the user explicitly saves a converted draft.

## Building blocks

- Timing: all day on selected days; a window between clock, sunrise, sunset or sun-elevation anchors; or events from a clock/solar anchor, state change or numeric crossing. End anchors may also be an elapsed duration.
- Conditions: nested All, Any and Not groups containing state, numeric, clock-window or started-by-event checks. Numeric inputs must all be fresh; Not cannot turn missing data into permission to operate.
- Behaviour: maintain a measurement band (maximum/minimum/average, high or low demand and hysteresis); switch while permitted; follow an editable brightness/white curve driven by the sun, elapsed time or a sensor; or run ordered actions, waits and conditional branches.
- Outputs: all controller outputs or an explicit subset per action. Supported actions come from actual capabilities. Household and Home Assistant connection boundaries, physical-device identity checks and existing watering ownership protections remain enforced.

Presets only populate the same editable blocks. Natural dusk starts at sunset and follows the setting sun through civil (-6°), nautical (-12°), astronomical (-18°), and deeper darkness (-20°). The default holds its final dim level; force-off can be selected. Natural dawn starts dim at 06:00 and follows the real rising sun rather than replaying already-passed phases.

## Local execution and previews

The compiler emits native Home Assistant automations; it accepts no arbitrary services, Jinja or scripts from clients. Clock-based continuous control reconciles after start/reload. Clock curves resume only lights already on; an explicit daily start can switch them on. Event sequences and solar windows wait for the next start after HA restart/reload; missed events and interrupted waits are not replayed. Solar windows stop at the end anchor or configured maximum duration. Missing polar solar phases are shown in the preview and never replaced with a hidden clock schedule.

Curves update at minute intervals. Native transition support is used only when advertised by the lamp. White temperature is clamped separately to each lamp's supported range. Manual switch-off is respected by default. The UI's read-only preview uses the connection's home location and household timezone, plots the requested curve, lists local event times and shows measurement ages. It does not predict future sensor readings or confirm physical device action.

A stale measurement forces measurement-band outputs off. The configured maximum age must exceed the sensor's normal reporting interval. Controller cards show last-known readings, their age and the applicable limit; unavailable or stale values are not presented as fresh measurements. The existing humidity automation is not converted automatically.

## Release and rollback

No SQL migration is required. Back up the application database before deploying. Once users save version-2 programs, downgrade only to a backend that understands them, or first export/remove those new program records. Restoring the pre-release database is a last resort because it also discards subsequent application changes. Never blindly downgrade an older backend against newly saved program JSON.

Validation includes isolated PostgreSQL household/connection tests, generated-template behavior tests, frontend lint/build, and read-only validation of representative configurations against HA 2026.9.4. CI publishes and deploys main through the existing restricted WireGuard/SSH workflow. Tests and preview must never invoke real device actions.
