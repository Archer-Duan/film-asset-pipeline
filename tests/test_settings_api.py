from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from film_asset_pipeline.web import create_app


class SettingsApiTests(unittest.TestCase):
    def test_credentials_are_saved_but_never_returned(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for relative in ("data/input", "data/output", "data/models", "data/state"):
                (root / relative).mkdir(parents=True, exist_ok=True)
            config = root / "config.toml"
            config.write_text(
                """
[paths]
input_dir = "data/input"
output_dir = "data/output"
state_db = "data/state/pipeline.sqlite3"
model_input = "data/output/manifest.json"
model_output_dir = "data/models"
model_state_db = "data/state/models.sqlite3"

[hunyuan]
api_style = "tencentcloud_sdk"
""".strip(),
                encoding="utf-8",
            )
            secrets: dict[str, str] = {}

            def get_secret(name: str, _: Path) -> str:
                return secrets.get(name, "")

            def set_secret(name: str, value: str, _: Path) -> str:
                secrets[name] = value
                return "system_keyring"

            patches = (
                patch("film_asset_pipeline.config.get_secret", side_effect=get_secret),
                patch("film_asset_pipeline.hunyuan_config.get_secret", side_effect=get_secret),
                patch("film_asset_pipeline.web.get_secret", side_effect=get_secret),
                patch("film_asset_pipeline.web.set_secret", side_effect=set_secret),
            )
            with patches[0], patches[1], patches[2], patches[3], TestClient(
                create_app(config)
            ) as client:
                initial = client.get("/api/settings/status").json()
                self.assertTrue(initial["setup_required"])
                response = client.post(
                    "/api/settings/credentials",
                    json={
                        "ark_api_key": "ark-super-secret-1234",
                        "hunyuan_api_style": "tencentcloud_sdk",
                        "tencentcloud_secret_id": "secret-id-5678",
                        "tencentcloud_secret_key": "secret-key-9012",
                    },
                )
                self.assertEqual(response.status_code, 200)
                payload = response.json()
                self.assertFalse(payload["setup_required"])
                self.assertTrue(payload["ark"]["configured"])
                self.assertTrue(payload["hunyuan"]["configured"])
                serialized = response.text
                self.assertNotIn("ark-super-secret-1234", serialized)
                self.assertNotIn("secret-id-5678", serialized)
                self.assertNotIn("secret-key-9012", serialized)
                self.assertIn("****1234", serialized)


if __name__ == "__main__":
    unittest.main()
