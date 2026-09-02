from __future__ import annotations

import base64
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from film_asset_pipeline.hunyuan import Hunyuan3DClient


class Hunyuan3DClientTests(unittest.TestCase):
    def setUp(self) -> None:
        self.client = Hunyuan3DClient(
            api_style="tokenhub",
            submit_endpoint="https://example.test/submit",
            query_endpoint="https://example.test/query",
            api_key="test-key",
            model="hy-3d-3.1",
            minimal_request=False,
            enable_pbr=True,
            face_count=1_000_000,
            generate_type="Normal",
            timeout_seconds=30,
        )

    def test_submit_payload_uses_raw_base64_and_pro_settings(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "asset.jpg"
            source.write_bytes(b"image-bytes")
            payload = self.client.build_submit_payload(source)
        self.assertEqual(payload["model"], "hy-3d-3.1")
        self.assertEqual(payload["image_base64"], base64.b64encode(b"image-bytes").decode("ascii"))
        self.assertTrue(payload["enable_pbr"])
        self.assertEqual(payload["face_count"], 1_000_000)
        self.assertEqual(payload["generate_type"], "Normal")
        self.assertNotIn("result_format", payload)

    def test_ai3d_openai_payload_uses_pascal_case_and_data_url(self) -> None:
        client = Hunyuan3DClient(
            api_style="ai3d_openai",
            submit_endpoint="https://example.test/submit",
            query_endpoint="https://example.test/query",
            api_key="test-key",
            model="hy-3d-3.1",
            minimal_request=False,
            enable_pbr=True,
            face_count=1_000_000,
            generate_type="Normal",
            timeout_seconds=30,
        )
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "asset.jpg"
            source.write_bytes(b"image-bytes")
            payload = client.build_submit_payload(source)
        self.assertEqual(payload["Model"], "3.1")
        self.assertTrue(payload["ImageUrl"]["Url"].startswith("data:image/jpeg;base64,"))
        self.assertTrue(payload["EnablePBR"])
        self.assertEqual(payload["FaceCount"], 1_000_000)

    def test_ai3d_minimal_payload_omits_optional_parameters(self) -> None:
        client = Hunyuan3DClient(
            api_style="ai3d_openai",
            submit_endpoint="https://example.test/submit",
            query_endpoint="https://example.test/query",
            api_key="test-key",
            model="hy-3d-3.1",
            minimal_request=True,
            enable_pbr=False,
            face_count=500_000,
            generate_type="Normal",
            timeout_seconds=30,
        )
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "asset.jpg"
            source.write_bytes(b"image-bytes")
            payload = client.build_submit_payload(source)
        self.assertEqual(set(payload), {"Model", "ImageUrl"})
        self.assertEqual(payload["Model"], "3.1")

    def test_tencentcloud_sdk_payload_uses_raw_image_base64(self) -> None:
        client = Hunyuan3DClient(
            api_style="tencentcloud_sdk",
            submit_endpoint="",
            query_endpoint="",
            api_key="",
            secret_id="test-secret-id",
            secret_key="test-secret-key",
            model="hy-3d-3.1",
            minimal_request=False,
            enable_pbr=True,
            face_count=1_000_000,
            generate_type="Normal",
            timeout_seconds=30,
        )
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "asset.jpg"
            source.write_bytes(b"image-bytes")
            payload = client.build_submit_payload(source)
            with patch.object(
                client,
                "_cloud_call",
                return_value={"Response": {"JobId": "job-1", "RequestId": "request-1"}},
            ) as cloud_call:
                submitted = client.submit(source)
        self.assertEqual(payload["ImageBase64"], base64.b64encode(b"image-bytes").decode("ascii"))
        self.assertNotIn("data:image", payload["ImageBase64"])
        self.assertEqual(payload["Model"], "3.1")
        self.assertEqual(submitted.job_id, "job-1")
        cloud_call.assert_called_once_with("SubmitHunyuanTo3DProJob", payload)

    def test_query_parses_completed_glb(self) -> None:
        response = {
            "request_id": "request-1",
            "status": "completed",
            "result_credit_consumed": 30,
            "data": [
                {
                    "type": "glb",
                    "url": "https://example.test/model.glb",
                    "preview_image_url": "https://example.test/preview.jpg",
                }
            ],
        }
        with patch.object(self.client, "_post", return_value=response):
            result = self.client.query("job-1")
        self.assertEqual(result.status, "completed")
        self.assertEqual(result.files[0].file_type, "glb")
        self.assertEqual(result.credits_consumed, 30)

    def test_http_error_parses_ai3d_compatibility_error(self) -> None:
        error = self.client._http_error(
            400,
            b'{"Type":2,"Code":1001,"Msg":"Invalid param","Desc":""}',
        )
        self.assertEqual(error.status_code, 400)
        self.assertEqual(error.error_code, "1001")
        self.assertEqual(str(error), "Invalid param")


if __name__ == "__main__":
    unittest.main()
