import os
import tempfile
import unittest

from utils.json_utils import json_load, json_save_atomic


class TestJsonUtils(unittest.TestCase):
    def test_load_merges_defaults_and_drops_unknown_keys(self):
        defaults = {
            "items": [],
            "debug": False,
            "nested": {"enabled": True, "count": 1},
        }

        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "settings.json")
            json_save_atomic(
                path,
                {
                    "items": [{"url": "https://kick.com/example"}],
                    "debug": True,
                    "unknown": "removed",
                    "nested": {"enabled": False, "extra": "removed"},
                },
            )

            loaded = json_load(path, defaults, merge=True)

        self.assertEqual(loaded["items"], [{"url": "https://kick.com/example"}])
        self.assertTrue(loaded["debug"])
        self.assertNotIn("unknown", loaded)
        self.assertEqual(loaded["nested"], {"enabled": False, "count": 1})

    def test_load_recovers_new_file(self):
        defaults = {"debug": False}

        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "settings.json")
            json_save_atomic(path, {"debug": False})
            with open(f"{path}.new", "w", encoding="utf-8") as f:
                f.write('{"debug": true}')

            loaded = json_load(path, defaults, merge=True)

        self.assertTrue(loaded["debug"])


if __name__ == "__main__":
    unittest.main()
