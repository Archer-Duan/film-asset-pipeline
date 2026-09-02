from __future__ import annotations

import base64
import tempfile
import unittest
from pathlib import Path

from film_asset_pipeline.ark import ArkApiError, ArkImageClient


class ArkImageClientTests(unittest.TestCase):
    def setUp(self) -> None:
        self.client = ArkImageClient(
            endpoint="https://example.invalid/images/generations",
            api_key="test-key",
            model="doubao-seedream-5-0-lite-260128",
            size="2K",
            response_format="b64_json",
            watermark=False,
            timeout_seconds=10,
        )

    def test_group_payload_uses_base64_and_max_images(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            image_path = Path(directory) / "frame.png"
            image_path.write_bytes(b"\x89PNG\r\n\x1a\nmock")
            payload = self.client.build_payload(image_path, "提取物品", "group", 3)

        self.assertEqual(payload["model"], "doubao-seedream-5-0-lite-260128")
        self.assertEqual(payload["sequential_image_generation"], "auto")
        self.assertEqual(payload["sequential_image_generation_options"], {"max_images": 3})
        self.assertTrue(payload["image"][0].startswith("data:image/png;base64,"))
        encoded = payload["image"][0].split(",", 1)[1]
        self.assertEqual(base64.b64decode(encoded), b"\x89PNG\r\n\x1a\nmock")

    def test_single_payload_disables_group_fields(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            image_path = Path(directory) / "frame.jpg"
            image_path.write_bytes(b"\xff\xd8\xffmock")
            payload = self.client.build_payload(image_path, "只提取椅子", "single", 3)

        self.assertEqual(payload["sequential_image_generation"], "disabled")
        self.assertNotIn("sequential_image_generation_options", payload)

    def test_parse_response_preserves_nested_api_error(self) -> None:
        with self.assertRaises(ArkApiError) as captured:
            self.client._parse_response(
                {"error": {"code": "SetLimitExceeded", "message": "额度已用尽"}},
                {"X-Request-Id": "request-nested"},
            )

        error = captured.exception
        self.assertEqual(str(error), "额度已用尽")
        self.assertEqual(error.error_code, "SetLimitExceeded")
        self.assertEqual(error.request_id, "request-nested")

    def test_http_error_supports_top_level_error_fields(self) -> None:
        error = self.client._http_error(
            400,
            b'{"code":"InvalidParameter","message":"bad request"}',
            {"X-Tt-Logid": "request-top-level"},
        )

        self.assertEqual(str(error), "bad request")
        self.assertEqual(error.status_code, 400)
        self.assertEqual(error.error_code, "InvalidParameter")
        self.assertEqual(error.request_id, "request-top-level")
        self.assertFalse(error.retryable)

    def test_http_error_falls_back_for_non_json_response(self) -> None:
        error = self.client._http_error(502, b"bad gateway", {})

        self.assertEqual(str(error), "方舟请求失败，HTTP 502")
        self.assertEqual(error.error_code, "http_502")
        self.assertTrue(error.retryable)


if __name__ == "__main__":
    unittest.main()
