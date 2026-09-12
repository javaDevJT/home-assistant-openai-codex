"""Exercise native HA flow results and credential lifecycle without a login."""

import asyncio
import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from types import MappingProxyType
from unittest.mock import AsyncMock, Mock, patch

from homeassistant.config_entries import ConfigEntries, ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import AbortFlow, FlowResultType

from custom_components.openai_codex import async_setup_entry
from custom_components.openai_codex.client import DeviceCode, LoginExpiredError
from custom_components.openai_codex.config_flow import OpenAICodexConfigFlow, OpenAICodexOptionsFlow
from custom_components.openai_codex.const import DOMAIN


def entry(account="account"):
    return ConfigEntry(
        data={
            "account_id": account,
            "access_token": "old",
            "refresh_token": "refresh",
            "expires_at": 1,
        },
        discovery_keys=MappingProxyType({}),
        domain=DOMAIN,
        minor_version=1,
        options={},
        source="user",
        subentries_data=[],
        title="OpenAI",
        unique_id=account,
        version=1,
    )


class FlowTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.hass = HomeAssistant(self.directory.name)
        self.hass.config_entries = ConfigEntries(self.hass, {})
        self.flow = OpenAICodexConfigFlow()
        self.flow.hass = self.hass
        self.flow.context = {"source": "user"}
        self.flow.handler = DOMAIN
        self.flow.flow_id = "flow"

    async def asyncTearDown(self):
        await self.hass.async_stop(force=True)

    async def test_device_progress_finishes_with_account_entry(self):
        self.assertEqual((await self.flow.async_step_user())["type"], FlowResultType.FORM)
        ready = asyncio.Event()

        async def finish(*args):
            await ready.wait()
            return {"account_id": "account", "access_token": "access", "refresh_token": "refresh"}

        with (
            patch(
                "custom_components.openai_codex.config_flow.async_get_clientsession",
                return_value=Mock(),
            ),
            patch(
                "custom_components.openai_codex.config_flow.async_start_device_login",
                AsyncMock(return_value=DeviceCode("id", "CODE", 5, 999999999999)),
            ),
            patch(
                "custom_components.openai_codex.config_flow.async_complete_device_login",
                side_effect=finish,
            ),
        ):
            progress = await self.flow.async_step_user({})
            self.assertEqual(progress["type"], FlowResultType.SHOW_PROGRESS)
            self.assertEqual(progress["description_placeholders"]["user_code"], "CODE")
            ready.set()
            await self.flow._login_task
        self.assertEqual((await self.flow.async_step_authorize())["step_id"], "finish")
        result = await self.flow.async_step_finish()
        self.assertEqual(result["type"], FlowResultType.CREATE_ENTRY)
        self.assertEqual(result["data"]["refresh_token"], "refresh")
        self.assertEqual(self.flow.unique_id, "account")

    async def test_expired_device_code_can_restart(self):
        async def expired():
            raise LoginExpiredError("expired")

        self.flow._code = DeviceCode("id", "CODE", 5, 0)
        self.flow._login_task = asyncio.create_task(expired())
        await asyncio.gather(self.flow._login_task, return_exceptions=True)
        self.assertEqual((await self.flow.async_step_authorize())["step_id"], "login_failed")
        self.assertEqual(
            (await self.flow.async_step_login_failed())["errors"]["base"], "login_expired"
        )

    async def test_reauth_cannot_switch_accounts(self):
        old = entry()
        self.flow.context = {"source": "reauth", "entry_id": old.entry_id}
        self.flow._tokens = {"account_id": "other"}
        with patch.object(self.flow, "_get_reauth_entry", return_value=old):
            result = await self.flow.async_step_finish()
        self.assertEqual(result["reason"], "wrong_account")

    async def test_successful_reauth_updates_same_entry(self):
        old = entry()
        self.flow.context = {"source": "reauth", "entry_id": old.entry_id}
        self.flow._tokens = {"account_id": "account", "access_token": "new"}
        with (
            patch.object(self.flow, "_get_reauth_entry", return_value=old),
            patch.object(
                self.flow,
                "async_update_reload_and_abort",
                return_value={"type": FlowResultType.ABORT},
            ) as update,
        ):
            await self.flow.async_step_finish()
        update.assert_called_once_with(old, data_updates=self.flow._tokens)

    async def test_duplicate_account_aborts(self):
        self.flow._tokens = {"account_id": "account"}
        with patch.object(
            self.hass.config_entries, "async_entry_for_domain_unique_id", return_value=entry()
        ):
            with self.assertRaises(AbortFlow) as error:
                await self.flow.async_step_finish()
        self.assertEqual(error.exception.reason, "already_configured")

    async def test_options_use_real_selectors(self):
        options = OpenAICodexOptionsFlow()
        options.hass = self.hass
        with patch.object(type(options), "config_entry", property(lambda _: entry())):
            result = await options.async_step_init()
            schema = result["data_schema"]
            values = schema({"model": "gpt-5.6-luna"})
            self.assertEqual(values["llm_hass_api"], [])
            self.assertTrue(values["prompt"])
            invalid = await options.async_step_init({"model": "  "})
            self.assertEqual(invalid["errors"]["model"], "invalid_model")
            self.assertEqual(
                (await options.async_step_init(values))["type"], FlowResultType.CREATE_ENTRY
            )

    async def test_refresh_saves_without_reloading_platforms(self):
        old = entry()
        fake_client = Mock(async_validate=AsyncMock())
        callbacks = []
        with (
            patch("custom_components.openai_codex.async_get_clientsession", return_value=Mock()),
            patch(
                "custom_components.openai_codex.CodexClient", return_value=fake_client
            ) as factory,
            patch.object(self.hass.config_entries, "async_forward_entry_setups", AsyncMock()),
            patch.object(self.hass.config_entries, "async_update_entry") as update,
            patch.object(
                type(old),
                "add_update_listener",
                lambda _, cb: callbacks.append(cb) or (lambda: None),
            ),
            patch.object(self.hass.config_entries, "async_reload", AsyncMock()) as reload,
        ):
            await async_setup_entry(self.hass, old)
            save = factory.call_args.args[2]
            await save({"access_token": "new", "refresh_token": "rotated"})
            self.assertEqual(update.call_args.kwargs["data"]["refresh_token"], "rotated")
            await callbacks[0](self.hass, old)
            reload.assert_not_awaited()


class MetadataTest(unittest.TestCase):
    def test_manifest_and_english_translation(self):
        component = Path(__file__).parents[1] / "custom_components" / DOMAIN
        manifest = json.loads((component / "manifest.json").read_text())
        self.assertEqual(manifest["domain"], DOMAIN)
        self.assertTrue(manifest["config_flow"])
        self.assertEqual(
            json.loads((component / "strings.json").read_text()),
            json.loads((component / "translations/en.json").read_text()),
        )


if __name__ == "__main__":
    unittest.main()
