"""Home Assistant conversation and Assist tools over a Codex sign-in."""

import json
from collections.abc import Iterable
from typing import Any, Literal

from homeassistant.components import conversation
from homeassistant.const import CONF_LLM_HASS_API, CONF_PROMPT, MATCH_ALL
from homeassistant.core import HomeAssistant
from homeassistant.helpers import intent, llm
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.json import json_dumps
from probatio import to_openapi

from . import OpenAICodexConfigEntry
from .client import AuthenticationError, CodexError
from .const import CONF_MODEL, DEFAULT_MODEL, DOMAIN, MAX_TOOL_ROUNDS

PARALLEL_UPDATES = 0


async def async_setup_entry(
    hass: HomeAssistant,
    entry: OpenAICodexConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Add the account's conversation entity."""
    async_add_entities([OpenAICodexConversationEntity(entry)])


def _messages(content: Iterable[conversation.Content]) -> list[dict[str, Any]]:
    """Convert HA history into Responses input, keeping tool call IDs intact."""
    messages: list[dict[str, Any]] = []
    for item in content:
        if isinstance(item, conversation.SystemContent):
            continue
        if isinstance(item, conversation.ToolResultContent):
            messages.append(
                {
                    "type": "function_call_output",
                    "call_id": item.tool_call_id,
                    "output": json_dumps(item.tool_result),
                }
            )
            continue
        if isinstance(item, conversation.AssistantContent) and isinstance(item.native, list):
            messages.extend(
                native
                for native in item.native
                if isinstance(native, dict)
                and native.get("type") == "reasoning"
                and isinstance(native.get("encrypted_content"), str)
            )
        if item.content:
            messages.append({"role": item.role, "content": item.content})
        if isinstance(item, conversation.AssistantContent):
            for call in item.tool_calls or []:
                messages.append(
                    {
                        "type": "function_call",
                        "call_id": call.id,
                        "name": call.tool_name,
                        "arguments": json_dumps(call.tool_args),
                    }
                )
    return messages


def _assistant_content(
    response: dict[str, Any], agent_id: str, allowed_tools: set[str]
) -> conversation.AssistantContent:
    """Validate model output before any tool can run."""
    text: list[str] = []
    calls: list[llm.ToolInput] = []
    reasoning: list[dict[str, Any]] = []
    call_ids: set[str] = set()
    output = response.get("output")
    if not isinstance(output, list):
        raise CodexError("OpenAI returned an invalid response")

    for item in output:
        if not isinstance(item, dict):
            raise CodexError("OpenAI returned an invalid response item")
        if item.get("type") == "message":
            parts = item.get("content", [])
            if not isinstance(parts, list):
                raise CodexError("OpenAI returned invalid message content")
            for part in parts:
                if not isinstance(part, dict):
                    raise CodexError("OpenAI returned invalid message content")
                value = (
                    part.get("text")
                    if part.get("type") == "output_text"
                    else part.get("refusal")
                    if part.get("type") == "refusal"
                    else None
                )
                if value is not None:
                    if not isinstance(value, str):
                        raise CodexError("OpenAI returned invalid message text")
                    text.append(value)
        elif item.get("type") == "function_call":
            name, call_id = item.get("name"), item.get("call_id")
            if (
                not isinstance(name, str)
                or name not in allowed_tools
                or not isinstance(call_id, str)
                or not call_id
                or call_id in call_ids
            ):
                raise CodexError("OpenAI requested an unavailable or invalid tool")
            try:
                args = json.loads(item["arguments"])
            except (KeyError, TypeError, ValueError) as err:
                raise CodexError("OpenAI returned invalid tool arguments") from err
            if not isinstance(args, dict):
                raise CodexError("OpenAI tool arguments must be a JSON object")
            call_ids.add(call_id)
            calls.append(llm.ToolInput(tool_name=name, tool_args=args, id=call_id))
        elif item.get("type") == "reasoning" and item.get("encrypted_content"):
            reasoning.append(item)

    if not text and not calls:
        raise CodexError("OpenAI returned no answer")
    return conversation.AssistantContent(
        agent_id=agent_id,
        content="\n".join(text) or None,
        tool_calls=calls or None,
        native=reasoning or None,
    )


class OpenAICodexConversationEntity(
    conversation.ConversationEntity, conversation.AbstractConversationAgent
):
    """A text conversation agent with optional HA-managed Assist tools."""

    def __init__(self, entry: OpenAICodexConfigEntry) -> None:
        self.entry = entry
        self._attr_unique_id = entry.entry_id
        self._attr_name = entry.title
        if entry.options.get(CONF_LLM_HASS_API):
            self._attr_supported_features = conversation.ConversationEntityFeature.CONTROL

    @property
    def supported_languages(self) -> list[str] | Literal["*"]:
        """Use Home Assistant's language context for all supported languages."""
        return MATCH_ALL

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        conversation.async_set_agent(self.hass, self.entry, self)

    async def async_will_remove_from_hass(self) -> None:
        conversation.async_unset_agent(self.hass, self.entry)
        await super().async_will_remove_from_hass()

    async def _async_handle_message(
        self,
        user_input: conversation.ConversationInput,
        chat_log: conversation.ChatLog,
    ) -> conversation.ConversationResult:
        """Generate a response, letting HA validate and execute exposed tools."""
        options = self.entry.options
        try:
            await chat_log.async_provide_llm_data(
                user_input.as_llm_context(DOMAIN),
                options.get(CONF_LLM_HASS_API),
                options.get(CONF_PROMPT),
                user_input.extra_system_prompt,
            )
            api = chat_log.llm_api
            tools = (
                [
                    {
                        "type": "function",
                        "name": tool.name,
                        "description": tool.description or "",
                        "parameters": to_openapi(
                            tool.parameters, custom_serializer=api.custom_serializer
                        ),
                        "strict": False,
                    }
                    for tool in api.tools
                ]
                if api
                else []
            )
            allowed_tools = {tool["name"] for tool in tools}
            instructions = "\n\n".join(
                item.content
                for item in chat_log.content
                if isinstance(item, conversation.SystemContent) and item.content
            )
            for round_number in range(MAX_TOOL_ROUNDS + 1):
                result = await self.entry.runtime_data.async_request(
                    model=options.get(CONF_MODEL, DEFAULT_MODEL),
                    instructions=instructions,
                    messages=_messages(chat_log.content),
                    tools=tools,
                )
                content = _assistant_content(result, self.entity_id, allowed_tools)
                previous_call_ids = {
                    call.id
                    for item in chat_log.content
                    if isinstance(item, conversation.AssistantContent)
                    for call in item.tool_calls or []
                }
                if any(call.id in previous_call_ids for call in content.tool_calls or []):
                    raise CodexError("OpenAI repeated a tool call that was already handled")
                if content.tool_calls and round_number == MAX_TOOL_ROUNDS:
                    raise CodexError("OpenAI exceeded the maximum tool rounds")
                async for _ in chat_log.async_add_assistant_content(content):
                    pass
                if not content.tool_calls:
                    return conversation.async_get_result_from_chat_log(user_input, chat_log)
        except conversation.ConverseError as err:
            return err.as_conversation_result()
        except AuthenticationError:
            self.entry.async_start_reauth(self.hass)
            error = "OpenAI sign-in expired. Reauthenticate the integration."
        except CodexError as err:
            error = str(err)
        except TimeoutError:
            error = "Unable to complete the request with OpenAI. Please try again."

        response = intent.IntentResponse(language=user_input.language)
        response.async_set_error(intent.IntentResponseErrorCode.UNKNOWN, error)
        return conversation.ConversationResult(
            response=response, conversation_id=chat_log.conversation_id
        )
