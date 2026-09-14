import asyncio
import importlib
import os
import sys
import types


PACKAGE_DIR = os.path.join(
    os.path.dirname(__file__),
    "..",
    "custom_components",
    "benni_scene_presets",
)

package = types.ModuleType("benni_scene_presets")
package.__path__ = [PACKAGE_DIR]
sys.modules.setdefault("benni_scene_presets", package)

presets = types.ModuleType("benni_scene_presets.presets")


async def _unused_apply_preset(*_args, **_kwargs):
    raise AssertionError("test must monkeypatch apply_preset")


presets.apply_preset = _unused_apply_preset
sys.modules.setdefault("benni_scene_presets.presets", presets)

dynamic_scenes = importlib.import_module("benni_scene_presets.dynamic_scenes")


class _State:
    state = "on"


class _States:
    def get(self, _entity_id):
        return _State()


class _Hass:
    states = _States()

    def async_create_task(self, coro):
        return asyncio.create_task(coro)


class _LegacyNoneTaskHass(_Hass):
    states = _States()
    legacy_create_task_called = False

    def create_task(self, coro):
        self.legacy_create_task_called = True
        coro.close()
        return None


def test_async_stop_all_cancels_awaits_and_clears_dynamic_scene_tasks(monkeypatch):
    calls = []

    async def fake_apply_preset(*_args, **kwargs):
        calls.append(kwargs.get("step"))

    monkeypatch.setattr(dynamic_scenes, "apply_preset", fake_apply_preset)

    async def run():
        manager = dynamic_scenes.DynamicSceneManager()
        manager.create_new(
            _Hass(),
            {
                "light_entity_ids": ["light.test"],
                dynamic_scenes.ATTR_SCENE_PRESET_ID: "preset",
            },
            interval=3600,
        )

        await asyncio.sleep(0)
        scene = next(iter(manager.dynamic_scenes.values()))
        task = scene._task

        assert calls == [0]
        assert task is not None
        assert not task.done()

        await manager.async_stop_all()

        assert manager.dynamic_scenes == {}
        assert manager.active_looks == set()
        assert scene._running is False
        assert scene._task is None
        assert task.done()

    asyncio.run(run())


def test_start_loop_ignores_legacy_create_task_returning_none(monkeypatch):
    calls = []

    async def fake_apply_preset(*_args, **kwargs):
        calls.append(kwargs.get("step"))

    monkeypatch.setattr(dynamic_scenes, "apply_preset", fake_apply_preset)

    async def run():
        hass = _LegacyNoneTaskHass()
        manager = dynamic_scenes.DynamicSceneManager()
        manager.create_new(
            hass,
            {
                "light_entity_ids": ["light.test"],
                dynamic_scenes.ATTR_SCENE_PRESET_ID: "preset",
            },
            interval=3600,
        )

        await asyncio.sleep(0)
        scene = next(iter(manager.dynamic_scenes.values()))

        assert hass.legacy_create_task_called is False
        assert calls == [0]
        assert scene._task is not None

        await manager.async_stop_all()

    asyncio.run(run())


def test_async_stop_all_for_look_drains_paint_before_following_turn_off(monkeypatch):
    paint_started = asyncio.Event()
    paint_cancelled = asyncio.Event()
    calls = []

    async def fake_apply_preset(*_args, **kwargs):
        calls.append(kwargs.get("blocking"))
        paint_started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            paint_cancelled.set()
            raise

    monkeypatch.setattr(dynamic_scenes, "apply_preset", fake_apply_preset)

    async def run():
        manager = dynamic_scenes.DynamicSceneManager()
        manager.create_new(
            _Hass(),
            {
                "look": "nightlight",
                "light_entity_ids": ["light.hallway_ceiling_light"],
                dynamic_scenes.ATTR_SCENE_PRESET_ID: "preset",
            },
            interval=3600,
        )

        await paint_started.wait()
        await manager.async_stop_all_for_look("nightlight")
        calls.append("turn_off")

        assert calls == [True, "turn_off"]
        assert paint_cancelled.is_set()
        assert manager.dynamic_scenes == {}

    asyncio.run(run())
