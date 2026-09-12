"""Run with: python -m unittest discover -s tests -p test_conversation.py."""

import json
import unittest
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import probatio as vol
from homeassistant.components import conversation
from homeassistant.core import Context, HomeAssistant

from custom_components.openai_codex.client import AuthenticationError, CodexError
from custom_components.openai_codex.const import CONF_MODEL, DEFAULT_MODEL
from custom_components.openai_codex.conversation import (
    OpenAICodexConversationEntity,
    _assistant_content,
    _messages,
)


def answer(text="Done."):
    return {
        "output": [
            {
                "type": "message",
                "content": [{"type": "output_text", "text": text}],
            }
        ]
    }


def tool_response(name="HassTurnOn", arguments='{"name":"Kitchen"}'):
    return {
        "output": [
            {
                "type": "reasoning",
                "id": "rs_1",
                "encrypted_content": "encrypted-reasoning",
                "summary": [],
            },
            {
                "type": "function_call",
                "call_id": "call_1",
                "name": name,
                "arguments": arguments,
            },
        ]
    }


class ConversationTest(unittest.IsolatedAsyncioTestCase):
    async def test_native_conversation_process_preserves_followup(self):
        self.client.async_request.side_effect = [answer("Hello."), answer("I remember.")]
        first = await self.entity.async_process(self.user_input)
        followup = conversation.ConversationInput(
            text="What did I just ask?",
            context=Context(),
            conversation_id=first.conversation_id,
            device_id=None,
            satellite_id=None,
            language="en",
            agent_id=self.entity.entity_id,
        )
        second = await self.entity.async_process(followup)
        self.assertEqual(second.response.speech["plain"]["speech"], "I remember.")
        request = self.client.async_request.await_args.kwargs
        self.assertTrue(request["instructions"])
        self.assertTrue(any(message.get("content") == "Hello." for message in request["messages"]))

    async def test_repeated_tool_call_does_not_execute_twice(self):
        api = SimpleNamespace(
            tools=[SimpleNamespace(name="HassTurnOn", description="", parameters=vol.Schema({}))],
            custom_serializer=None,
            async_call_tool=AsyncMock(return_value={"success": True}),
        )
        self.chat_log.llm_api = api
        self.client.async_request.side_effect = [tool_response(), tool_response()]
        with patch.object(conversation.ChatLog, "async_provide_llm_data", AsyncMock()):
            result = await self.entity._async_handle_message(self.user_input, self.chat_log)
        api.async_call_tool.assert_awaited_once()
        self.assertIsNotNone(result.response.error_code)

    async def asyncSetUp(self):
        self.config_dir = TemporaryDirectory()
        self.addCleanup(self.config_dir.cleanup)
        self.hass = HomeAssistant(self.config_dir.name)
        self.client = SimpleNamespace(async_request=AsyncMock())
        self.entry = SimpleNamespace(
            entry_id="test-entry",
            title="Test account",
            options={CONF_MODEL: DEFAULT_MODEL},
            runtime_data=self.client,
            async_start_reauth=Mock(),
        )
        self.entity = OpenAICodexConversationEntity(self.entry)
        self.entity.hass = self.hass
        self.entity.entity_id = "conversation.test_account"
        self.user_input = conversation.ConversationInput(
            text="Turn on Kitchen",
            context=Context(),
            conversation_id="test-conversation",
            device_id=None,
            satellite_id=None,
            language="en",
            agent_id=self.entity.entity_id,
        )
        self.chat_log = conversation.ChatLog(self.hass, "test-conversation")
        self.chat_log.content.extend(
            [
                conversation.SystemContent(content="Control only exposed entities."),
                conversation.UserContent(content="Turn on Kitchen"),
            ]
        )

    async def asyncTearDown(self):
        await self.hass.async_stop(force=True)

    async def test_native_chat_log_tool_round_trip_and_history(self):
        api = SimpleNamespace(
            custom_serializer=None,
            tools=[
                SimpleNamespace(
                    name="HassTurnOn",
                    description="Turn on an exposed entity",
                    parameters=vol.Schema({vol.Required("name"): str}),
                )
            ],
            async_call_tool=AsyncMock(return_value={"success": True}),
        )
        self.chat_log.llm_api = api
        self.client.async_request.side_effect = [tool_response(), answer()]
        with patch.object(conversation.ChatLog, "async_provide_llm_data", AsyncMock()):
            result = await self.entity._async_handle_message(self.user_input, self.chat_log)

        self.assertEqual(result.response.speech["plain"]["speech"], "Done.")
        self.assertEqual(result.conversation_id, "test-conversation")
        api.async_call_tool.assert_awaited_once()
        tool_input = api.async_call_tool.await_args.args[0]
        self.assertEqual(tool_input.tool_args, {"name": "Kitchen"})
        second_request = self.client.async_request.await_args_list[1].kwargs
        self.assertEqual(second_request["instructions"], "Control only exposed entities.")
        messages = second_request["messages"]
        self.assertTrue(any(m.get("encrypted_content") for m in messages))
        call = next(m for m in messages if m.get("type") == "function_call")
        output = next(m for m in messages if m.get("type") == "function_call_output")
        self.assertEqual(call["call_id"], output["call_id"])
        self.assertEqual(json.loads(output["output"]), {"success": True})
        self.assertEqual(second_request["tools"][0]["type"], "function")
        self.assertFalse(second_request["tools"][0]["strict"])

    async def test_no_tools_and_untrusted_response_rejected(self):
        for response in [
            tool_response(),
            tool_response("UnknownTool"),
            tool_response(arguments="[]"),
            tool_response(arguments="not-json"),
            {"output": [{"type": "message", "content": "invalid"}]},
            {"output": []},
        ]:
            with self.subTest(response=response):
                with self.assertRaises(CodexError):
                    _assistant_content(response, self.entity.entity_id, set())

        for arguments in ["[]", "null", "not-json"]:
            with self.subTest(arguments=arguments):
                with self.assertRaises(CodexError):
                    _assistant_content(
                        tool_response(arguments=arguments),
                        self.entity.entity_id,
                        {"HassTurnOn"},
                    )

        self.client.async_request.return_value = tool_response()
        with patch.object(conversation.ChatLog, "async_provide_llm_data", AsyncMock()):
            result = await self.entity._async_handle_message(self.user_input, self.chat_log)
        self.assertIsNotNone(result.response.error_code)
        self.assertFalse(self.client.async_request.await_args.kwargs["tools"])
        self.assertFalse(
            any(isinstance(item, conversation.AssistantContent) for item in self.chat_log.content)
        )

    async def test_reauth_and_tool_round_limit(self):
        self.client.async_request.side_effect = AuthenticationError("Sign-in expired")
        with patch.object(conversation.ChatLog, "async_provide_llm_data", AsyncMock()):
            result = await self.entity._async_handle_message(self.user_input, self.chat_log)
        self.entry.async_start_reauth.assert_called_once_with(self.hass)
        self.assertIsNotNone(result.response.error_code)

        self.client.async_request.side_effect = None
        self.client.async_request.return_value = tool_response()
        self.chat_log.llm_api = SimpleNamespace(
            custom_serializer=None,
            tools=[SimpleNamespace(name="HassTurnOn", description="", parameters=vol.Schema({}))],
            async_call_tool=AsyncMock(return_value={"success": True}),
        )
        with (
            patch.object(conversation.ChatLog, "async_provide_llm_data", AsyncMock()),
            patch("custom_components.openai_codex.conversation.MAX_TOOL_ROUNDS", 0),
        ):
            result = await self.entity._async_handle_message(self.user_input, self.chat_log)
        self.assertIsNotNone(result.response.error_code)
        self.chat_log.llm_api.async_call_tool.assert_not_awaited()

    async def test_history_ignores_other_agents_native_data(self):
        content = [
            conversation.AssistantContent(
                agent_id="conversation.other", content="Hello", native=object()
            ),
            conversation.ToolResultContent(
                agent_id=self.entity.entity_id,
                tool_call_id="call_2",
                tool_name="HassTurnOn",
                tool_result={"success": True},
            ),
        ]
        self.assertEqual(_messages(content)[0], {"role": "assistant", "content": "Hello"})


if __name__ == "__main__":
    unittest.main()
