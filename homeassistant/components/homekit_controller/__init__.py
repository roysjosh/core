"""Support for Homekit device discovery."""
from __future__ import annotations

import asyncio
import contextlib
import logging
import voluptuous as vol

import aiohomekit
from aiohomekit.exceptions import (
    AccessoryDisconnectedError,
    AccessoryNotFoundError,
    EncryptionError,
)

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import ATTR_IDENTIFIERS, EVENT_HOMEASSISTANT_STOP
from homeassistant.core import Event, HomeAssistant, ServiceCall
from homeassistant.exceptions import ConfigEntryNotReady
from homeassistant.helpers import config_validation as cv, device_registry as dr
from homeassistant.helpers.service import async_register_admin_service
from homeassistant.helpers.typing import ConfigType

from .config_flow import normalize_hkid
from .connection import HKDevice
from .const import (
    ATTR_HKID,
    ATTR_THREAD_CHANNEL,
    ATTR_THREAD_EXTENDED_PAN_ID,
    ATTR_THREAD_NETWORK_KEY,
    ATTR_THREAD_NETWORK_NAME,
    ATTR_THREAD_PAN_ID,
    ATTR_THREAD_UNKNOWN_FLAG,
    DOMAIN,
    KNOWN_DEVICES,
    SERVICE_THREAD_PROVISION,
    TRIGGERS
)
from .utils import async_get_controller

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up a HomeKit connection on a config entry."""
    conn = HKDevice(hass, entry, entry.data)
    hass.data[KNOWN_DEVICES][conn.unique_id] = conn

    # For backwards compat
    if entry.unique_id is None:
        hass.config_entries.async_update_entry(
            entry, unique_id=normalize_hkid(conn.unique_id)
        )

    try:
        await conn.async_setup()
    except (
        asyncio.TimeoutError,
        AccessoryNotFoundError,
        EncryptionError,
        AccessoryDisconnectedError,
    ) as ex:
        del hass.data[KNOWN_DEVICES][conn.unique_id]
        with contextlib.suppress(asyncio.TimeoutError):
            await conn.pairing.close()
        raise ConfigEntryNotReady from ex

    return True


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    """Set up for Homekit devices."""
    await async_get_controller(hass)

    hass.data[KNOWN_DEVICES] = {}
    hass.data[TRIGGERS] = {}

    async def _async_stop_homekit_controller(event: Event) -> None:
        await asyncio.gather(
            *(
                connection.async_unload()
                for connection in hass.data[KNOWN_DEVICES].values()
            )
        )

    hass.bus.async_listen_once(EVENT_HOMEASSISTANT_STOP, _async_stop_homekit_controller)

    async def thread_provision(service: ServiceCall) -> None:
        hkid: str = service.data[ATTR_HKID]
        network_name: str = service.data[ATTR_THREAD_NETWORK_NAME]
        channel: int = service.data[ATTR_THREAD_CHANNEL]
        pan_id: str = service.data[ATTR_THREAD_PAN_ID]
        extended_pan_id: str = service.data[ATTR_THREAD_EXTENDED_PAN_ID]
        network_key: str = service.data[ATTR_THREAD_NETWORK_KEY]
        unknown: int = service.data[ATTR_THREAD_UNKNOWN_FLAG]
        _LOGGER.warning("Provisioning Thread credentials: %s=%s, %s=%s, %s=%s, %s=%s, %s=%s, %s=%s, %s=%s" % (
            ATTR_HKID, hkid,
            ATTR_THREAD_NETWORK_NAME, network_name,
            ATTR_THREAD_CHANNEL, channel,
            ATTR_THREAD_PAN_ID, pan_id,
            ATTR_THREAD_EXTENDED_PAN_ID, extended_pan_id,
            ATTR_THREAD_NETWORK_KEY, 'REDACTED',
            ATTR_THREAD_UNKNOWN_FLAG, unknown,
        ))

        if hkid not in hass.data[KNOWN_DEVICES]:
            _LOGGER.warning("Unknown HKID")
            return

        connection: HKDevice = hass.data[KNOWN_DEVICES][hkid]
        await connection.pairing.thread_provision(network_name, channel, pan_id, extended_pan_id, network_key, unknown)

        return

    async_register_admin_service(
        hass,
        DOMAIN,
        SERVICE_THREAD_PROVISION,
        thread_provision,
        schema=vol.Schema(
            {
                vol.Required(ATTR_HKID): cv.string,
                vol.Required(ATTR_THREAD_NETWORK_NAME): vol.All(cv.string, vol.Length(max=16)),
                vol.Required(ATTR_THREAD_CHANNEL): vol.All(cv.positive_int, vol.Range(min=11, max=26)),
                vol.Required(ATTR_THREAD_PAN_ID): vol.All(cv.string, vol.Length(min=1, max=4)),
                vol.Required(ATTR_THREAD_EXTENDED_PAN_ID): vol.All(cv.string, vol.Length(min=1, max=16)),
                vol.Required(ATTR_THREAD_NETWORK_KEY): vol.All(cv.string, vol.Length(min=1, max=32)),
                vol.Required(ATTR_THREAD_UNKNOWN_FLAG): vol.All(cv.positive_int, vol.Range(min=0, max=255)),
            }
        )
    )

    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Disconnect from HomeKit devices before unloading entry."""
    hkid = entry.data["AccessoryPairingID"]

    if hkid in hass.data[KNOWN_DEVICES]:
        connection: HKDevice = hass.data[KNOWN_DEVICES][hkid]
        await connection.async_unload()

    return True


async def async_remove_entry(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Cleanup caches before removing config entry."""
    hkid = entry.data["AccessoryPairingID"]

    controller = await async_get_controller(hass)

    # Remove the pairing on the device, making the device discoverable again.
    # Don't reuse any objects in hass.data as they are already unloaded
    controller.load_pairing(hkid, dict(entry.data))
    try:
        await controller.remove_pairing(hkid)
    except aiohomekit.AccessoryDisconnectedError:
        _LOGGER.warning(
            "Accessory %s was removed from HomeAssistant but was not reachable "
            "to properly unpair. It may need resetting before you can use it with "
            "HomeKit again",
            entry.title,
        )


async def async_remove_config_entry_device(
    hass: HomeAssistant, config_entry: ConfigEntry, device_entry: dr.DeviceEntry
) -> bool:
    """Remove homekit_controller config entry from a device."""
    hkid = config_entry.data["AccessoryPairingID"]
    connection: HKDevice = hass.data[KNOWN_DEVICES][hkid]
    return not device_entry.identifiers.intersection(
        identifier
        for accessory in connection.entity_map.accessories
        for identifier in connection.device_info_for_accessory(accessory)[
            ATTR_IDENTIFIERS
        ]
    )
