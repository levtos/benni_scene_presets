"""A switch per Look: on = the look is currently playing.

turn_on/off call the apply_look/stop_look services. The set of switches is kept
in sync with looks.json at runtime via the SIGNAL_LOOKS_CHANGED dispatcher.
"""
from homeassistant.components.switch import SwitchEntity
from homeassistant.core import callback
from homeassistant.helpers.dispatcher import async_dispatcher_connect

from . import dynamic_scene_manager, file_utils
from .const import DOMAIN, SIGNAL_LOOKS_CHANGED, SERVICE_APPLY_LOOK, SERVICE_STOP_LOOK, ATTR_LOOK_ID
from .util import ensure_list


async def async_setup_entry(hass, entry, async_add_entities):
    known: dict[str, "BenniLookSwitch"] = {}

    @callback
    def _sync():
        looks = {l["slug"]: l for l in file_utils.LOOKS.get("looks", []) if l.get("slug")}

        to_add = []
        for slug, look in looks.items():
            if slug not in known:
                entity = BenniLookSwitch(slug, look.get("name") or slug)
                known[slug] = entity
                to_add.append(entity)
        if to_add:
            async_add_entities(to_add)

        for slug in list(known):
            if slug not in looks:
                hass.async_create_task(known.pop(slug).async_remove())

    _sync()
    entry.async_on_unload(async_dispatcher_connect(hass, SIGNAL_LOOKS_CHANGED, _sync))


class BenniLookSwitch(SwitchEntity):
    _attr_should_poll = True  # state reflects live scene activity (incl. self-destruct)
    _attr_icon = "mdi:palette"
    _attr_has_entity_name = False

    def __init__(self, slug, name):
        self._slug = slug
        self._attr_name = f"Look: {name}"
        self._attr_unique_id = f"{DOMAIN}_look_{slug}"
        # entity_ids must use underscores; slugs may contain hyphens (HA warns
        # and will reject hyphenated entity_ids from 2027.2).
        self.entity_id = f"switch.benni_look_{slug.replace('-', '_')}"

    @property
    def is_on(self):
        return dynamic_scene_manager.is_look_active(self._slug)

    async def async_turn_on(self, **kwargs):
        await self.hass.services.async_call(
            DOMAIN, SERVICE_APPLY_LOOK, {ATTR_LOOK_ID: self._slug}, blocking=True
        )
        self.async_write_ha_state()

    async def async_turn_off(self, **kwargs):
        await self.hass.services.async_call(
            DOMAIN, SERVICE_STOP_LOOK, {ATTR_LOOK_ID: self._slug}, blocking=True
        )
        look = file_utils.get_look(self._slug)
        aqara_targets = []
        for binding in (look or {}).get("bindings", []):
            if binding.get("kind") == "aqara":
                aqara_targets += ensure_list(
                    (binding.get("targets") or {}).get("entity_id")
                )
        if aqara_targets:
            await self.hass.services.async_call(
                "light",
                "turn_off",
                {"entity_id": list(dict.fromkeys(aqara_targets))},
                blocking=True,
            )
        self.async_write_ha_state()
