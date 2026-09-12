# OpenAI Codex Conversation

<img src="custom_components/openai_codex/brand/icon.png" alt="Cyan house-shaped chat bubble with a teal sparkle" width="144" height="144">

A custom Home Assistant conversation integration that signs in with your ChatGPT account instead of an OpenAI API key. Supports Assist conversations, follow-up questions, and optional control through Home Assistant's exposed tools.

**Requires Home Assistant 2026.9.0 or newer and a ChatGPT account with Codex access.** This is an unofficial integration using Codex's subscription backend. Access and usage limits depend on your account; OpenAI can change the backend independently of this integration. No separate API key is used.

## Install

### Manual installation

1. Extract the ZIP from the latest release into your Home Assistant configuration directory. The result must be `/config/custom_components/openai_codex/manifest.json`.
2. Restart Home Assistant.
3. Go to **Settings → Devices & services → Add integration → OpenAI Codex Conversation**.
4. Enable **device code login** in your ChatGPT account's **Security** settings, or ask your ChatGPT workspace administrator to enable it.
5. Continue in Home Assistant, open the OpenAI sign-in link, and enter the displayed code. Sign in and approve the request on OpenAI's website. The Home Assistant screen updates automatically. Codes expire after 15 minutes.
6. Go to **Settings → Voice assistants**, edit or create an assistant, and select the new conversation agent.

### HACS custom repository

1. Open **HACS → Custom repositories**.
2. Add `https://github.com/javaDevJT/home-assistant-openai-codex`, category **Integration**.
3. Download **OpenAI Codex Conversation** and restart Home Assistant.
4. Follow manual-installation steps 3–6 above to sign in and select the conversation agent.

This is a HACS **custom repository**, not an entry in the HACS default catalog. HACS installs from the repository. The [release ZIP](https://github.com/javaDevJT/home-assistant-openai-codex/releases/latest) is for manual installation.

## Settings and device control

Open the integration's **Configure** dialog to change:

| Setting | Behavior |
| --- | --- |
| Codex model | Defaults to `gpt-5.6-luna`. Enter another model your Codex account can access if needed. |
| Instructions | Home Assistant's standard voice-assistant prompt, editable with HA templates. |
| Home Assistant tools | Empty by default. Select **Assist** to permit control through Home Assistant's native LLM API. |

Expose the intended entities to Assist in Home Assistant. The integration delegates tool validation and execution to HA; it does not provide arbitrary shell commands, service execution, or access to unexposed entities. It permits at most ten tool rounds per user message.

Conversations use HA's native conversation ID and history. The backend streams internally, but this first version returns the completed answer to Assist. Existing speech-to-text and text-to-speech providers in your assistant pipeline continue to handle audio.

This version implements the conversation-agent portion of the built-in OpenAI integration. AI Task entities, image generation/analysis, web search, direct audio endpoints, multiple agent subentries, and API-only tuning options are not included. It sends text, the configured prompt, and any context/tools supplied by your selected HA LLM API to OpenAI.

## Credentials and recovery

- Your password and MFA code are entered only on OpenAI's website. No browser cookies or existing Codex credential files are imported.
- Access and refresh tokens are stored in the integration's Home Assistant config entry, like other OAuth integrations. HA config-entry storage is not a separate encrypted vault; protect the configuration directory and backups.
- Refresh tokens are updated automatically and saved after rotation. Refresh is serialized per account entry to avoid concurrent token rotation.
- If OpenAI rejects the saved login, Home Assistant starts reauthentication. Use the same account; add a separate integration for another account.
- A usage-limit error does not fall back to a billed API key. Wait for your account's allowance to recover.
- For HTTP 403 or model-access errors, check the selected model and your account's Codex access. Device-code setup errors can mean device login is disabled or the service is unavailable.
- Removing the integration removes its HA config entry; it does not revoke the authorization at OpenAI. Use ChatGPT security/session controls when remote revocation is needed.

## Development and validation

Requires Python 3.14.2 or newer. From this directory:

```sh
python3.14 -m venv .venv
.venv/bin/python -m pip install -r requirements-dev.txt
.venv/bin/python -m unittest discover -s tests -v
.venv/bin/ruff check custom_components tests
.venv/bin/ruff format --check custom_components tests
```

Local verification on September 12, 2026: all 27 tests passed; Ruff lint and formatting checks passed; the ZIP passed its integrity check.

Tests use the real Home Assistant 2026.9.2 Python classes with mocked OpenAI responses. No subscription login, inference request, live Home Assistant installation, or real device action is performed by the tests. Actual account authorization and a first Assist conversation must be checked after installation; local tests do not establish account access or live backend compatibility.

## Project files and protocol references

### Release 0.1.1

Fixes `OpenAI returned no answer` when Codex sends complete messages in earlier stream events and an empty final response. The parser now preserves completed text, tool calls, and encrypted reasoning while still rejecting failed or interrupted streams. Includes the integration logo. Update through HACS and restart Home Assistant; your existing sign-in is retained.

### Files and sources

- `custom_components/openai_codex/`: installable integration, device login, conversation agent, and translations.
- `custom_components/openai_codex/brand/icon.png`: transparent house-and-chat logo, also used for local Home Assistant branding.
- `tests/`: authorization, response parsing, config flow, and native HA conversation checks.
- `hacs.json`: HACS metadata; `requirements-dev.txt`: reproducible validation environment.
- [OpenAI device-code authentication](https://developers.openai.com/codex/auth).
- [OpenAI Codex protocol source](https://github.com/openai/codex/tree/53c542d944c705f3a66780a19223223bee57cbb6/codex-rs): device authorization in `login/src/device_code_auth.rs`, refresh in `login/src/auth/manager.rs`, and requests in `codex-api/src/endpoint/responses.rs`.
- [Codex stream parser and completion regression fixture](https://github.com/openai/codex/blob/b4c864dd6497ae764e6a826300b34f7ca77ba965/codex-rs/codex-api/src/sse/responses.rs): completed output items arrive before the final response metadata.
- [Home Assistant OpenAI integration source, 2026.9.2](https://github.com/home-assistant/core/tree/2026.9.2/homeassistant/components/openai_conversation).
- [Home Assistant conversation source, 2026.9.2](https://github.com/home-assistant/core/tree/2026.9.2/homeassistant/components/conversation).

Protocol and HA source checked September 12, 2026. This integration is independent of OpenAI and Home Assistant.
