"""Tests for gemini-web plugin."""
import json
import pathlib
import sys
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
import gemini_web_plugin as gwp


class TestLoadAccounts(unittest.TestCase):
    """Test cookie loading."""

    def test_load_valid(self):
        with patch.object(gwp, "_COOKIES_PATH") as cp:
            cp.exists.return_value = True
            cp.read_text.return_value = json.dumps([{
                "label": "test",
                "__Secure-1PSID": "fakepsid",
                "__Secure-1PSIDTS": "fakepsidts",
            }])
            accts = gwp._load_accounts()
            self.assertEqual(len(accts), 1)
            self.assertEqual(accts[0]["label"], "test")
            self.assertEqual(accts[0]["psid"], "fakepsid")

    def test_load_missing_psid(self):
        with patch.object(gwp, "_COOKIES_PATH") as cp:
            cp.exists.return_value = True
            cp.read_text.return_value = json.dumps([{"label": "x"}])
            with self.assertRaises(ValueError):
                gwp._load_accounts()

    def test_load_missing_file(self):
        with patch.object(gwp, "_COOKIES_PATH") as cp:
            cp.exists.return_value = False
            with self.assertRaises(FileNotFoundError):
                gwp._load_accounts()


class TestRoundRobin(unittest.TestCase):
    """Test account rotation and cooldown."""

    def setUp(self):
        gwp._rr_index = 0
        gwp._cooldowns.clear()

    def test_rotation(self):
        accts = [
            {"label": "a", "psid": "1", "psidts": ""},
            {"label": "b", "psid": "2", "psidts": ""},
        ]
        a1 = gwp._next_account(accts)
        a2 = gwp._next_account(accts)
        self.assertEqual(a1["label"], "a")
        self.assertEqual(a2["label"], "b")

    def test_cooldown_skip(self):
        accts = [
            {"label": "a", "psid": "1", "psidts": ""},
            {"label": "b", "psid": "2", "psidts": ""},
        ]
        gwp._mark_cooldown("a")
        picked = gwp._next_account(accts)
        self.assertEqual(picked["label"], "b")

    def test_all_on_cooldown(self):
        accts = [{"label": "a", "psid": "1", "psidts": ""}]
        gwp._mark_cooldown("a")
        self.assertIsNone(gwp._next_account(accts))


class TestHandlers(unittest.TestCase):
    """Test handler wrappers."""

    @patch("gemini_web_plugin.gemini_web_generate")
    def test_h_generate(self, mock_gen):
        mock_gen.return_value = {"success": True, "images": ["/tmp/x.png"]}
        result = json.loads(gwp._h_generate({"prompt": "test"}))
        self.assertTrue(result["success"])
        mock_gen.assert_called_once_with(prompt="test", aspect_ratio="square")

    @patch("gemini_web_plugin.gemini_web_edit")
    def test_h_edit(self, mock_edit):
        mock_edit.return_value = {"success": True, "images": ["/tmp/y.png"]}
        result = json.loads(gwp._h_edit({"prompt": "test", "reference_path": "/tmp/ref.jpg"}))
        self.assertTrue(result["success"])
        mock_edit.assert_called_once_with(prompt="test", reference_path="/tmp/ref.jpg")

    @patch("gemini_web_plugin.gemini_web_generate", side_effect=Exception("boom"))
    def test_h_generate_exception(self, mock_gen):
        result = json.loads(gwp._h_generate({"prompt": "test"}))
        self.assertIn("error", result)
        self.assertEqual(result["type"], "Exception")


class TestSchemas(unittest.TestCase):
    """Validate tool schemas."""

    def test_schemas_exist(self):
        self.assertIn("gemini_web_generate", gwp.SCHEMAS)
        self.assertIn("gemini_web_edit", gwp.SCHEMAS)

    def test_generate_required(self):
        s = gwp.SCHEMAS["gemini_web_generate"]
        self.assertEqual(s["parameters"]["required"], ["prompt"])

    def test_edit_required(self):
        s = gwp.SCHEMAS["gemini_web_edit"]
        self.assertIn("prompt", s["parameters"]["required"])
        self.assertIn("reference_path", s["parameters"]["required"])


class TestCookieRotation(unittest.TestCase):
    """Test cookie rotation and persistence."""

    def test_update_persisted_ts(self):
        import tempfile
        with tempfile.NamedTemporaryFile("w+", delete=False) as f:
            f.write(json.dumps([{"label": "test", "__Secure-1PSID": "fake_psid", "__Secure-1PSIDTS": "old_ts"}]))
            f.flush()
            temp_path = pathlib.Path(f.name)

        try:
            with patch.object(gwp, "_COOKIES_PATH", temp_path):
                ok = gwp._update_persisted_ts("test", "fake_psid", "new_ts")
                self.assertTrue(ok)
                data = json.loads(temp_path.read_text())
                self.assertEqual(data[0]["__Secure-1PSIDTS"], "new_ts")
        finally:
            if temp_path.exists():
                temp_path.unlink()


if __name__ == "__main__":
    unittest.main()
