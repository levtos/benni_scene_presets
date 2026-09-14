import asyncio
import importlib.util
import os
import sys
import types


PACKAGE_DIR = os.path.join(
    os.path.dirname(__file__),
    "..",
    "custom_components",
    "benni_scene_presets",
)


def _module(name, **attributes):
    module = types.ModuleType(name)
    for key, value in attributes.items():
        setattr(module, key, value)
    sys.modules[name] = module
    return module


def _load_integration(monkeypatch):
    package_name = "benni_scene_presets_stop_test"
    for name in list(sys.modules):
        if name == package_name or name.startswith(f"{package_name}."):
            monkeypatch.delitem(sys.modules, name, raising=False)

    class _Schema:
        def __init__(self, value):
            self.value = value

    vol = _module("voluptuous")
    vol.Schema = _Schema
    vol.Required = lambda value: value
    vol.Optional = lambda value, default=None: value
    vol.Coerce = lambda value: value
    vol.Any = lambda *values: values

    homeassistant = _module("homeassistant")
    helpers = _module("homeassistant.helpers")
    config_validation = _module(
        "homeassistant.helpers.config_validation",
        empty_config_schema=lambda _domain: _Schema({}),
        string=str,
        boolean=bool,
    )
    helpers.config_validation = config_validation
    homeassistant.helpers = helpers
    _module("homeassistant.const", EVENT_HOMEASSISTANT_STOP="homeassistant_stop")

    class _SupportsResponse:
        ONLY = "only"
        OPTIONAL = "optional"

    _module(
        "homeassistant.core",
        Event=object,
        HomeAssistant=object,
        SupportsResponse=_SupportsResponse,
    )
    _module("homeassistant.config_entries", ConfigEntry=object)
    _module(
        "homeassistant.helpers.dispatcher",
        async_dispatcher_send=lambda *_args, **_kwargs: None,
    )

    package = types.ModuleType(package_name)
    package.__path__ = [PACKAGE_DIR]
    sys.modules[package_name] = package

    const_spec = importlib.util.spec_from_file_location(
        f"{package_name}.const", os.path.join(PACKAGE_DIR, "const.py")
    )
    const = importlib.util.module_from_spec(const_spec)
    sys.modules[const_spec.name] = const
    const_spec.loader.exec_module(const)

    class _Manager:
        def __init__(self):
            self.calls = []

        async def async_stop_all_for_look(self, look):
            self.calls.append(("loops_stopped", look))

        def stop_all(self):
            pass

    _module(
        f"{package_name}.dynamic_scenes",
        DynamicScene=object,
        DynamicSceneManager=_Manager,
    )
    _module(f"{package_name}.presets", apply_preset=lambda *_args, **_kwargs: None)
    _module(
        f"{package_name}.view",
        async_setup_view=lambda *_args, **_kwargs: None,
        async_remove_view=lambda *_args, **_kwargs: None,
    )
    _module(
        f"{package_name}.util",
        ensure_list=lambda value: value if isinstance(value, list) else [value] if isinstance(value, str) else [],
        resolve_targets=lambda *_args, **_kwargs: [],
    )
    _module(
        f"{package_name}.websocket_api",
        async_setup_websocket_api=lambda *_args, **_kwargs: None,
    )
    file_utils = _module(f"{package_name}.file_utils")

    spec = importlib.util.spec_from_file_location(
        package_name,
        os.path.join(PACKAGE_DIR, "__init__.py"),
        submodule_search_locations=[PACKAGE_DIR],
    )
    integration = importlib.util.module_from_spec(spec)
    sys.modules[package_name] = integration
    spec.loader.exec_module(integration)
    integration.file_utils = file_utils
    return integration


class _Call:
    def __init__(self, **data):
        self.data = data


class _Services:
    def __init__(self, calls=None):
        self.handlers = {}
        self.calls = calls if calls is not None else []
        self.stop_started = asyncio.Event()
        self.release_stop = asyncio.Event()

    def async_register(self, domain, service, handler, **_kwargs):
        self.handlers[(domain, service)] = handler

    async def async_call(self, domain, service, data, blocking=False):
        if domain == "aqara_advanced_lighting" and service == "stop_effect":
            self.calls.append(("stop_started", data, blocking))
            self.stop_started.set()
            await self.release_stop.wait()
            self.calls.append(("stop_finished", data, blocking))
            return
        self.calls.append((f"{domain}.{service}", data, blocking))


class _Hass:
    def __init__(self, calls=None):
        self.services = _Services(calls)


def test_stop_look_awaits_aqara_stop_before_unconditional_off(monkeypatch):
    integration = _load_integration(monkeypatch)
    integration.file_utils.get_look = lambda _slug: {
        "slug": "overwatch",
        "bindings": [
            {
                "kind": "aqara",
                "aqara": "ring-effect",
                "targets": {"entity_id": ["light.living_ceiling_light_rgb"]},
            }
        ],
    }
    integration.file_utils.get_aqara = lambda _slug: {
        "service": "set_dynamic_effect"
    }
    hass = _Hass(integration.dynamic_scene_manager.calls)

    async def run():
        await integration.async_setup(hass, {})
        handler = hass.services.handlers[(integration.DOMAIN, integration.SERVICE_STOP_LOOK)]
        task = asyncio.create_task(handler(_Call(look="overwatch")))

        await hass.services.stop_started.wait()
        assert [call[0] for call in hass.services.calls] == [
            "loops_stopped",
            "stop_started",
        ]

        hass.services.release_stop.set()
        await task

    asyncio.run(run())

    assert hass.services.calls == [
        ("loops_stopped", "overwatch"),
        (
            "stop_started",
            {
                "entity_id": ["light.living_ceiling_light_rgb"],
                "restore_state": False,
            },
            True,
        ),
        (
            "stop_finished",
            {
                "entity_id": ["light.living_ceiling_light_rgb"],
                "restore_state": False,
            },
            True,
        ),
        (
            "light.turn_off",
            {"entity_id": ["light.living_ceiling_light_rgb"]},
            True,
        ),
    ]


def test_switch_off_sends_aqara_off_even_when_state_is_off(monkeypatch):
    package_name = "benni_scene_presets_switch_test"
    package = types.ModuleType(package_name)
    package.__path__ = [PACKAGE_DIR]
    sys.modules[package_name] = package

    class _SwitchEntity:
        def async_write_ha_state(self):
            pass

    components = _module("homeassistant.components")
    switch_component = _module(
        "homeassistant.components.switch", SwitchEntity=_SwitchEntity
    )
    components.switch = switch_component
    _module("homeassistant.core", callback=lambda func: func)
    _module(
        "homeassistant.helpers.dispatcher",
        async_dispatcher_connect=lambda *_args, **_kwargs: None,
    )

    manager = types.SimpleNamespace(is_look_active=lambda _slug: False)
    file_utils = _module(
        f"{package_name}.file_utils",
        get_look=lambda _slug: {
            "bindings": [
                {
                    "kind": "aqara",
                    "targets": {
                        "entity_id": ["light.living_ceiling_light_rgb"]
                    },
                }
            ]
        },
    )
    package.dynamic_scene_manager = manager
    package.file_utils = file_utils
    sys.modules[f"{package_name}.dynamic_scene_manager"] = manager

    const_spec = importlib.util.spec_from_file_location(
        f"{package_name}.const", os.path.join(PACKAGE_DIR, "const.py")
    )
    const = importlib.util.module_from_spec(const_spec)
    sys.modules[const_spec.name] = const
    const_spec.loader.exec_module(const)
    _module(
        f"{package_name}.util",
        ensure_list=lambda value: value if isinstance(value, list) else [value] if isinstance(value, str) else [],
    )

    spec = importlib.util.spec_from_file_location(
        f"{package_name}.switch", os.path.join(PACKAGE_DIR, "switch.py")
    )
    switch = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = switch
    spec.loader.exec_module(switch)

    calls = []

    class _SwitchServices:
        async def async_call(self, domain, service, data, blocking=False):
            calls.append((domain, service, data, blocking))

    hass = types.SimpleNamespace(
        services=_SwitchServices(),
        states=types.SimpleNamespace(
            get=lambda _entity_id: types.SimpleNamespace(state="off")
        ),
    )
    entity = switch.BenniLookSwitch("overwatch", "Overwatch")
    entity.hass = hass

    asyncio.run(entity.async_turn_off())

    assert calls == [
        (
            const.DOMAIN,
            const.SERVICE_STOP_LOOK,
            {const.ATTR_LOOK_ID: "overwatch"},
            True,
        ),
        (
            "light",
            "turn_off",
            {"entity_id": ["light.living_ceiling_light_rgb"]},
            True,
        ),
    ]
