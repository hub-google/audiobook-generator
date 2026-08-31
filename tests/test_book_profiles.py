import hashlib
import json
import unittest

from src.book_profiles import (
    book_profile_id, empty_profiles, get_book_profile, normalize_catalog_url,
    profile_snapshot, update_book_profile, validate_remove_patterns,
)


class BookProfileTests(unittest.TestCase):
    def test_normalized_number_overrides_persist_without_changing_cleaner_rules(self):
        data = update_book_profile(
            empty_profiles(), "https://example.com/book/1", "A",
            cleaner_remove_patterns=["廣告"],
            chapter_normalized_number_overrides={"1698": "1782.50"},
        )
        _, profile = get_book_profile(data, "https://example.com/book/1")
        self.assertEqual(profile["cleaner_remove_patterns"], ["廣告"])
        self.assertEqual(profile["chapter_normalized_number_overrides"], {"1698": "1782.5"})
        snapshot = profile_snapshot("book-id", profile)
        self.assertEqual(snapshot["chapter_normalized_number_overrides"], {"1698": "1782.5"})

    def test_normalized_url_has_stable_book_id(self):
        left = "HTTPS://TW.HJWZW.COM/Book/Chapter/1644/"
        right = "https://tw.hjwzw.com/Book/Chapter/1644"
        self.assertEqual(normalize_catalog_url(left), normalize_catalog_url(right))
        self.assertEqual(book_profile_id(left), book_profile_id(right))

    def test_books_keep_independent_cleaner_rules(self):
        data = update_book_profile(empty_profiles(), "https://example.com/book/1", "A", cleaner_remove_patterns=["ad.com"])
        data = update_book_profile(data, "https://example.com/book/2", "B", cleaner_remove_patterns=["另一廣告"])
        _, first = get_book_profile(data, "https://example.com/book/1")
        _, second = get_book_profile(data, "https://example.com/book/2")
        self.assertEqual(first["cleaner_remove_patterns"], ["ad.com"])
        self.assertEqual(second["cleaner_remove_patterns"], ["另一廣告"])

    def test_literal_texts_are_validated_and_deduplicated(self):
        self.assertEqual(validate_remove_patterns(["廣告", "廣告", ""]), ["廣告"])
        self.assertEqual(validate_remove_patterns(["(", ".*", "[公告]?"]), ["(", ".*", "[公告]?"])
        self.assertEqual(validate_remove_patterns(["第一行\r\n第二行"]), ["第一行\n第二行"])

    def test_literal_text_length_limit_is_ten_thousand_characters(self):
        self.assertEqual(validate_remove_patterns(["文" * 10_000]), ["文" * 10_000])
        with self.assertRaisesRegex(ValueError, "10000"):
            validate_remove_patterns(["文" * 10_001])

    def test_snapshot_fingerprint_changes_only_with_cleaner_rules(self):
        profile = {
            "profile_revision": 1, "cleaner_remove_patterns": ["廣告"],
            "duplicate_detection": {"use_normalized_number": True, "use_chapter_name": True, "use_number_and_name": False},
            "chapter_title_overrides": {},
        }
        first = profile_snapshot("id", profile)
        profile["duplicate_detection"]["use_chapter_name"] = False
        second = profile_snapshot("id", profile)
        self.assertEqual(first["cleaner_fingerprint"], second["cleaner_fingerprint"])
        profile["cleaner_remove_patterns"] = ["另一廣告"]
        third = profile_snapshot("id", profile)
        self.assertNotEqual(first["cleaner_fingerprint"], third["cleaner_fingerprint"])

    def test_snapshot_uses_prosody_cleaner_fingerprint_version(self):
        profile = {"profile_revision": 1, "cleaner_remove_patterns": [".*"]}
        snapshot = profile_snapshot("id", profile)
        canonical = json.dumps([".*"], ensure_ascii=False, separators=(",", ":"))
        expected = hashlib.sha256(("cleaner-v4-prosody|" + canonical).encode("utf-8")).hexdigest()
        self.assertEqual(snapshot["cleaner_fingerprint"], expected)


if __name__ == "__main__":
    unittest.main()
