from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from film_asset_pipeline.credentials import delete_secret, get_secret, masked, set_secret


class CredentialTests(unittest.TestCase):
    def test_fallback_file_round_trip_and_masking(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with patch("film_asset_pipeline.credentials._keyring", return_value=None):
                with patch.dict(os.environ, {}, clear=True):
                    backend = set_secret("ARK_API_KEY", "secret-value-1234", root)
                    self.assertEqual(backend, "local_env_file")
                    self.assertEqual(get_secret("ARK_API_KEY", root), "secret-value-1234")
                    self.assertEqual(masked(get_secret("ARK_API_KEY", root)), "****1234")
                    env_text = (root / ".env").read_text(encoding="utf-8")
                    self.assertIn("ARK_API_KEY=", env_text)
                    delete_secret("ARK_API_KEY", root)
                    self.assertFalse(get_secret("ARK_API_KEY", root))
                    self.assertNotIn("ARK_API_KEY=", (root / ".env").read_text(encoding="utf-8"))

    def test_unknown_secret_name_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaises(ValueError):
                set_secret("UNSUPPORTED", "value", Path(directory))


if __name__ == "__main__":
    unittest.main()
