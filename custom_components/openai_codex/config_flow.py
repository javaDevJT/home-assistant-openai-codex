"""Sign in on OpenAI's website using a one-time device code."""

import asyncio
from collections.abc import Mapping
from typing import Any

import voluptuous as vol
from homeassistant.config_entries import ConfigEntry, ConfigFlow, ConfigFlowResult, OptionsFlow
from homeassistant.const import CONF_LLM_HASS_API, CONF_PROMPT
from homeassistant.core import callback
from homeassistant.helpers import llm
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.selector import (
    SelectSelector,
    SelectSelectorConfig,
    TemplateSelector,
    TextSelector,
)

from .client import (
    VERIFICATION_URL,
    CodexError,
    DeviceCode,
    LoginExpiredError,
    async_complete_device_login,
    async_start_device_login,
)
from .const import CONF_MODEL, DEFAULT_MODEL, DEFAULT_NAME, DOMAIN


class OpenAICodexConfigFlow(ConfigFlow, domain=DOMAIN):
    """Manage initial authorization and reauthentication."""

    VERSION = 1

    def __init__(self) -> None:
        self._code: DeviceCode | None = None
        self._login_task: asyncio.Task[dict[str, Any]] | None = None
        self._tokens: dict[str, Any] | None = None
        self._login_error = "cannot_connect"

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        if user_input is not None:
            return await self._async_start_login("user")
        return self.async_show_form(step_id="user", data_schema=vol.Schema({}))

    async def async_step_reauth(self, entry_data: Mapping[str, Any]) -> ConfigFlowResult:
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        if user_input is not None:
            return await self._async_start_login("reauth_confirm")
        return self.async_show_form(step_id="reauth_confirm", data_schema=vol.Schema({}))

    async def _async_start_login(self, step_id: str) -> ConfigFlowResult:
        try:
            self._code = await async_start_device_login(async_get_clientsession(self.hass))
        except CodexError:
            return self.async_show_form(
                step_id=step_id, data_schema=vol.Schema({}), errors={"base": "cannot_connect"}
            )
        self._login_task = self.hass.async_create_background_task(
            async_complete_device_login(async_get_clientsession(self.hass), self._code),
            f"{DOMAIN} device sign-in",
        )
        return await self.async_step_authorize()

    async def async_step_authorize(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        assert self._login_task is not None and self._code is not None
        if not self._login_task.done():
            return self.async_show_progress(
                step_id="authorize",
                progress_action="authorize",
                progress_task=self._login_task,
                description_placeholders={
                    "verification_url": VERIFICATION_URL,
                    "user_code": self._code.user_code,
                },
            )
        try:
            self._tokens = self._login_task.result()
        except LoginExpiredError:
            self._login_error = "login_expired"
        except CodexError:
            self._login_error = "cannot_connect"
        else:
            return self.async_show_progress_done(next_step_id="finish")
        return self.async_show_progress_done(next_step_id="login_failed")

    async def async_step_login_failed(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        if user_input is not None:
            return await self._async_start_login("login_failed")
        return self.async_show_form(
            step_id="login_failed", data_schema=vol.Schema({}), errors={"base": self._login_error}
        )

    async def async_step_finish(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        assert self._tokens is not None
        await self.async_set_unique_id(self._tokens["account_id"])
        if self.source == "reauth":
            entry = self._get_reauth_entry()
            if entry.unique_id != self.unique_id:
                return self.async_abort(reason="wrong_account")
            return self.async_update_reload_and_abort(entry, data_updates=self._tokens)
        self._abort_if_unique_id_configured()
        return self.async_create_entry(title=DEFAULT_NAME, data=self._tokens)

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> OptionsFlow:
        return OpenAICodexOptionsFlow()


class OpenAICodexOptionsFlow(OptionsFlow):
    """Choose the subscription model, instructions, and permitted HA tools."""

    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            user_input[CONF_MODEL] = user_input[CONF_MODEL].strip()
            if user_input[CONF_MODEL]:
                return self.async_create_entry(title="", data=user_input)
            errors[CONF_MODEL] = "invalid_model"
        options = self.config_entry.options
        schema = vol.Schema(
            {
                vol.Required(
                    CONF_MODEL, default=options.get(CONF_MODEL, DEFAULT_MODEL)
                ): TextSelector(),
                vol.Optional(
                    CONF_PROMPT,
                    default=options.get(CONF_PROMPT, llm.DEFAULT_INSTRUCTIONS_PROMPT),
                ): TemplateSelector(),
                vol.Optional(
                    CONF_LLM_HASS_API, default=options.get(CONF_LLM_HASS_API, [])
                ): SelectSelector(
                    SelectSelectorConfig(
                        options=[
                            {"label": api.name, "value": api.id}
                            for api in llm.async_get_apis(self.hass)
                        ],
                        multiple=True,
                    )
                ),
            }
        )
        return self.async_show_form(step_id="init", data_schema=schema, errors=errors)
