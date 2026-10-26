# Natural dawn lighting rules

A lighting controller can use an **On / off schedule** or a **Natural dawn ramp**. The controller continues to own the zone and lamp assignments; the rule only defines their behaviour. A group must use dimmable `light` entities from the same household and Home Assistant connection.

## The 06:00 example

The default rule turns the selected lamps on at 06:00 local time at 1% brightness and warm white. It follows the actual morning sun at the home location configured in Home Assistant, rather than replaying a fixed sequence of phase durations.

| Sun elevation | Phase anchor | Default brightness | Default white |
| --- | --- | --- | --- |
| −18° | Astronomical | 1% | 2702 K |
| −12° | Nautical | 10% | 3000 K |
| −6° | Civil | 45% | 4000 K |
| 0° | Daylight | 100% | 5500 K |

Levels interpolate linearly with the rising sun's elevation. For this rule, “daylight” means the sun's centre at 0°; it is not the earlier instant at which the upper edge of the sun first becomes visible. The preview uses Astral, as Home Assistant does, and displays the phase crossings in the household time zone. Home Assistant updates its sun position periodically, so actual lamp settings can lag the continuous preview by its sun-update interval and the lamp's transition time.

The lamps always start dim, even if some or all dawn phases have passed by 06:00. During an adjustable **Gentle start** (10 minutes by default), they blend from the initial dim level to the current natural level. Passed phases are not replayed. In summer, if it is already daylight at switch-on, this is a gentle fade from 1% to the daylight setting.

Start time, stop time, weekdays, gentle-start duration, initial brightness, and each phase's brightness and Kelvin are editable. Brightness must stay level or increase. The default stop time is 12:00; the default end behaviour leaves the lamps at their last level. Users can choose to switch the group off at the stop time instead. Stops are on the same local day as starts.

## Lamp support and manual control

Warm-to-cool white requires `color_temp` support and valid Kelvin limits on every selected lamp. White temperature is limited to each lamp's individual range at execution time. Brightness-only groups omit colour data. RGB colours are not simulated by this rule.

The generated automation runs locally in Home Assistant, without a continuous connection to Semantic Home. It evaluates once a minute and only sends a level update when brightness changes by at least 1 percentage point or white temperature by at least 25 K. Lamps advertising transition support receive a 60-second transition on updates. The scheduled initial dim setting has no transition.

A minute update only acts on lamps that are already on. A manually switched-off lamp stays off until the next scheduled start (or until the user turns it on again). After HA starts or automations reload, lamps already on resume at the current natural level; lamps off are not switched on to replay a missed start. A missed stop trigger is also not replayed. Pausing or removing a rule stops future adjustments; it does not itself switch the group off.

Sun data that is missing or invalid prevents subsequent level adjustments; the fixed start can still set the initial dim level. A descending sun below the horizon does not reverse the morning ramp. At high latitudes, missing phase crossings are shown explicitly. Local clock times follow HA's daylight-saving behaviour: nonexistent clock triggers are skipped, repeated starts run twice. The preview warns about those cases.

## Preview and ownership

`POST /automations/preview` accepts `kind: lighting_ramp` and a `ramp` object. It returns a `ramp` preview containing local phase times, a sampled light curve, source location, per-lamp capabilities, and warnings. Saving and previewing do not queue device commands. Drafts must be activated separately.

This uses the existing automation ownership markers, registry bindings, physical-device identity guards, conflict checks, version checks, and household/connection isolation. No database migration is required: rule specifications remain versioned JSON in PostgreSQL. The agent's existing `automation.apply` protocol is unchanged. The solar-location metadata agent update is required for location previews; execution uses the local HA Sun integration.
