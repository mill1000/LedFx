import json
import logging
import socket
from dataclasses import dataclass
from typing import Any, Optional

import paho.mqtt.client as mqtt
import voluptuous as vol

from ledfx.color import parse_color
from ledfx.config import save_config
from ledfx.consts import PROJECT_VERSION
from ledfx.effects.audio import AudioInputSource
from ledfx.events import Event
from ledfx.integrations import Integration

_LOGGER = logging.getLogger(__name__)


@dataclass
class EntityConfig:
    name: str
    unique_id: str
    icon: str
    entity_category: Optional[str] = None


command_template = """{
    "state": "on"
    {%- if red is defined and green is defined and blue is defined -%}
    , "color": [{{ red }}, {{ green }}, {{ blue }}]
    {%- endif -%}
    {%- if effect is defined -%}
    , "effect": "{{ effect }}"
    {%- endif -%}
}
"""


def extract_ip():
    st = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        st.connect(("10.255.255.255", 1))
        IP = st.getsockname()[0]
    except Exception:
        IP = "127.0.0.1"
    finally:
        st.close()
    return IP


class MQTT_HASS(Integration):
    """MQTT HomeAssistant Integration"""

    NAME = "Home Assistant MQTT"
    DESCRIPTION = "MQTT Integration for Home Assistant"

    CONFIG_SCHEMA = vol.Schema(
        {
            vol.Required(
                "name",
                description="Name of this HomeAssistant instance",
                default="Home Assistant",
            ): str,
            vol.Required(
                "state_topic",
                description="Topic prefix to publish LedFx state to",
                default="ledfx",
            ): str,
            vol.Required(
                "discovery_topic",
                description="HomeAssistant's MQTT discovery prefix",
                default="homeassistant",
            ): str,
            vol.Required(
                "ip_address",
                description="MQTT IP address",
                default="127.0.0.1",
            ): str,
            vol.Required(
                "port", description="MQTT port", default=1883
            ): vol.All(vol.Coerce(int), vol.Range(min=1, max=65535)),
            vol.Optional(
                "username",
                description="MQTT username",
                default="",
            ): str,
            vol.Optional(
                "password",
                description="MQTT password",
                default="",
            ): str,
            vol.Optional(
                "description",
                description="Internal Description",
                default="MQTT Integration with auto-discovery",
            ): str,
        }
    )

    TRANSITION_MAPPING = {
        "ledfxtransitiontype": "transition_mode",
        "ledfxtransitiontime": "transition_time",
    }

    def __init__(self, ledfx, config, active, data):
        super().__init__(ledfx, config, active, data)

        self._ledfx = ledfx
        self._config = config
        self._client = None
        self._data = []
        self._listeners = []

        self._host = f"{extract_ip()}:{ledfx.port}"

        self._state_prefix = self._config['state_topic']
        self._discovery_prefix = self._config['discovery_topic']

    def _discovery_topic(self, platform: str) -> str:
        return f"{self._discovery_prefix}/{platform}/ledfx"

    def _publish_virtual_config(self, virtual_id):
        virtual = self._ledfx.virtuals.get(virtual_id)
        self._client.publish(
            f"{self._discovery_topic("light")}/{virtual_id}/meta",
            json.dumps(virtual.config),
        )

    def _publish_virtual_paused(self, virtual_id):
        virtual = self._ledfx.virtuals.get(virtual_id)
        paused_state = "OFF"
        if virtual.active:
            paused_state = "ON"
        self._client.publish(
            f"{self._discovery_topic("light")}/{virtual_id}/state",
            json.dumps({"state": paused_state}),
        )

    def _publish_audio_input_changed(self, event):
        self._client.publish(
            f"{self._discovery_topic("select")}/ledfxaudio/state",
            event.audio_input_device_name,
        )

    def _publish_scene_actived(self, event):
        self._client.publish(
            f"{self._discovery_topic("select")}/ledfxsceneselect/state",
            event.scene_id,
        )

    def _publish_global_paused_state(self, event):
        paused_state = "OFF"
        if self._ledfx.virtuals._paused:
            paused_state = "OFF"
        else:
            paused_state = "ON"
        self._client.publish(
            f"{self._discovery_topic("switch")}/ledfxplay/state",
            paused_state,
        )

    def _publish_single_color_updated(self, event):
        color = parse_color(event.effect_config.get("color"))
        self._client.publish(
            f"{self._discovery_topic("light")}/{event.virtual_id}/state",
            json.dumps(
                {
                    "state": "on",
                    "color": [color.red, color.green, color.blue],
                    "effect": event.effect_config.get("color"),
                }
            ),
        )

    def _publish_paused_state(self, event):
        virtual = self._ledfx.virtuals.get(event.virtual_id)
        paused_state = "ON" if virtual.active else "OFF"
        self._client.publish(
            f"{self._state_prefix}/{event.virtual_id}/state",
            json.dumps({"state": paused_state}),
        )

    @property
    def _hass_device(self) -> dict[str, Any]:
        """Get the HomeAssistant device for discovery payloads."""
        return {
            "identifiers": [self._host],
            "configuration_url": f"http://{self._host}/#/Integrations",  # TODO not docker friendly
            "name": "LedFx",
            "manufacturer": "LedFx",
            "sw_version": f"{PROJECT_VERSION}",
        }

    def _publish_sensor_config(self, sensor: str, config: EntityConfig) -> None:
        """Publish a sensor component discovery config."""
        discovery_config = {
            "name": config.name,
            "unique_id": config.unique_id,
            "icon": config.icon,
            "device": self._hass_device,
            "~": f"{self._state_prefix}/{sensor}",
            "stat_t": "~/state",

        }

        if category := config.entity_category:
            discovery_config["entity_category"] = category

        self._client.publish(
            f"{self._discovery_topic("sensor")}/{sensor}/config",
            json.dumps(discovery_config),
        )

    def _publish_select_config(self, select: str, options: list[str], config: EntityConfig) -> None:
        """Publish a select component discovery config."""
        discovery_config = {
            "name": config.name,
            "unique_id": config.unique_id,
            "icon": config.icon,
            "device": self._hass_device,
            "~": f"{self._state_prefix}/{select}",
            "cmd_t": "~/set",
            "stat_t": "~/state",
            "options": options,

        }

        if category := config.entity_category:
            discovery_config["entity_category"] = category

        self._client.publish(
            f"{self._discovery_topic("select")}/{select}/config",
            json.dumps(discovery_config),
        )

    def _publish_switch_config(self, switch: str, config: EntityConfig) -> None:
        """Publish a switch component discovery config."""
        discovery_config = {
            "name": config.name,
            "unique_id": config.unique_id,
            "icon": config.icon,
            "device": self._hass_device,
            "~": f"{self._state_prefix}/{switch}",
            "cmd_t": "~/set",
            "stat_t": "~/state",
        }

        if category := config.entity_category:
            discovery_config["entity_category"] = category

        self._client.publish(
            f"{self._discovery_topic("switch")}/{switch}/config",
            json.dumps(discovery_config),
        )

    def _publish_light_config(self, light: str, effects: list[str], config: EntityConfig) -> None:
        """Publish a light component discovery config."""
        discovery_config = {
            "name": config.name,
            "unique_id": config.unique_id,
            "icon": config.icon,
            "device": self._hass_device,
            "~": f"{self._state_prefix}/{light}",
            "cmd_t": "~/set",
            "stat_t": "~/state",
            "stat_tpl": "{{ value_json.state | lower }}",
            "stat_val_tpl": "{{ value_json.state | lower }}",
            "schema": "template",
            "brightness": False,
            "enabled_by_default": True,
            "command_on_template": command_template,
            "command_off_template": '{"state": "off"}',
            "red_template": "{{ value_json.color[0] }}",
            "green_template": "{{ value_json.color[1] }}",
            "blue_template": "{{ value_json.color[2] }}",
            "effect_template": "{{ value_json.effect }}",
            "json_attributes_topic": "~/meta",
            "effect": True if len(effects) > 0 else False,
            "effect_list": effects,
        }

        if category := config.entity_category:
            discovery_config["entity_category"] = category

        self._client.publish(
            f"{self._discovery_topic("light")}/{light}/config",
            json.dumps(discovery_config),
        )

    def _publish_config(self) -> None:
        """Publish all discovery configs."""
        # Pixle count sensor
        pixel_count = EntityConfig(
            name="Pixel Count",
            unique_id="ledfxpixelsensor",  # TODO
            icon="mdi:led-variant-outline",
            entity_category="diagnostic"
        )
        self._publish_sensor_config("pixel_count_sensor", pixel_count)

        # Scene selector
        scene_select = EntityConfig(
            name="Scene",
            unique_id="ledfxsceneselect",  # TODO
            icon="mdi:image-multiple-outline",
        )
        self._publish_select_config(
            "scene",
            list(self._ledfx.scenes._scenes.keys()),
            scene_select,
        )

       # Audio selector
        audio_select = EntityConfig(
            name="Audio Source",
            unique_id="ledfxaudio",  # TODO
            icon="mdi:volume-high",
        )
        self._publish_select_config(
            "audio_source",
            [*AudioInputSource.input_devices().values()],
            audio_select,
        )

        # Main play / pause switch
        main_switch = EntityConfig(
            name="Play / Pause",  # TODO name
            unique_id="ledfxplay",  # TODO
            icon="mdi:play-pause",
        )
        self._publish_switch_config("pause", main_switch)

        # Create light for each virtual segment
        for virtual in self._ledfx.virtuals.values():
            name = virtual.config["name"]
            if (
                name.startswith("gap-")
                or name.endswith("-background")
                or name.endswith("-mask")
                or name.endswith("-foreground")
            ):
                continue

            entity_config = EntityConfig(
                name=name,
                unique_id=virtual.id,
                icon="mdi:led-strip"
            )

            if virtual.config["icon_name"].startswith("mdi:"):
                entity_config.icon = virtual.config["icon_name"]

            self._publish_light_config(
                virtual.id,
                list(self._ledfx.effects.classes().keys()),
                entity_config
            )

        # # TRANSITION TYPE
        # self._client.publish(
        #     f"{self._discovery_topic("select")}/ledfxtransitiontype/config",
        #     json.dumps(
        #         {
        #             "~": f"{self._discovery_topic("select")}/ledfxtransitiontype",
        #             "name": "Transition Type",
        #             "unique_id": "ledfxtransitiontype",
        #             "cmd_t": "~/set",
        #             "stat_t": "~/state",
        #             "icon": "mdi:transfer-right",
        #             "entity_category": "config",
        #             "options": list(
        #                 [
        #                     "Add",
        #                     "Dissolve",
        #                     "Push",
        #                     "Slide",
        #                     "Iris",
        #                     "Through White",
        #                     "Through Black",
        #                     "None",
        #                 ]
        #             ),
        #             "device": hass_device,
        #         }
        #     ),
        # )

        # # TRANSITION TIME
        # self._client.publish(
        #     f"{self.discovery_topic}/number/ledfxtransitiontime/config",
        #     json.dumps(
        #         {
        #             "~": f"{self.discovery_topic}/number/ledfxtransitiontime",
        #             "name": "Transition_Time",
        #             "unique_id": "ledfxtransitiontime",
        #             "cmd_t": "~/set",
        #             "stat_t": "~/state",
        #             "icon": "mdi:camera-timer",
        #             "min": 0,
        #             "max": 5,
        #             "step": 0.1,
        #             "unit_of_measurement": "s",
        #             "entity_category": "config",
        #             "device": hass_device,
        #         }
        #     ),
        # )

    def _on_mqtt_connect(self, client, userdata, flags, rc) -> None:
        """MQTT callback when we connect to the broker."""
        # Save client now that we're online
        self._client = client

        total_pixels = 0
        for device in self._ledfx.devices.values():
            total_pixels += device.pixel_count

        active_pixels = 0
        for virtual in self._ledfx.virtuals.values():
            if virtual.active:
                active_pixels += virtual.pixel_count

        _LOGGER.debug(
            "active_pixels/total_pixels:"
            + str(active_pixels)
            + "/"
            + str(total_pixels)
        )
        # ToDo create sensor with total_pixels

        # Internal State-Handler
        client.subscribe(f"{self._state_prefix}/state")

        self._listeners.append(
            self._ledfx.events.add_listener(
                self._publish_scene_actived,
                Event.SCENE_ACTIVATED,
            )
        )

        self._listeners.append(
            self._ledfx.events.add_listener(
                self._publish_single_color_updated,
                Event.EFFECT_SET,
                event_filter={"effect_name": "Single Color"},
            )
        )

        self._listeners.append(
            self._ledfx.events.add_listener(
                self._publish_virtual_config,
                Event.VIRTUAL_CONFIG_UPDATE,
            )
        )

        self._listeners.append(
            self._ledfx.events.add_listener(
                self._publish_global_paused_state, Event.GLOBAL_PAUSE
            )
        )

        self._listeners.append(
            self._ledfx.events.add_listener(
                self._publish_paused_state, Event.VIRTUAL_PAUSE
            )
        )

        self._listeners.append(
            self._ledfx.events.add_listener(
                self._publish_audio_input_changed,
                Event.AUDIO_INPUT_DEVICE_CHANGED,
            )
        )

        self._publish_config()

        client.publish(f"{self._state_prefix}/state", "HomeAssistant initialized")

    def _on_mqtt_message(self, client, userdata, msg) -> None:
        """MQTT callback when messages are received."""
        _LOGGER.error(
            "MQTT-Message incoming: \n[MQTT    ] Topic: "
            + msg.topic
            + "\n[MQTT    ] Payload: "
            + str(msg.payload)
        )
        return
        segs = msg.topic.split("/")
        try:
            payload = json.loads(msg.payload)
        except json.decoder.JSONDecodeError:
            payload = msg.payload.decode("utf-8")

        paused_state = "OFF"
        if self._ledfx.virtuals._paused:
            paused_state = "OFF"
        else:
            paused_state = "ON"

        # React to Internal State-Handler
        total_pixels = 0
        for device in self._ledfx.devices.values():
            total_pixels += device.pixel_count

        active_pixels = 0
        for virtual in self._ledfx.virtuals.values():
            if virtual.active:
                active_pixels += virtual.pixel_count

        if segs[0] == "ledfx":
            if payload == "HomeAssistant initialized":
                virtual = self._ledfx.virtuals.get(
                    next(iter(self._ledfx.virtuals))
                )
                client.publish(
                    f"{self._discovery_topic("select")}/ledfxtransitiontype/state",
                    virtual.config["transition_mode"],
                )
                client.publish(
                    f"{self.discovery_topic}/number/ledfxtransitiontime/state",
                    virtual.config["transition_time"],
                )
                # PausedState
                client.publish(
                    f"{self._discovery_topic("switch")}/ledfxplay/state",
                    paused_state,
                )
                # AudioSelector
                client.publish(
                    f"{self._discovery_topic("select")}/ledfxaudio/state",
                    AudioInputSource.input_devices()[
                        self._ledfx.config.get("audio", {}).get(
                            "audio_device", {}
                        )
                    ],
                )
                # Pixel-Sensor
                client.publish(
                    f"{self.discovery_topic}/sensor/ledfxpixelsensor/state",
                    str(active_pixels) + " / " + str(total_pixels),
                )
                # publish all virtual data on connect (meta)
                for virtual in self._ledfx.virtuals.values():
                    self._publish_virtual_config(virtual.id, client)
                    self._publish_virtual_paused(virtual.id, client)
            return

        # React to SET commands
        if segs[3] != "set":
            return
        virtualid = segs[2]

        # React to Global-PlayPause
        if virtualid == "ledfxplay":
            # _LOGGER.info("Paused: " + str(self._ledfx.virtuals._paused) + str(payload))
            self._ledfx.virtuals.pause_all()
            paused_state = "OFF"
            if self._ledfx.virtuals._paused:
                paused_state = "OFF"
            else:
                paused_state = "ON"
            client.publish(
                f"{self._discovery_topic("switch")}/{virtualid}/state",
                paused_state,
            )
            return

        # React to Transition-Type
        if virtualid in self.TRANSITION_MAPPING.keys():
            # _LOGGER.info("Transitions: " + str(payload))
            prior_state = self._ledfx.config["global_transitions"]
            self._ledfx.config["global_transitions"] = True
            virtual = self._ledfx.virtuals.get(
                next(iter(self._ledfx.virtuals))
            )
            key = self.TRANSITION_MAPPING[virtualid]
            if key == "transition_time":
                try:
                    val = float(payload)
                except ValueError as e:
                    _LOGGER.warning(e)
                    val = 0.5
            else:
                val = payload

            virtual.update_config({key: val})
            self._ledfx.config["global_transitions"] = prior_state

        # React to Scene-Selector
        elif virtualid == "ledfxsceneselect":
            self._ledfx.scenes.activate(str(payload))

        # React to Audio-Selector
        elif virtualid == "ledfxaudio":
            _LOGGER.debug("AUDIO DEVICE BROOOO: " + str(payload))
            if hasattr(self._ledfx, "audio") and self._ledfx.audio is not None:
                # index = self._ledfx.audio.get_device_index_by_name(payload)
                index = -1
                for key, value in AudioInputSource.input_devices().items():
                    if str(payload) == value:
                        index = key

                new_config = self._ledfx.config.get("audio", {})
                new_config["audio_device"] = int(index)
                self._ledfx.config["audio"] = new_config
                save_config(
                    config=self._ledfx.config,
                    config_dir=self._ledfx.config_dir,
                )
                self._ledfx.audio.update_config(new_config)
            return

        # React to Virtuals
        elif isinstance(payload, dict):
            virtual = self._ledfx.virtuals.get(virtualid, None)
            if virtual:
                # SET VIRTUAL COLOR AND ACTIVE
                color = payload.get("effect", "orange")
                color = payload.get("color", None)

                if color is not None:
                    effect = self._ledfx.effects.create(
                        ledfx=self._ledfx,
                        type="singleColor",
                        config={"color": color},
                    )
                    try:
                        virtual.set_effect(effect)
                        virtual.active = payload.get("state", "off") == "on"

                    except (ValueError, RuntimeError) as msg:
                        _LOGGER.warning(msg)
                else:
                    _LOGGER.debug("COLOR: %s", color)
                    # effect = self._ledfx.effects.create(
                    #     ledfx=self._ledfx,
                    #     type="singleColor",
                    #     config={"color": "orange"},
                    # )

                # Handle effect selection
                selected_effect_or_preset = payload.get("effect")
                if selected_effect_or_preset:
                    if selected_effect_or_preset == "back":
                        effect_list = list(
                            self._ledfx.effects.classes().keys()
                        )
                    elif (
                        selected_effect_or_preset
                        in self._ledfx.effects.classes().keys()
                    ):
                        # If an effect is selected, show its presets
                        ledfx_presets = self._ledfx.config.get(
                            "ledfx_presets", {}
                        ).get(selected_effect_or_preset, {})
                        user_presets = self._ledfx.config.get(
                            "user_presets", {}
                        ).get(selected_effect_or_preset, {})
                        effect_list = (
                            ["back"]
                            + list(ledfx_presets.keys())
                            + list(user_presets.keys())
                        )
                        effect = self._ledfx.effects.create(
                            ledfx=self._ledfx,
                            type=selected_effect_or_preset,
                            config=payload.get("effect_config", {}),
                        )
                        virtual.set_effect(effect)
                    else:
                        # If a preset is selected, apply it
                        ledfx_presets = self._ledfx.config.get(
                            "ledfx_presets", {}
                        ).get(getattr(virtual.active_effect, "type", ""), {})
                        user_presets = self._ledfx.config.get(
                            "user_presets", {}
                        ).get(getattr(virtual.active_effect, "type", ""), {})
                        preset_config = ledfx_presets.get(
                            selected_effect_or_preset
                        ) or user_presets.get(selected_effect_or_preset)
                        effect_list = (
                            ["back"]
                            + list(ledfx_presets.keys())
                            + list(user_presets.keys())
                        )
                        if preset_config:
                            effect = self._ledfx.effects.create(
                                ledfx=self._ledfx,
                                type=virtual.active_effect.type,
                                config=preset_config["config"],
                            )
                            virtual.set_effect(effect)
                        return
                    name = virtual.config["name"]
                    if (
                        name.startswith("gap-")
                        or name.endswith("-background")
                        or name.endswith("-mask")
                        or name.endswith("-foreground")
                    ):
                        return

                    if virtual.config["icon_name"].startswith("mdi:"):
                        icon = virtual.config["icon_name"]
                    else:
                        icon = "mdi:led-strip"
                    hass_device = {
                        "identifiers": ["yzlights"],
                        "configuration_url": f"http://{extract_ip()}:{self._ledfx.port}/#/Integrations",
                        "name": "LedFx",
                        "model": "BladeMOD",
                        "manufacturer": "Yeon",
                        "sw_version": f"{PROJECT_VERSION}",
                    }
                    client.publish(
                        f"{self._discovery_topic("light")}/{virtual.id}/config",
                        json.dumps(
                            {
                                "~": f"{self._discovery_topic("light")}/{virtual.id}",
                                "name": "⮑ " + name,
                                "unique_id": virtual.id,
                                "cmd_t": "~/set",
                                "stat_t": "~/state",
                                "state_template": "{{ value_json.state | lower }}",
                                "state_value_template": "{{ value_json.state | lower }}",
                                "schema": "template",
                                "brightness": False,
                                "enabled_by_default": True,
                                "command_on_template": command_template,
                                "command_off_template": '{"state": "off"}',
                                "red_template": "{{ value_json.color[0] }}",
                                "green_template": "{{ value_json.color[1] }}",
                                "blue_template": "{{ value_json.color[2] }}",
                                "effect_template": "{{ value_json.effect }}",
                                "json_attributes_topic": "~/meta",
                                "icon": icon,
                                "effect": True,
                                # "effect_list": list(COLORS.keys()),
                                "effect_list": effect_list,
                                "device": hass_device,
                            }
                        ),
                    )

                # TODO: Stare at this to convince self, not writing unit test for this
                virtual.virtual_cfg["active"] = virtual.active
                virtual.virtual_cfg["effect"] = {}
                virtual.virtual_cfg["effect"]["type"] = "singleColor"
                virtual.virtual_cfg["effect"]["config"] = {"color": color}

                save_config(
                    config=self._ledfx.config,
                    config_dir=self._ledfx.config_dir,
                )

        # client.publish(
        #     f"{self._discovery_topic("light")}/{virtualid}/state",
        #     msg.payload,
        # )

    async def on_delete(self):
        """Integration is being removed from LedFx."""
        # TODO clean up all published configs
        self._client.publish(
            f"{self._discovery_topic("light")}/ledfxscene/config", json.dumps({})
        )
        self._client.publish(
            f"{self._discovery_topic("light")}/ledfxtransition/config",
            json.dumps({}),
        )
        self._client.publish(
            f"{self._discovery_topic("select")}/ledfxaudio/config", json.dumps({})
        )
        self._client.publish(
            f"{self._discovery_topic("select")}/ledfxsceneselect/config",
            json.dumps({}),
        )
        self._client.publish(
            f"{self._discovery_topic("select")}/ledfxtransitiontype/config",
            json.dumps({}),
        )
        self._client.publish(
            f"{self.discovery_topic}/number/ledfxtransitiontime/config",
            json.dumps({}),
        )
        self._client.publish(
            f"{self.discovery_topic}/sensor/ledfxpixelsensor/config",
            json.dumps({}),
        )
        self._client.publish(
            f"{self._discovery_topic("switch")}/ledfxplay/config", json.dumps({})
        )
        for virtual in self._ledfx.virtuals.values():
            self._client.publish(
                f"{self._discovery_topic("light")}/{virtual.id}/config",
                json.dumps({}),
            )

    def on_shutdown(self) -> None:
        """LedFx is shutting down. Perform necessary cleanup."""
        # TODO set availablity of entities?

        # TODO Stop client if present?
        if self._client:
            self._client.loop_stop()
            self._client = None

    async def disconnect(self) -> None:
        """Integration disabled. Disconnect from MQTT."""

        # Remove all listers
        for remove_listener in self._listeners:
            remove_listener()
        self._listeners.clear()

        # Stop client if present
        if self._client:
            self._client.loop_stop()
            self._client = None

        # Ensure super is called
        await super().disconnect()

    async def connect(self) -> None:
        """Integration enabled. Connect to MQTT and perform setup."""
        client = mqtt.Client()
        client.on_connect = self._on_mqtt_connect
        client.on_message = self._on_mqtt_message

        if self._config["username"] is not None:
            client.username_pw_set(
                self._config["username"], password=self._config["password"]
            )
        client.connect_async(
            self._config["ip_address"], self._config["port"], 60
        )
        client.loop_start()

        # Ensure super is called
        await super().connect()
