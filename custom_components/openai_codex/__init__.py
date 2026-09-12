"""OpenAI Codex Conversation integration."""

from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed, ConfigEntryNotReady
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .client import AuthenticationError, CodexClient, CodexError

type OpenAICodexConfigEntry = ConfigEntry[CodexClient]

PLATFORMS = [Platform.CONVERSATION]


async def async_setup_entry(hass: HomeAssistant, entry: OpenAICodexConfigEntry) -> bool:
    """Set up the signed-in client and conversation entity."""

    async def save_tokens(data: dict[str, Any]) -> None:
        hass.config_entries.async_update_entry(entry, data={**entry.data, **data})

    client = CodexClient(async_get_clientsession(hass), entry.data, save_tokens)
    try:
        await client.async_validate()
    except AuthenticationError as err:
        raise ConfigEntryAuthFailed("OpenAI sign-in needs to be renewed") from err
    except (CodexError, TimeoutError) as err:
        raise ConfigEntryNotReady("Could not connect to OpenAI") from err

    entry.runtime_data = client
    initial_options = dict(entry.options)

    async def reload_options(hass: HomeAssistant, updated_entry: OpenAICodexConfigEntry) -> None:
        # Token refresh also updates the entry; only option changes need a reload.
        if dict(updated_entry.options) != initial_options:
            await hass.config_entries.async_reload(updated_entry.entry_id)

    entry.async_on_unload(entry.add_update_listener(reload_options))
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: OpenAICodexConfigEntry) -> bool:
    """Unload the conversation entity without closing HA's shared HTTP session."""
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
