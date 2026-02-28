import unittest

from scripts.check_open_box import _extract_next_data_json, _parse_non_negative_int, has_open_box_stock


class OpenBoxDetectionTests(unittest.TestCase):
    def test_detects_open_box_text_signal(self):
        html = "<html><body><div>See Open-Box options now</div></body></html>"
        self.assertTrue(has_open_box_stock(html))

    def test_suppresses_known_negative_text(self):
        html = "<html><body><div>No Open-Box options</div></body></html>"
        self.assertFalse(has_open_box_stock(html))

    def test_detects_open_box_from_next_data_json(self):
        html = (
            '<script id="__NEXT_DATA__" type="application/json">'
            '{"props":{"pageProps":{"buyingOptions":{"openBox":{"available":true}}}}}'
            "</script>"
        )
        self.assertTrue(has_open_box_stock(html))

    def test_parse_non_negative_int_handles_blank(self):
        self.assertEqual(_parse_non_negative_int(""), 0)
        self.assertEqual(_parse_non_negative_int("   "), 0)

    def test_parse_non_negative_int_handles_invalid_and_negative(self):
        self.assertEqual(_parse_non_negative_int("abc"), 0)
        self.assertEqual(_parse_non_negative_int("-5"), 0)

    def test_parse_non_negative_int_handles_valid(self):
        self.assertEqual(_parse_non_negative_int("15"), 15)

    def test_extract_next_data_json(self):
        html = '<script id="__NEXT_DATA__" type="application/json">{"foo":1}</script>'
        self.assertEqual(_extract_next_data_json(html), {"foo": 1})


if __name__ == "__main__":
    unittest.main()
