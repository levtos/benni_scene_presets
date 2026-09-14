import voluptuous as vol
import homeassistant.helpers.config_validation as cv
import asyncio
import logging
from homeassistant.const import EVENT_HOMEASSISTANT_STOP
from homeassistant.core import Event, HomeAssistant, SupportsResponse
from homeassistant.config_entries import ConfigEntry
from homeassistant.helpers.dispatcher import async_dispatcher_send
from .const import *

from .dynamic_scenes import DynamicScene, DynamicSceneManager
from .presets import apply_preset
from .view import async_setup_view, async_remove_view
from .util import ensure_list, resolve_targets
from .websocket_api import async_setup_websocket_api
from . import file_utils

CONFIG_SCHEMA = cv.empty_config_schema(DOMAIN)

APPLY_PRESET_SCHEMA = vol.Schema({
    vol.Required(ATTR_SCENE_PRESET_ID): cv.string,
    vol.Required(ATTR_TARGETS): vol.Any(dict),
    vol.Optional(ATTR_BRIGHTNESS): vol.Coerce(int),
    vol.Optional(ATTR_TRANSITION, default=1): vol.Coerce(int),
    vol.Optional(ATTR_SHUFFLE, default=False): cv.boolean,
    vol.Optional(ATTR_SMART_SHUFFLE, default=False): cv.boolean
})

START_DYNAMIC_SCENE_SCHEMA = vol.Schema({
    vol.Required(ATTR_SCENE_PRESET_ID): cv.string,
    vol.Required(ATTR_TARGETS): vol.Any(dict),
    vol.Optional(ATTR_INTERVAL, default=60): vol.Coerce(int),
    vol.Optional(ATTR_BRIGHTNESS): vol.Coerce(int),
    vol.Optional(ATTR_TRANSITION, default=1): vol.Coerce(int),
    # Transition for the very first paint. If omitted, the requested transition
    # is used (smooth crossfade) instead of the upstream hard-coded 0.5s snap.
    # Set to 0.5 to restore the original behaviour.
    vol.Optional(ATTR_INITIAL_TRANSITION): vol.Coerce(float),
})

STOP_DYNAMIC_SCENE_SCHEMA = vol.Schema({
    vol.Required(ATTR_DYNAMIC_SCENE_ID): cv.string,
})

STOP_DYNAMIC_SCENES_FOR_TARGETS_SCHEMA = vol.Schema({
    vol.Required(ATTR_TARGETS): vol.Any(dict),
})

APPLY_LOOK_SCHEMA = vol.Schema({
    vol.Required(ATTR_LOOK_ID): cv.string,
    # Brightness override (typically supplied by the light policy / day phase).
    vol.Optional(ATTR_BRIGHTNESS): vol.Coerce(int),
    # First-paint fade override (seconds). When set, overrides the look-level
    # crossfade for this apply — e.g. the light policy sends a short value for a
    # brightness-only change so it shows immediately instead of over the long
    # look crossfade. Omitted = keep the look/scene default.
    vol.Optional(ATTR_TRANSITION): vol.Coerce(float),
})

STOP_LOOK_SCHEMA = vol.Schema({
    vol.Required(ATTR_LOOK_ID): cv.string,
})

RESET_USERDATA_SCHEMA = vol.Schema({
    vol.Optional("delete_images", default=True): cv.boolean,
})


_LOGGER = logging.getLogger(__name__)

dynamic_scene_manager = DynamicSceneManager()


async def async_setup(hass, config):
    async def apply_preset_service(call):
        preset_id = call.data.get(ATTR_SCENE_PRESET_ID)
        brightness_override = call.data.get(ATTR_BRIGHTNESS)
        transition = call.data.get(ATTR_TRANSITION, 1)
        shuffle = call.data.get(ATTR_SHUFFLE, False)
        smart_shuffle = call.data.get(ATTR_SMART_SHUFFLE, False)

        light_entity_ids = _resolve(call.data.get(ATTR_TARGETS))

        await apply_preset(
            hass,
            preset_id,
            light_entity_ids,
            transition,
            shuffle,
            smart_shuffle,
            brightness_override
        )


    def _resolve(targets):
        return resolve_targets(
            hass,
            ensure_list(targets.get("entity_id")),
            ensure_list(targets.get("device_id")),
            ensure_list(targets.get("area_id")),
            ensure_list(targets.get("floor_id")),
            ensure_list(targets.get("label_id")),
        )

    async def _wake_lights(entity_ids):
        """Turn on any lights that are currently off, then let them settle.

        Aqara T1M-style lights sit in a deep standby when off: the first
        scene/effect payload arrives before the device is ready and is dropped,
        so the effect only "takes" on the second apply. We adopt Aqara Advanced
        Lighting's _ensure_light_on approach (absent42): turn the light on with
        a blocking call and wait a beat before sending the real command, so the
        device is awake when the scene/effect payload lands.
        """
        off_lights = [
            eid for eid in entity_ids
            if eid and eid.startswith("light.")
            and (state := hass.states.get(eid)) and state.state == "off"
        ]
        if not off_lights:
            return
        try:
            await hass.services.async_call(
                "light", "turn_on", {"entity_id": off_lights}, blocking=True
            )
            await asyncio.sleep(0.3)  # let the device wake before the real command
        except Exception as ex:  # noqa: BLE001 - best-effort wake, never block the look
            _LOGGER.warning("Could not pre-wake lights %s: %s", off_lights, ex)

    def _start_scene(preset_ident, light_entity_ids, interval, brightness, transition, initial_transition, look=None):
        # always stop any existing actions on these lights first
        for light_entity_id in light_entity_ids:
            dynamic_scene_manager.stop_all_for_entity_id(light_entity_id)

        return dynamic_scene_manager.create_new(
            hass,
            {
                "light_entity_ids": light_entity_ids,
                ATTR_SCENE_PRESET_ID: preset_ident,
                ATTR_BRIGHTNESS: brightness,
                ATTR_TRANSITION: transition,
                ATTR_INITIAL_TRANSITION: initial_transition,
                ATTR_SHUFFLE: True,
                "look": look,  # slug of the look this scene belongs to (for the look switch)
            },
            interval
        )

    async def start_dynamic_scene(call):
        light_entity_ids = _resolve(call.data.get(ATTR_TARGETS))

        return _start_scene(
            call.data.get(ATTR_SCENE_PRESET_ID),
            light_entity_ids,
            call.data.get(ATTR_INTERVAL),
            call.data.get(ATTR_BRIGHTNESS),
            call.data.get(ATTR_TRANSITION, 1),
            call.data.get(ATTR_INITIAL_TRANSITION),
        )

    async def apply_look(call):
        look_ident = call.data.get(ATTR_LOOK_ID)
        brightness = call.data.get(ATTR_BRIGHTNESS)
        transition_override = call.data.get(ATTR_TRANSITION)

        look = file_utils.get_look(look_ident)
        if not look:
            _LOGGER.warning("Look '%s' not found; apply_look skipped", look_ident)
            dynamic_scene_manager.stop_all_for_look(look_ident)
            return {"dynamic_scenes": [], "look_found": False}

        look_slug = look.get("slug")
        look_transition = look.get("transition")  # look-level default crossfade (s)
        dynamic_scene_manager.mark_look_active(look_slug)
        started = []
        for binding in look.get("bindings", []):
            kind = binding.get("kind", "scene")

            if kind == "off":
                # Explicitly turn these lights off (any vendor), fading over the
                # look transition so a CCT->colour-on-another-light switch doesn't
                # produce a hard dark flash.
                off_targets = _resolve(binding.get("targets", {}))
                if off_targets:
                    data = {"entity_id": off_targets}
                    off_transition = (
                        transition_override if transition_override is not None
                        else look_transition
                    )
                    if off_transition is not None:
                        data["transition"] = off_transition
                    hass.async_create_task(
                        hass.services.async_call("light", "turn_off", data, blocking=False)
                    )
                continue

            if kind == "aqara":
                # Named Aqara preset → call its AAL service on the targets.
                aqara = file_utils.get_aqara(binding.get("aqara"))
                raw_targets = ensure_list((binding.get("targets") or {}).get("entity_id"))
                if aqara and aqara.get("service") and raw_targets:
                    data = dict(aqara.get("data") or {})
                    data["entity_id"] = raw_targets
                    # Make the effect fire even if the light was off (only
                    # set_dynamic_effect accepts turn_on; don't add it to others).
                    if aqara["service"] == "set_dynamic_effect":
                        data.setdefault("turn_on", True)
                    # Pre-wake: AAL sends the effect payload before turning the
                    # light on, which a cold-standby T1M drops (effect only takes
                    # on the 2nd apply). Wake it first so the payload lands awake.
                    await _wake_lights(raw_targets)
                    hass.async_create_task(
                        hass.services.async_call(AQARA_DOMAIN, aqara["service"], data, blocking=False)
                    )
                continue

            if kind == "effect":
                # Generic service binding (raw service + data — fallback/advanced).
                # Pass the configured targets straight through (not light-only
                # resolved) so non-light services work.
                service = binding.get("service")
                raw_targets = ensure_list((binding.get("targets") or {}).get("entity_id"))
                if service and "." in service and raw_targets:
                    domain, svc = service.split(".", 1)
                    data = dict(binding.get("data") or {})
                    data["entity_id"] = raw_targets
                    hass.async_create_task(
                        hass.services.async_call(domain, svc, data, blocking=False)
                    )
                continue

            if kind == "switch":
                action = binding.get("action") or "turn_on"
                if action not in ("turn_on", "turn_off"):
                    action = "turn_on"
                switch_targets = [
                    eid for eid in ensure_list((binding.get("targets") or {}).get("entity_id"))
                    if eid and eid.startswith("switch.")
                ]
                if switch_targets:
                    hass.async_create_task(
                        hass.services.async_call(
                            "switch", action, {"entity_id": switch_targets}, blocking=False
                        )
                    )
                continue

            light_entity_ids = _resolve(binding.get("targets", {}))
            if not light_entity_ids:
                continue

            scene_ident = binding.get("scene") or binding.get("scene_id")
            if not scene_ident:
                continue

            interval = binding.get("interval")
            transition = binding.get("transition")
            if interval is None or transition is None:
                preset = file_utils.find_preset(scene_ident)
                if preset:
                    if interval is None:
                        interval = preset.get("interval", 60)
                    if transition is None:
                        transition = preset.get("transition", 1)
            # Binding transition wins; otherwise the look-level transition (the
            # crossfade for this look) overrides the scene's own first-paint time.
            initial_transition = None
            if binding.get("transition") is None and look_transition is not None:
                initial_transition = look_transition
            # Explicit per-call override (e.g. brightness-only re-apply) wins, so
            # the change shows over a short fade instead of the long look crossfade.
            if transition_override is not None:
                initial_transition = transition_override

            # Pre-wake off lights (esp. Aqara CCT ceilings) so the scene's first
            # paint isn't dropped while the device is still in standby.
            await _wake_lights(light_entity_ids)

            started.append(_start_scene(
                scene_ident,
                light_entity_ids,
                interval if interval is not None else 60,
                brightness,
                transition if transition is not None else 1,
                initial_transition,
                look_slug,
            ))

        return {"dynamic_scenes": started}

    async def stop_look(call):
        look_ident = call.data.get(ATTR_LOOK_ID)
        look = file_utils.get_look(look_ident)
        if not look:
            await dynamic_scene_manager.async_stop_all_for_look(look_ident)
            return

        await dynamic_scene_manager.async_stop_all_for_look(look.get("slug"))
        effect_off = []
        for binding in look.get("bindings", []):
            kind = binding.get("kind")
            if kind == "aqara":
                aqara = file_utils.get_aqara(binding.get("aqara"))
                raw_targets = ensure_list((binding.get("targets") or {}).get("entity_id"))
                if raw_targets:
                    stop_svc = AQARA_STOP_SERVICES.get((aqara or {}).get("service"), "stop_dynamic_scene")
                    stop_data = {"entity_id": raw_targets}
                    if stop_svc == "stop_effect":
                        stop_data["restore_state"] = False
                    await hass.services.async_call(
                        AQARA_DOMAIN, stop_svc, stop_data, blocking=True
                    )
                    effect_off += raw_targets
            elif kind == "effect":
                # Best-effort: turn the effect targets (e.g. the RGB ring) off.
                effect_off += _resolve(binding.get("targets", {}))

        if effect_off:
            await hass.services.async_call(
                "light",
                "turn_off",
                {"entity_id": list(dict.fromkeys(effect_off))},
                blocking=True,
            )

    async def reset_userdata(call):
        # Stop everything, wipe custom scenes + looks, drop the look switches.
        dynamic_scene_manager.stop_all()
        counts = await hass.async_add_executor_job(
            file_utils.reset_userdata, call.data.get("delete_images", True)
        )
        async_dispatcher_send(hass, SIGNAL_LOOKS_CHANGED)
        return counts

    async def stop_dynamic_scene(call):
        scene_id = call.data.get(ATTR_DYNAMIC_SCENE_ID)

        dynamic_scene_manager.delete_by_id(scene_id)

    async def stop_dynamic_scenes_for_targets(call):
        for light_entity_id in _resolve(call.data.get(ATTR_TARGETS)):
            dynamic_scene_manager.stop_all_for_entity_id(light_entity_id)

        return True

    async def stop_all_dynamic_scenes(call):
        dynamic_scene_manager.stop_all()

    async def get_dynamic_scenes(call):
        return dynamic_scene_manager.get_all_as_dict()


    hass.services.async_register(
        DOMAIN,
        SERVICE_APPLY_PRESET,
        apply_preset_service,
        schema=APPLY_PRESET_SCHEMA,
    )

    hass.services.async_register(
        DOMAIN,
        SERVICE_GET_DYNAMIC_SCENES,
        get_dynamic_scenes,
        supports_response=SupportsResponse.ONLY
    )

    hass.services.async_register(
        DOMAIN,
        SERVICE_START_DYNAMIC_SCENE,
        start_dynamic_scene,
        schema=START_DYNAMIC_SCENE_SCHEMA,
        supports_response=SupportsResponse.OPTIONAL
    )

    hass.services.async_register(
        DOMAIN,
        SERVICE_STOP_DYNAMIC_SCENE,
        stop_dynamic_scene,
        schema=STOP_DYNAMIC_SCENE_SCHEMA
    )

    hass.services.async_register(
        DOMAIN,
        SERVICE_STOP_DYNAMIC_SCENES_FOR_TARGETS,
        stop_dynamic_scenes_for_targets,
        schema=STOP_DYNAMIC_SCENES_FOR_TARGETS_SCHEMA
    )

    hass.services.async_register(
        DOMAIN,
        SERVICE_STOP_ALL_DYNAMIC_SCENES,
        stop_all_dynamic_scenes,
    )

    hass.services.async_register(
        DOMAIN,
        SERVICE_APPLY_LOOK,
        apply_look,
        schema=APPLY_LOOK_SCHEMA,
        supports_response=SupportsResponse.OPTIONAL
    )

    hass.services.async_register(
        DOMAIN,
        SERVICE_STOP_LOOK,
        stop_look,
        schema=STOP_LOOK_SCHEMA
    )

    hass.services.async_register(
        DOMAIN,
        SERVICE_RESET_USERDATA,
        reset_userdata,
        schema=RESET_USERDATA_SCHEMA,
        supports_response=SupportsResponse.OPTIONAL
    )


    return True

async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry
) -> bool:
    hass.data.setdefault(DOMAIN, {})

    async def _async_stop_dynamic_scenes(_event: Event) -> None:
        await dynamic_scene_manager.async_stop_all()

    entry.async_on_unload(
        hass.bus.async_listen_once(
            EVENT_HOMEASSISTANT_STOP,
            _async_stop_dynamic_scenes,
        )
    )

    await async_setup_view(hass)

    async_setup_websocket_api(hass, dynamic_scene_manager)

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    return True

async def async_unload_entry(
    hass: HomeAssistant, entry: ConfigEntry
) -> bool:
    await dynamic_scene_manager.async_stop_all()
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unload_ok:
        # Panel beim Unload entfernen, sonst wirft das nächste Setup (Reload/HACS-
        # Update) "Overwriting panel". async_remove_entry deckt nur das Löschen ab.
        await async_remove_view(hass)
    return unload_ok

async def async_remove_entry(
    hass: HomeAssistant, entry: ConfigEntry
) -> None:

    await async_remove_view(hass)
