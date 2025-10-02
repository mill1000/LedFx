import json
import logging
import re
import socket
from dataclasses import dataclass
from typing import Any, Callable, Optional

import paho.mqtt.client as mqtt
import voluptuous as vol

from ledfx.color import parse_color
from ledfx.config import save_config
from ledfx.consts import PROJECT_VERSION
from ledfx.effects.audio import AudioInputSource
from ledfx.effects.singleColor import SingleColorEffect
from ledfx.effects import DummyEffect
from ledfx.events import Event
from ledfx.integrations import Integration

_LOGGER = logging.getLogger(__name__)

STATE_ON = "ON"
STATE_OFF = "OFF"


@dataclass
class EntityConfig:
    name: str
    unique_id: str
    icon: str
    entity_category: Optional[str] = None


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
        self._listeners = []  # TODO rename, these are ledfx listeners

        self._host = f"{extract_ip()}:{ledfx.port}"  # TODO hash this into a "unique" ID? or get MAC address?

        self._state_prefix = self._config['state_topic']
        self._discovery_prefix = self._config['discovery_topic']

        self._mqtt_listeners = []

    def _add_mqtt_listener(self, topic_regex: str, callback: Callable):
        self._mqtt_listeners.append((re.compile(topic_regex), callback))

    def _discovery_topic(self, platform: str) -> str:
        return f"{self._discovery_prefix}/{platform}/ledfx"

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

    def _publish_sensor_discovery_config(self, sensor: str, config: EntityConfig) -> None:
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

    def _publish_select_discovery_config(self, select: str, options: list[str], config: EntityConfig) -> None:
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

    def _publish_switch_discovery_config(self, switch: str, config: EntityConfig) -> None:
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

    def _publish_light_discovery_config(self, light: str, effects: list[str], config: EntityConfig) -> None:
        """Publish a light component discovery config."""
        discovery_config = {
            "name": config.name,
            "unique_id": config.unique_id,
            "icon": config.icon,
            "device": self._hass_device,
            "~": f"{self._state_prefix}/virtuals/{light}",
            "cmd_t": "~/set",
            "stat_t": "~/state",
            "schema": "json",
            "brightness": True,
            "brightness_scale": 100,
            "effect": True if len(effects) > 0 else False,
            "effect_list": effects,
            "flash": False,
            "json_attributes_topic": "~/attributes",
            "supported_color_modes": ["rgb"],  # TODO
            # TODO transition?
            # TODO white scale?
        }

        if category := config.entity_category:
            discovery_config["entity_category"] = category

        self._client.publish(
            f"{self._discovery_topic("light")}/{light}/config",
            json.dumps(discovery_config),
        )

    def _publish_discovery_config(self) -> None:
        """Publish all discovery configs."""
        # Pixle count sensor
        pixel_count = EntityConfig(
            name="Pixel Count",
            unique_id="ledfxpixelsensor",  # TODO
            icon="mdi:led-variant-outline",
            entity_category="diagnostic"
        )
        self._publish_sensor_discovery_config("pixel_count_sensor", pixel_count)

        # Scene selector
        scene_select = EntityConfig(
            name="Scene",
            unique_id="ledfxsceneselect",  # TODO
            icon="mdi:image-multiple-outline",
        )
        self._publish_select_discovery_config(
            "scene",
            # TODO how to deal with scene updates
            # TODO add "no scene" option?
            list(self._ledfx.scenes._scenes.keys()),
            scene_select,
        )

       # Audio selector
        audio_select = EntityConfig(
            name="Audio Source",
            unique_id="ledfxaudio",  # TODO
            icon="mdi:volume-high",
        )
        self._publish_select_discovery_config(
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
        self._publish_switch_discovery_config("pause", main_switch)

        # Create light for each virtual segment
        for virtual in self._ledfx.virtuals.values():
            # TODO are these valid?
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

            self._publish_light_discovery_config(
                virtual.id,
                [e.NAME for e in self._ledfx.effects.classes().values()],
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

    def _get_audio_source(self) -> str:
        audio_config = self._ledfx.config.get("audio", {})
        index = audio_config.get("audio_device", AudioInputSource.default_device_index())
        return AudioInputSource.input_devices()[index]

    def _publish_initial_state(self) -> None:

        # TODO There's no such thing as an active "scene"

        # Audio source
        self._client.publish(
            f"{self._state_prefix}/audio_source/state",
            self._get_audio_source(),
        )

        # Global pause state
        self._client.publish(
            f"{self._state_prefix}/pause/state",
            STATE_OFF if self._ledfx.virtuals._paused else STATE_ON,
        )

        # Publish each virtual
        for virtual in self._ledfx.virtuals.values():
            state = {
                "state": STATE_ON if virtual.active else STATE_OFF
            }

            if effect := virtual.active_effect:
                state["effect"] = effect.name
                state["brightness"] = 100 * effect.brightness
                if effect.name == SingleColorEffect.NAME:
                    color = parse_color(effect.config["color"])
                    state["color"] = {
                        "r": color.red,
                        "g": color.green,
                        "b": color.blue
                    }
                    state["color_mode"] = "rgb"  # TODO color ignored if no color_mode?

            _LOGGER.warning("Publish virtual state %r", state)
            self._client.publish(
                f"{self._state_prefix}/virtuals/{virtual.id}/state",
                json.dumps(state)
            )

            self._client.publish(
                f"{self._state_prefix}/virtuals/{virtual.id}/attributes",
                json.dumps(virtual.config),
            )


    def _on_virtual_config_update(self, event):
        # Event on settings change but not edit device
        _LOGGER.warning("Virtual config update event %s fosr %s", event, event.virtual_id)

        virtual = self._ledfx.virtuals.get(event.virtual_id)
        self._client.publish(
            f"{self._state_prefix}/virtuals/{event.virtual_id}/attributes",
            json.dumps(virtual.config),
        )

    def _on_system_config_update(self, event):
        _LOGGER.warning("System config event %s", event)

        # Send potentially updated audio device
        self._client.publish(
            f"{self._state_prefix}/audio_source/state",
            self._get_audio_source(),
        )

    def _on_scene_activated(self, event):
        # Was able to trigger
        _LOGGER.warning("Scene activated event %s", event)
        self._client.publish(
            f"{self._state_prefix}/scene/state",
            event.scene_id,
        )

    def _on_global_state_paused(self, event):
        # Was able to trigger
        # TODO event should obviously have current paused state
        _LOGGER.warning("Global state updated %s", event)
        self._client.publish(
            f"{self._state_prefix}/pause/state",
            STATE_OFF if self._ledfx.virtuals._paused else STATE_ON,
        )

    def _on_virtual_update(self, event):
        # Was able to trigger
        _LOGGER.warning("Virtual update event %s for %s", event.event_type, event.virtual_id)

        virtual = self._ledfx.virtuals.get(event.virtual_id)

        state = {
            "state": STATE_ON if virtual.active else STATE_OFF
        }

        if event.event_type == Event.EFFECT_SET:
            if effect := virtual.active_effect:
                state["effect"] = effect.name
                state["brightness"] = 100 * effect.brightness
                if effect.name == SingleColorEffect.NAME:
                    color = parse_color(effect.config["color"])
                    state["color"] = {
                        "r": color.red,
                        "g": color.green,
                        "b": color.blue
                    }
                    state["color_mode"] = "rgb"  # TODO color ignored if no color_mode?

        _LOGGER.warning("Publish virtual state %r", state)
        self._client.publish(
            f"{self._state_prefix}/virtuals/{virtual.id}/state",
            json.dumps(state)
        )

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

        self._listeners.append(
            self._ledfx.events.add_listener(
                self._on_scene_activated, Event.SCENE_ACTIVATED,
            )
        )

        self._listeners.append(
            self._ledfx.events.add_listener(
                self._on_virtual_update, Event.EFFECT_SET,
            )
        )

        self._listeners.append(
            self._ledfx.events.add_listener(
                self._on_virtual_update, Event.VIRTUAL_PAUSE
            )
        )

        # Useless event, when is an effect cleared but the virtual remains on?
        # self._listeners.append(
        #     self._ledfx.events.add_listener(
        #         self._on_virtual_update,
        #         Event.EFFECT_CLEARED,
        #     )
        # )

        self._listeners.append(
            self._ledfx.events.add_listener(
                self._on_virtual_config_update,
                Event.VIRTUAL_CONFIG_UPDATE,
            )
        )

        self._listeners.append(
            self._ledfx.events.add_listener(
                self._on_global_state_paused, Event.GLOBAL_PAUSE
            )
        )

        # Event is broken, config
        # self._listeners.append(
        #     self._ledfx.events.add_listener(
        #         self._on_audio_source_changed,
        #         Event.AUDIO_INPUT_DEVICE_CHANGED,
        #     )
        # )

        self._listeners.append(
            self._ledfx.events.add_listener(
                self._on_system_config_update,
                Event.BASE_CONFIG_UPDATE,
            )
        )

        self._publish_discovery_config()

        # Subscribe to all set topics for all entities and virtuals
        self._client.subscribe(f"{self._state_prefix}/+/set")
        self._client.subscribe(f"{self._state_prefix}/virtuals/+/set")

        # Add listener to catch all virtual set command
        self._add_mqtt_listener(rf"{self._state_prefix}/virtuals/(?P<virtual_id>[^/]+)/set", self._on_virtual_set)

        # Add listner to catch any set command for basic entities
        self._add_mqtt_listener(rf"{self._state_prefix}/(?P<entity>[^/]+)/set", self._on_entity_set)

        self._publish_initial_state();

        # TODO should publish entire states on connect
        # but updates can be partial?

    def _on_entity_set(self, topic, payload, match) -> None:
        """MQTT listener for set commands on base entities."""
        _LOGGER.warning("Handling set for %s: %s", topic, payload)

        # Get ID from RE match
        entity = match.group('entity')

        if entity == "pause":
            self._ledfx.virtuals.pause_all()
            return

        if entity == "scene":
            new_scene = payload.decode()

            # TODO are we passing names or IDs?
            if new_scene not in self._ledfx.config["scenes"].keys():
                _LOGGER.error("Unknown scene '%s'.", new_scene)
                return

            self._ledfx.scenes.activate(new_scene)
            return

        if entity == "audio_source":
            # Find selected source
            new_source = payload.decode()
            index = next((
                index
                for index, name in AudioInputSource.input_devices().items()
                if name == new_source
            ), None)

            if index is None:
                _LOGGER.error("Unknown audio source '%s'.", new_source)
                return

            # Update and save config
            new_config = self._ledfx.config.get("audio", {})
            new_config["audio_device"] = index
            self._ledfx.config["audio"] = new_config

            save_config(
                config=self._ledfx.config,
                config_dir=self._ledfx.config_dir,
            )

            if self._ledfx.audio:
                self._ledfx.audio.update_config(new_config)
            return

    def _on_virtual_set(self, topic, payload, match) -> None:
        """MQTT listener for set commands on virtuals"""
        _LOGGER.warning("Virtual set for %s: %s", topic, payload)

        # Get ID from RE match
        virtual_id = match.group('virtual_id')

        # Grab the virtual
        virtual = self._ledfx.virtuals.get(virtual_id, None)
        if not virtual:
            _LOGGER.error("Unknown virtual '%s'.", virtual_id)
            return

        # Parse JSON payload
        try:
            payload = json.loads(payload)
        except json.decoder.JSONDecodeError as e:
            _LOGGER.error("Failed to parse payload '%s'. Error: %s", payload, e)
            return

        if color := payload.get("color"):
            # Specific color requested
            effect = self._ledfx.effects.create(
                ledfx=self._ledfx,
                type="singleColor",  # TODO better way to get type?
                config={"color": f"#{color["r"]:02x}{color["g"]:02x}{color["b"]:02x}"},
            )
            virtual.set_effect(effect)

        if effect := payload.get("effect"):
            # Set provided effect
            effect_id = next((
                id
                for id, cls in self._ledfx.effects.classes().items()
                if cls.NAME == effect
            ), None)

            if not effect_id:
                _LOGGER.error("Unknown effect '%s'.", effect)
                return

            effect = self._ledfx.effects.create(
                ledfx=self._ledfx,
                type=effect_id,
                config=virtual.get_effects_config(effect_id)
            )

            # TODO probably need to try/except
            virtual.set_effect(effect)

            # Update effect config? Did we change it?
            virtual.update_effect_config(effect)

            save_config(
                config=self._ledfx.config,
                config_dir=self._ledfx.config_dir,
            )

        if brightness := payload.get("brightness"):
            # Set effect brightness
            if effect := virtual.active_effect:
                effect.brightness = float(brightness) / 100.0
                # TODO?
                #virtual.update_effect_config(effect)

        if state := payload.get("state"):
            # Virtual can't be activated without an effect
            # So first try to restore the previous, then fallback to solid color
            if not virtual.active_effect or isinstance(virtual.active_effect, DummyEffect):
                if ((last_effect := virtual.virtual_cfg.get("last_effect")) and 
                    (effect_config := virtual.get_effects_config(last_effect))):
                    # Set previous effect
                    effect = self._ledfx.effects.create(
                        ledfx=self._ledfx,
                        type=last_effect,
                        config=effect_config,
                    )
                else:
                     # Fall back to a color
                    effect = self._ledfx.effects.create(
                        ledfx=self._ledfx,
                        type="singleColor",  # TODO better way to get type?
                        config={"color": "orange"}, # Orange because WLED does it
                    )
            
                virtual.set_effect(effect)
                virtual.update_effect_config(effect)

            virtual.active = state == STATE_ON

    def _on_mqtt_message(self, client, userdata, msg) -> None:
        """MQTT callback when messages are received."""
        _LOGGER.error(
            "MQTT-Message incoming: \n[MQTT    ] Topic: "
            + msg.topic
            + "\n[MQTT    ] Payload: "
            + str(msg.payload)
        )

        # Sanity check incoming message is at the right prefix
        prefix, _ = msg.topic.split("/", maxsplit=1)
        if prefix != self._state_prefix:
            _LOGGER.warning("Received unexpected MQTT message at '%s'", msg.topic)
            return

        # Make required callbacks
        for pattern, callback in self._mqtt_listeners:
            if match := pattern.fullmatch(msg.topic):
                callback(msg.topic, msg.payload, match)

        # TODO want someway to know if there's an unhandled message

        return

        # TODO this is the initial state push
        # paused_state = "OFF"
        # if self._ledfx.virtuals._paused:
        #     paused_state = "OFF"
        # else:
        #     paused_state = "ON"

        # # React to Internal State-Handler
        # total_pixels = 0
        # for device in self._ledfx.devices.values():
        #     total_pixels += device.pixel_count

        # active_pixels = 0
        # for virtual in self._ledfx.virtuals.values():
        #     if virtual.active:
        #         active_pixels += virtual.pixel_count

        # if segs[0] == "ledfx":
        #     if payload == "HomeAssistant initialized":
        #         virtual = self._ledfx.virtuals.get(
        #             next(iter(self._ledfx.virtuals))
        #         )
        #         client.publish(
        #             f"{self._discovery_topic("select")}/ledfxtransitiontype/state",
        #             virtual.config["transition_mode"],
        #         )
        #         client.publish(
        #             f"{self.discovery_topic}/number/ledfxtransitiontime/state",
        #             virtual.config["transition_time"],
        #         )
        #         # PausedState
        #         client.publish(
        #             f"{self._discovery_topic("switch")}/ledfxplay/state",
        #             paused_state,
        #         )
        #         # AudioSelector
        #         client.publish(
        #             f"{self._discovery_topic("select")}/ledfxaudio/state",
        #             AudioInputSource.input_devices()[
        #                 self._ledfx.config.get("audio", {}).get(
        #                     "audio_device", {}
        #                 )
        #             ],
        #         )
        #         # Pixel-Sensor
        #         client.publish(
        #             f"{self.discovery_topic}/sensor/ledfxpixelsensor/state",
        #             str(active_pixels) + " / " + str(total_pixels),
        #         )
        #         # publish all virtual data on connect (meta)
        #         for virtual in self._ledfx.virtuals.values():
        #             self._publish_virtual_config(virtual.id, client)
        #             self._publish_virtual_paused(virtual.id, client)
        #     return

        # TODO transitions?
        # React to Transition-Type
        # if virtualid in self.TRANSITION_MAPPING.keys():
        #     # _LOGGER.info("Transitions: " + str(payload))
        #     prior_state = self._ledfx.config["global_transitions"]
        #     self._ledfx.config["global_transitions"] = True
        #     virtual = self._ledfx.virtuals.get(
        #         next(iter(self._ledfx.virtuals))
        #     )
        #     key = self.TRANSITION_MAPPING[virtualid]
        #     if key == "transition_time":
        #         try:
        #             val = float(payload)
        #         except ValueError as e:
        #             _LOGGER.warning(e)
        #             val = 0.5
        #     else:
        #         val = payload

        #     virtual.update_config({key: val})
        #     self._ledfx.config["global_transitions"] = prior_state

    async def on_delete(self):
        """Integration is being removed from LedFx."""
        # TODO clean up all published configs, these don't match new layout
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
