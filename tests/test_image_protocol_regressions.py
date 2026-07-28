from __future__ import annotations

import json
import unittest
from unittest import mock

import pybase64

from services.openai_backend_api import OpenAIBackendAPI
from services.protocol import conversation
from services.protocol.conversation import ConversationRequest, ConversationState, update_conversation_state
from utils.helper import iter_sse_payloads
from utils.pow import build_legacy_requirements_token, build_pow_config


class FakeResponse:
    def __init__(self, lines: list[bytes]):
        self.lines = lines

    def iter_lines(self):
        yield from self.lines


class ImageProtocolRegressionTests(unittest.TestCase):
    def test_android_bundle_uses_current_pow_payload(self):
        config = build_pow_config("test-agent", ["/sdk.js"], "build")
        self.assertEqual(len(config), 25)
        self.assertEqual(config[3], 1)
        self.assertIsInstance(config[9], float)

        token = build_legacy_requirements_token("test-agent", ["/sdk.js"], "build")
        decoded = json.loads(pybase64.b64decode(token.removeprefix("gAAAAAC")))
        self.assertEqual(len(decoded), 25)

    def test_android_bundle_uses_current_image_model(self):
        backend = object.__new__(OpenAIBackendAPI)
        self.assertEqual(backend._image_model_slug("gpt-image-2"), "gpt-5-5")

    def test_multiline_sse_event_is_assembled(self):
        response = FakeResponse([b"event: message", b'data: {"value":', b'data: 1}', b""])
        self.assertEqual(list(iter_sse_payloads(response)), ['{"value":\n1}'])

    def test_nested_image_patch_collects_full_file_id(self):
        file_id = "file_abc-123_def"
        payload = json.dumps({
            "o": "patch",
            "v": [{
                "message": {
                    "author": {"role": "tool"},
                    "metadata": {"async_task_type": "image_gen"},
                    "content": {
                        "content_type": "multimodal_text",
                        "parts": [{"asset_pointer": f"file-service://{file_id}"}],
                    },
                },
            }],
            "conversation_id": "conv-1",
        })
        state = ConversationState()
        update_conversation_state(state, payload, json.loads(payload))
        self.assertEqual(state.file_ids, [file_id])

    def test_nested_user_image_is_not_an_output(self):
        payload = json.dumps({
            "o": "patch",
            "v": [{
                "message": {
                    "author": {"role": "user"},
                    "content": {
                        "content_type": "multimodal_text",
                        "parts": [{"asset_pointer": "file-service://file_input-123"}],
                    },
                },
            }],
        })
        state = ConversationState()
        update_conversation_state(state, payload, json.loads(payload))
        self.assertEqual(state.file_ids, [])

    def test_empty_upstream_result_is_an_error(self):
        class FakeBackend:
            def resolve_conversation_image_urls(self, *args):
                return []

        done = {
            "type": "conversation.done",
            "conversation_id": "conv-1",
            "file_ids": [],
            "sediment_ids": [],
            "text": "",
        }
        with mock.patch.object(conversation, "conversation_events", return_value=iter([done])):
            with self.assertRaisesRegex(RuntimeError, "no image result"):
                list(conversation.stream_image_outputs(
                    FakeBackend(),
                    ConversationRequest(model="gpt-image-2", prompt="cat"),
                ))


if __name__ == "__main__":
    unittest.main()
