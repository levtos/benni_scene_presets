import uuid
import asyncio
import logging
from contextlib import suppress
from .presets import apply_preset
from .const import *

_LOGGER = logging.getLogger(__name__)


class DynamicScene:
    def __init__(self, hass, self_destruct_callback, parameters, interval):
        self.id = str(uuid.uuid4())
        self.hass = hass
        self.interval = interval
        self._running = False
        self._task = None
        self.parameters = parameters
        self.self_destruct_callback = self_destruct_callback

        self.start_loop()

    async def _loop(self):
        run_count = 0

        try:
            while self._running:
                light_entity_ids = self.parameters.get("light_entity_ids")
                transition = self.parameters.get(ATTR_TRANSITION)
                smart_shuffle = True

                if run_count == 0:
                    # Upstream hard-codes transition = 0.5 here, which snaps the
                    # lights on the first paint and breaks crossfades from a
                    # previous scene. Honour an explicit initial_transition if
                    # given, otherwise keep the requested transition for a smooth
                    # first paint.
                    initial_transition = self.parameters.get(ATTR_INITIAL_TRANSITION)
                    if initial_transition is not None:
                        transition = initial_transition
                    # smart_shuffle stays off on the first run so the initial colour
                    # assignment is deterministic (intentional upstream behaviour).
                    smart_shuffle = False
                else:
                    entity_states = [
                        self.hass.states.get(x)
                        for x in light_entity_ids
                        if x is not None
                    ]
                    lights_on = len([x for x in entity_states if x.state == "on"])

                    if lights_on == 0:
                        self._running = False
                        self.self_destruct_callback(self.id)
                        return
                    else:
                        # The user probably purposefully turned off parts of the lights, so if one is off, we ignore it
                        light_entity_ids = [
                            entity_id for entity_id, state in zip(light_entity_ids, entity_states)
                            if state and state.state == "on"
                        ]


                await apply_preset(
                    self.hass,
                    self.parameters.get(ATTR_SCENE_PRESET_ID),
                    light_entity_ids,
                    transition,
                    self.parameters.get(ATTR_SHUFFLE),
                    smart_shuffle,
                    self.parameters.get(ATTR_BRIGHTNESS, None),
                    step=run_count,  # advances a Kelvin scene's sweep through its values
                    # Dynamic scene paints must finish before stop_look can drain
                    # this task; one-shot applies retain non-blocking dispatch.
                    blocking=True,
                )
                run_count += 1

                await asyncio.sleep(self.interval)
        except asyncio.CancelledError:
            self._running = False
            raise

    def start_loop(self):
        if self._running:
            return
        self._running = True
        self._task = self.hass.create_task(self._loop())
        if hasattr(self._task, "add_done_callback"):
            self._task.add_done_callback(self._task_done)

    def stop_loop(self):
        self._running = False
        if self._task:
            self._task.cancel()

    async def async_stop_loop(self):
        task = self._task
        self.stop_loop()
        if task:
            with suppress(asyncio.CancelledError):
                await task
            if self._task is task:
                self._task = None

    def _task_done(self, task):
        if self._task is task:
            self._task = None
        self._running = False
        try:
            task.result()
        except asyncio.CancelledError:
            pass
        except Exception:
            _LOGGER.exception("DynamicScene task failed")

    def to_dict(self):
        return {
            "id": self.id,
            "interval": self.interval,
            "parameters": self.parameters,
            "running": self._running,
        }

    def __del__(self):
        self.stop_loop()


class DynamicSceneManager:
    def __init__(self):
        self.dynamic_scenes = {}
        # Looks that were applied and not yet stopped. Needed because
        # effect-only (Aqara) looks start no dynamic scene, so the look switch
        # would otherwise always read "off".
        self.active_looks = set()

    def mark_look_active(self, look_slug):
        if look_slug:
            self.active_looks.add(look_slug)

    def mark_look_inactive(self, look_slug):
        self.active_looks.discard(look_slug)

    def create_new(self, hass, parameters, interval):
        scene = DynamicScene(
            hass,
            lambda scene_id: self.delete_by_id(scene_id),
            parameters,
            interval
        )
        self.dynamic_scenes[scene.id] = scene
        return scene.to_dict()

    def get_by_id(self, id):
        return self.dynamic_scenes.get(id)

    def delete_by_id(self, id):
        active_scene = self.dynamic_scenes.get(id)

        if active_scene:
            active_scene.stop_loop()
            del self.dynamic_scenes[id]

    def stop_all(self):
        self.active_looks.clear()
        scenes_to_delete = []

        for scene in self.dynamic_scenes.values():
            scene.stop_loop()
            scenes_to_delete.append(scene.id)

        for scene_id in scenes_to_delete:
            del self.dynamic_scenes[scene_id]

    def stop_all_for_entity_id(self, entity_id):
        scenes_to_delete = []

        for scene in self.dynamic_scenes.values():
            entity_ids = scene.parameters.get("light_entity_ids", [])
            if entity_id in entity_ids:
                scene.stop_loop()
                scenes_to_delete.append(scene.id)

        for scene_id in scenes_to_delete:
            del self.dynamic_scenes[scene_id]

    async def async_stop_all(self):
        self.active_looks.clear()
        scenes = list(self.dynamic_scenes.values())
        self.dynamic_scenes.clear()
        await asyncio.gather(
            *(scene.async_stop_loop() for scene in scenes),
            return_exceptions=True,
        )

    def stop_all_for_look(self, look_slug):
        if not look_slug:
            return

        self.mark_look_inactive(look_slug)
        scenes_to_delete = []

        for scene in self.dynamic_scenes.values():
            if scene.parameters.get("look") == look_slug:
                scene.stop_loop()
                scenes_to_delete.append(scene.id)

        for scene_id in scenes_to_delete:
            del self.dynamic_scenes[scene_id]

    async def async_stop_all_for_look(self, look_slug):
        if not look_slug:
            return

        self.mark_look_inactive(look_slug)
        scenes = [
            scene
            for scene in self.dynamic_scenes.values()
            if scene.parameters.get("look") == look_slug
        ]
        for scene in scenes:
            self.dynamic_scenes.pop(scene.id, None)

        await asyncio.gather(
            *(scene.async_stop_loop() for scene in scenes),
            return_exceptions=True,
        )

    def is_look_active(self, look_slug):
        """True if the look was applied (and not stopped), or any of its scenes run."""
        if look_slug in self.active_looks:
            return True
        return any(
            scene._running and scene.parameters.get("look") == look_slug
            for scene in self.dynamic_scenes.values()
        )

    def get_all(self):
        return list(self.dynamic_scenes.values())

    def get_all_as_dict(self):
        scenes_dict = {"dynamic_scenes": []}

        for scene in self.dynamic_scenes.values():
            scenes_dict["dynamic_scenes"].append(scene.to_dict())

        return scenes_dict
