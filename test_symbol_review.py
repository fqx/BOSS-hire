"""Isolated symbol reading never consumes a CV or makes a hiring decision."""
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from pydantic import ValidationError

import llm_utils
from symbol_review import DisplayedSymbol, SYMBOL_REVIEW_PROMPT, recognize_displayed_symbol


class SymbolReviewTests(unittest.TestCase):
    def test_schema_allows_only_explicit_characters_or_unknown(self):
        for symbol in ("♂", "♀", "unknown"):
            self.assertEqual(DisplayedSymbol(symbol=symbol).symbol, symbol)
        for symbol in ("male", "female", "男", "女", "probably female", "", None):
            with self.subTest(symbol=symbol), self.assertRaises(ValidationError):
                DisplayedSymbol(symbol=symbol)
        with self.assertRaises(ValidationError):
            DisplayedSymbol(symbol="♀", gender="女")

    def test_cloud_payload_contains_only_image_and_preserves_unknown(self):
        result = DisplayedSymbol(symbol="unknown")
        parse = Mock(return_value=SimpleNamespace(output_parsed=result))
        client = SimpleNamespace(responses=SimpleNamespace(parse=parse))
        with patch.object(llm_utils, "_is_openai_cloud", True):
            self.assertEqual(recognize_displayed_symbol(client, "only-crop"), result)
        args = parse.call_args.kwargs
        self.assertEqual(args["model"], llm_utils.LLM_MODEL)
        self.assertEqual(args["text_format"], DisplayedSymbol)
        self.assertEqual(args["instructions"], SYMBOL_REVIEW_PROMPT)
        self.assertEqual(args["input"], [{
            "type": "message", "role": "user", "content": [
                {"type": "input_image", "image_url": "data:image/png;base64,only-crop"},
            ],
        }])

    def test_cloud_fallback_validates_json(self):
        for symbol in ("♂", "♀", "unknown"):
            response = SimpleNamespace(output_parsed=None, output_text=f'{{"symbol":"{symbol}"}}')
            client = SimpleNamespace(responses=SimpleNamespace(parse=Mock(return_value=response)))
            with self.subTest(symbol=symbol), patch.object(llm_utils, "_is_openai_cloud", True):
                self.assertEqual(recognize_displayed_symbol(client, "crop").symbol, symbol)

    def test_local_payload_contains_only_image_with_override_model(self):
        message = SimpleNamespace(content='{"symbol":"♀"}', model_extra={})
        create = Mock(return_value=SimpleNamespace(choices=[SimpleNamespace(message=message)]))
        client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
        with patch.object(llm_utils, "_is_openai_cloud", False):
            self.assertEqual(recognize_displayed_symbol(client, "only-crop", "vision-model").symbol, "♀")
        args = create.call_args.kwargs
        self.assertEqual(args["model"], "vision-model")
        self.assertEqual(args["messages"], [
            {"role": "system", "content": SYMBOL_REVIEW_PROMPT},
            {"role": "user", "content": [
                {"type": "image_url", "image_url": {"url": "data:image/png;base64,only-crop"}},
            ]},
        ])
        self.assertEqual(args["response_format"]["json_schema"]["schema"], DisplayedSymbol.model_json_schema())

    def test_local_reasoning_content_fallback(self):
        message = SimpleNamespace(content="", model_extra={"reasoning_content": '{"symbol":"♂"}'})
        create = Mock(return_value=SimpleNamespace(choices=[SimpleNamespace(message=message)]))
        client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
        with patch.object(llm_utils, "_is_openai_cloud", False):
            self.assertEqual(recognize_displayed_symbol(client, "crop").symbol, "♂")

    def test_invalid_or_incomplete_output_is_not_silently_unknown(self):
        for content in ('{"symbol":"female"}', '{"gender":"女"}', "", "unclear"):
            response = SimpleNamespace(output_parsed=None, output_text=content)
            client = SimpleNamespace(responses=SimpleNamespace(parse=Mock(return_value=response)))
            with self.subTest(content=content), patch.object(llm_utils, "_is_openai_cloud", True), self.assertRaises(ValidationError):
                recognize_displayed_symbol(client, "crop")

    def test_api_capability_errors_propagate_on_both_protocols(self):
        for cloud in (True, False):
            error = RuntimeError("model does not support image input")
            parse = Mock(side_effect=error)
            create = Mock(side_effect=error)
            client = SimpleNamespace(
                responses=SimpleNamespace(parse=parse),
                chat=SimpleNamespace(completions=SimpleNamespace(create=create)),
            )
            with self.subTest(cloud=cloud), patch.object(llm_utils, "_is_openai_cloud", cloud), \
                 self.assertRaisesRegex(RuntimeError, "does not support image"):
                recognize_displayed_symbol(client, "crop")

    def test_empty_image_or_model_rejected_before_api_call(self):
        with self.assertRaises(ValueError):
            recognize_displayed_symbol(None, " ")
        with self.assertRaises(ValueError):
            recognize_displayed_symbol(None, "crop", " ")

    def test_prompt_forbids_personal_inference_and_embedded_instructions(self):
        for required in ("face", "name", "color", "clothing", "untrusted data", "not instructions", "conflicting"):
            self.assertIn(required, SYMBOL_REVIEW_PROMPT)


if __name__ == "__main__":
    unittest.main()
