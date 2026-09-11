import voluptuous as vol
import asyncio
import logging
from .file_utils import find_preset
from .color_management import *
from .color_temperature import find_closest_ct_match
from .color_conversion import kelvin_to_xy

_LOGGER = logging.getLogger(__name__)


async def apply_colors(
    hass,
    preset_colors,
    light_entity_ids,
    transition,
    shuffle,
    smart_shuffle,
    brightness,
    *,
    blocking=False,
):
    """Paint a set of xy colors onto the given lights (single pass)."""
    if not preset_colors:
        return

    randomized_colors = None
    if shuffle:
        randomized_colors = get_randomized_colors(preset_colors, len(light_entity_ids))
        random.shuffle(light_entity_ids) # While this _should_ be redundant, the result somehow feels better with it in place

    tasks = []

    for index, entity_id in enumerate(light_entity_ids):
        light_params = {
            "brightness": brightness,
            "transition": transition,
            "entity_id": entity_id,
        }
        hass_state = hass.states.get(entity_id)
        if not hass_state:
            continue

        if shuffle:
            current_color = hass_state.attributes.get("xy_color", None)

            if current_color is not None and smart_shuffle:
                next_color = get_next_smart_random_color(current_color, preset_colors)
            elif randomized_colors is not None and index < len(randomized_colors):
                next_color = randomized_colors[index]
            else:
                next_color = get_random_color(preset_colors)
        else:
            next_color = get_next_color(index, preset_colors)

        supported_color_modes = hass_state.attributes.get("supported_color_modes", "")
        color_support = any(mode in supported_color_modes for mode in ["xy", "hs", "rgb", "rgbw"])
        temp_support = "color_temp" in supported_color_modes
        brightness_support = "brightness" in supported_color_modes

        if color_support:
            light_params["xy_color"] = next_color
        elif temp_support:
            light_params["color_temp_kelvin"] = find_closest_ct_match(next_color[0], next_color[1])
        elif brightness_support:
            pass # Nothing to add to the payload. Brightness is already part of it
        else:
            continue # Not turning on the light since it neither supports color nor color temperature and not even dimming

        task = hass.services.async_call(
            "light",
            "turn_on",
            light_params,
            blocking=blocking,
        )
        tasks.append(task)

    await asyncio.gather(*tasks)


async def apply_kelvin(
    hass,
    kelvin,
    light_entity_ids,
    transition,
    brightness,
    *,
    blocking=False,
):
    """Paint a single colour temperature (Kelvin) onto the given lights.

    CCT-capable lights get color_temp_kelvin directly; colour-only lights
    fall back to the equivalent xy point. This is the apply path for a
    "white"/Kelvin scene (no colour palette).
    """
    tasks = []

    for entity_id in light_entity_ids:
        hass_state = hass.states.get(entity_id)
        if not hass_state:
            continue

        light_params = {
            "brightness": brightness,
            "transition": transition,
            "entity_id": entity_id,
        }

        supported_color_modes = hass_state.attributes.get("supported_color_modes", "")
        temp_support = "color_temp" in supported_color_modes
        color_support = any(mode in supported_color_modes for mode in ["xy", "hs", "rgb", "rgbw", "rgbww"])
        brightness_support = "brightness" in supported_color_modes

        if temp_support:
            light_params["color_temp_kelvin"] = kelvin
        elif color_support:
            light_params["xy_color"] = list(kelvin_to_xy(kelvin))
        elif brightness_support:
            pass  # Dimmable only — brightness is already in the payload.
        else:
            continue  # Can't represent a colour temperature on this light.

        tasks.append(
            hass.services.async_call("light", "turn_on", light_params, blocking=blocking)
        )

    await asyncio.gather(*tasks)


def _sweep_value(values, step):
    """Pick the value for a given step, sweeping smoothly back and forth.

    A list [A, B, C] yields A, B, C, B, A, B, C, ... (triangle wave) so the
    colour temperature glides up and down the range instead of snapping from
    the last value back to the first.
    """
    if len(values) <= 1:
        return values[0]
    period = 2 * (len(values) - 1)
    pos = step % period
    return values[pos] if pos < len(values) else values[period - pos]


def _preset_kelvins(preset_data):
    """The scene's list of Kelvin values, or None if it isn't a Kelvin scene.

    Supports the new `kelvins` list and the legacy single `kelvin` field.
    """
    kelvins = preset_data.get("kelvins")
    if kelvins:
        return list(kelvins)
    single = preset_data.get("kelvin")
    return [single] if single is not None else None


async def apply_preset(
    hass,
    preset_ident,
    light_entity_ids,
    transition,
    shuffle,
    smart_shuffle,
    brightness_override=None,
    step=0,
    *,
    blocking=False,
):
    preset_data = find_preset(preset_ident)

    if not preset_data:
        raise vol.Invalid(f"Preset '{preset_ident}' not found.")

    brightness = brightness_override if brightness_override is not None else preset_data.get("bri", 255)

    kelvins = _preset_kelvins(preset_data)
    if kelvins is not None:
        # All lights share one colour temperature per step; the dynamic loop
        # advances `step` so a multi-value scene sweeps through the range.
        await apply_kelvin(
            hass,
            _sweep_value(kelvins, step),
            light_entity_ids,
            transition,
            brightness,
            blocking=blocking,
        )
        return

    preset_colors = [(light["x"], light["y"]) for light in preset_data["lights"]]

    await apply_colors(
        hass,
        preset_colors,
        light_entity_ids,
        transition,
        shuffle,
        smart_shuffle,
        brightness,
        blocking=blocking,
    )
