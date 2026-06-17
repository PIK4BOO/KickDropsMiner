import unittest

from core.kick_public_api import _extract_data_list, livestream_to_streamer


class TestKickPublicAPIHelpers(unittest.TestCase):
    def test_extract_data_list_supports_flat_and_nested_shapes(self):
        self.assertEqual(_extract_data_list({"data": [1, 2]}), [1, 2])
        self.assertEqual(_extract_data_list({"data": {"livestreams": [3]}}), [3])
        self.assertEqual(_extract_data_list({"data": {"channels": [4]}}), [4])
        self.assertEqual(_extract_data_list({"data": None}), [])

    def test_livestream_to_streamer_maps_official_fields(self):
        stream = {
            "slug": "example",
            "stream_title": "Hello",
            "viewer_count": 42,
            "profile_picture": "https://img",
            "category": {"id": 123, "name": "Game"},
        }

        self.assertEqual(
            livestream_to_streamer(stream),
            {
                "url": "https://kick.com/example",
                "username": "example",
                "title": "Hello",
                "viewer_count": 42,
                "profile_picture": "https://img",
                "category_id": 123,
            },
        )


if __name__ == "__main__":
    unittest.main()
