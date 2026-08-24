import unittest
from unittest.mock import Mock, patch

import os
import tempfile

import yaml

from src.catalog_parser import (
    MAX_PARALLEL_WORKERS,
    apply_chapter_title_overrides,
    analyze_duplicate_chapters,
    find_direct_duplicate_matches,
    generate_config_yaml,
    generate_matrix,
    normalize_chapter_title,
    normalize_chapter_name_for_comparison,
    parse_catalog,
    parse_chapter_number,
    split_chapter_title,
    format_output_chapter_title,
)


def _parsed_catalog(total=5):
    return {
        "success": True, "book_title": "測試小說", "base_url": "https://example.com",
        "chapters": [f"/read/{number}" for number in range(1, total + 1)],
        "chapter_titles": [f"第{number}章" for number in range(1, total + 1)],
        "total_chapters": total,
    }


def parsed_catalog(chapter_count):
    return {
        "success": True,
        "book_title": "測試小說",
        "total_chapters": chapter_count,
        "chapters": [f"https://example.test/chapter/{index}" for index in range(1, chapter_count + 1)],
    }


class CatalogParserMatrixTests(unittest.TestCase):
    def test_stable_uuid_title_override_survives_renumbering(self):
        parsed = _parsed_catalog(3)
        parsed["chapter_titles"] = ["第一章 甲", "地二章 錯字", "第三章 丙"]
        apply_chapter_title_overrides(parsed, {"2": "第二章 修正"})
        with tempfile.TemporaryDirectory() as temp_dir:
            config = generate_config_yaml(
                "https://example.com/catalog", 1, 3,
                os.path.join(temp_dir, "config.yaml"), exclude_chapters=[1],
                parsed_result=parsed, renumber_selected=True,
            )
        self.assertEqual(config["source_indices"], [2, 3])
        self.assertEqual(config["selected_indices"], [1, 2])
        self.assertEqual(config["chapter_title_by_index"], {
            "1": "第1章 修正",
            "2": "第2章 丙",
        })

    def test_production_title_uses_output_number_not_website_number(self):
        self.assertEqual(format_output_chapter_title(1, "序章 大荒"), "第1章 大荒")
        self.assertEqual(format_output_chapter_title(2, "第一章 朝氣蓬勃"), "第2章 朝氣蓬勃")
        self.assertEqual(format_output_chapter_title(3, "第二章 骨文"), "第3章 骨文")

    @patch("src.catalog_parser.requests.get")
    def test_catalog_preserves_repeated_links_for_user_review(self, get):
        response = Mock()
        response.content = (
            "<html><h1>測試小說</h1>"
            "<a href='/Book/Read/1,10'>第一章 開始</a>"
            "<a href='/Book/Read/1,10'>第一章 開始</a>"
            "</html>"
        ).encode("utf-8")
        response.raise_for_status.return_value = None
        get.return_value = response

        result = parse_catalog("https://example.com/Book/Chapter/1")

        self.assertEqual(result["total_chapters"], 2)
        self.assertEqual(result["chapters"], ["/Book/Read/1,10", "/Book/Read/1,10"])
        self.assertEqual(result["duplicate_indices"], [2])
        self.assertEqual(result["duplicate_chapter_count"], 1)

    def test_duplicate_analysis_defaults_to_number_or_whitespace_free_name(self):
        titles = [
            "第243章 正常章節",
            "第244章 車神老呂",
            "第244章 車神老呂:腰命啊",
            "第一百三十五章 喋血藍原谷",
            "第一百三十五章\u200b 喋血藍原谷",
        ]
        urls = [f"/Book/Read/35728,{value}" for value in range(5)]

        result = analyze_duplicate_chapters(titles, urls)

        self.assertEqual(result["duplicate_indices"], [3, 5])
        self.assertEqual(result["duplicate_chapter_count"], 2)
        self.assertEqual(result["duplicate_chapters"][0]["reasons"], [
            "normalized_chapter_number",
        ])
        self.assertEqual(result["duplicate_chapters"][1]["reasons"], [
            "normalized_chapter_number", "chapter_name_without_whitespace",
        ])

        number_only = analyze_duplicate_chapters(
            titles, urls, use_normalized_number=True, use_chapter_name=False,
        )
        self.assertEqual(number_only["duplicate_indices"], [3, 5])

    def test_chinese_and_arabic_chapter_numbers_compare_equally(self):
        self.assertEqual(parse_chapter_number("第一百三十五章 喋血藍原谷"), 135)
        self.assertEqual(parse_chapter_number("第135章 喋血藍原谷"), 135)
        self.assertEqual(parse_chapter_number("第二〇一章"), 201)
        self.assertIsNone(parse_chapter_number("番外篇 喋血藍原谷"))

    def test_unit_first_chapter_numbers(self):
        cases = {
            "章八十四 借兵": ("章八十四", "84", "借兵"),
            "章九十九 軟蛋之謎": ("章九十九", "99", "軟蛋之謎"),
            "章一百 不可控的未來": ("章一百", "100", "不可控的未來"),
            "章一百零一 新的金主": ("章一百零一", "101", "新的金主"),
            "章 101、新的金主": ("章 101", "101", "新的金主"),
        }
        for title, expected in cases.items():
            with self.subTest(title=title):
                parts = split_chapter_title(title)
                self.assertEqual(
                    (parts["display_number"], parts["normalized_number"], parts["chapter_name"]),
                    expected,
                )
                self.assertEqual(parse_chapter_number(title), int(expected[1]))

    def test_unit_first_rule_does_not_consume_ordinary_titles(self):
        for title in ("章節介紹", "章魚的故事", "章法與修行", "章一個意外"):
            with self.subTest(title=title):
                self.assertEqual(split_chapter_title(title), {
                    "display_number": "",
                    "normalized_number": "",
                    "chapter_name": title,
                })

    def test_tolerates_known_malformed_thousands_ordinal(self):
        cases = {
            "第一千七八零二章 開殺戒": ("第一千七八零二章", "1782", "開殺戒"),
            "地一千七八零二章 開殺戒": ("地一千七八零二章", "1782", "開殺戒"),
            "第一千七八零二 開殺戒": ("第一千七八零二", "1782", "開殺戒"),
            "地1782 開殺戒": ("地1782", "1782", "開殺戒"),
        }
        for title, expected in cases.items():
            with self.subTest(title=title):
                parts = split_chapter_title(title)
                self.assertEqual(
                    (parts["display_number"], parts["normalized_number"], parts["chapter_name"]),
                    expected,
                )
        self.assertEqual(split_chapter_title("地下城")["normalized_number"], "")

    def test_recovers_damaged_leading_ordinal_and_missing_unit(self):
        cases = {
            "毒695章 邪惡禮袍": ("毒695章", "695", "邪惡禮袍"),
            "都3011章 星橋彼岸": ("都3011章", "3011", "星橋彼岸"),
            "帝3172章 自我辯護": ("帝3172章", "3172", "自我辯護"),
            "第1313亮晶晶大雷暴": ("第1313", "1313", "亮晶晶大雷暴"),
        }
        for title, expected in cases.items():
            with self.subTest(title=title):
                parts = split_chapter_title(title)
                self.assertEqual(
                    (parts["display_number"], parts["normalized_number"], parts["chapter_name"]),
                    expected,
                )

    def test_normalized_override_drives_duplicates_and_production_title(self):
        parsed = _parsed_catalog(2)
        parsed["chapter_titles"] = ["第一千七八零二章 開殺戒", "第1783章 下一章"]
        apply_chapter_title_overrides(parsed, {}, {"1": 1888})
        self.assertEqual(parsed["chapter_numbers"], [1888, 1783])
        with tempfile.TemporaryDirectory() as directory:
            config = generate_config_yaml(
                "https://example.com/catalog", 1, 2,
                os.path.join(directory, "config.yaml"), parsed_result=parsed,
            )
        self.assertEqual(config["chapter_titles"][0], "第1888章 開殺戒")
        # Website normalization is for catalog identity/duplicate detection.
        # Production numbering must always use 編號章節數.
        self.assertEqual(config["chapter_title_by_index"]["1"], "第1章 開殺戒")

    def test_decimal_override_works_when_original_display_number_is_empty(self):
        parts = split_chapter_title("上古戰帝法身", "1249.50")
        self.assertEqual(parts, {
            "display_number": "",
            "normalized_number": "1249.5",
            "chapter_name": "上古戰帝法身",
        })

        parsed = _parsed_catalog(2)
        parsed["chapter_titles"] = ["上古戰帝法身", "第1249章 上古戰帝法身"]
        apply_chapter_title_overrides(parsed, {}, {"1": "1249.50", "2": "1249.5"})
        self.assertEqual(parsed["chapter_normalized_number_overrides"], {
            "1": "1249.5", "2": "1249.5",
        })
        self.assertEqual(parsed["duplicate_indices"], [2])
        self.assertEqual(parsed["chapter_numbers"], ["1249.5", "1249.5"])

    def test_invalid_decimal_overrides_are_rejected(self):
        for value in ("0", "-1", "1e3", "1.2.3", "文字"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                split_chapter_title("無章號標題", value)

    def test_chapter_identifier_normalization_does_not_touch_name_numbers(self):
        cases = {
            "第 十二 章 2026年的約定": ("第 十二 章", "12", "2026年的約定"),
            "第 １２ 三 章 2026年的約定": ("第 12 三 章", "123", "2026年的約定"),
            "番外二 特別篇": ("番外二", "番外2", "特別篇"),
            "後記一 完結": ("後記一", "後記1", "完結"),
            "第一季第一集 新開始": ("第一季第一集", "第1季第1集", "新開始"),
            "第壹佰零貳章 名稱": ("第壹佰零貳章", "102", "名稱"),
            "第141章 一千萬話費，問你怕不怕": ("第141章", "141", "一千萬話費,問你怕不怕"),
        }
        for title, expected in cases.items():
            with self.subTest(title=title):
                parts = split_chapter_title(title)
                self.assertEqual(
                    (parts["display_number"], parts["normalized_number"], parts["chapter_name"]),
                    expected,
                )

    def test_chapter_name_comparison_removes_all_whitespace(self):
        self.assertEqual(normalize_chapter_name_for_comparison("新 的\u3000開始"), "新的開始")

    def test_title_normalization_only_removes_harmless_display_differences(self):
        self.assertEqual(
            normalize_chapter_title("\u200b第１３５章　喋血藍原谷  "),
            "第135章 喋血藍原谷",
        )

    def test_three_repeated_entries_keep_only_the_first_as_original(self):
        result = analyze_duplicate_chapters([
            "第十章 原始", "第十章 修正版", "第10章 再修正版",
        ])

        self.assertEqual(result["duplicate_indices"], [2, 3])
        self.assertEqual(
            [item["reasons"] for item in result["duplicate_chapters"]],
            [["normalized_chapter_number"], ["normalized_chapter_number"]],
        )

        result = analyze_duplicate_chapters(
            ["第十章 原始", "第十章 修正版", "第10章 再修正版"],
            use_normalized_number=True, use_chapter_name=False,
        )
        self.assertEqual(result["duplicate_indices"], [2, 3])
        self.assertEqual(
            [item["original_indices"] for item in result["duplicate_chapters"]],
            [[1], [1]],
        )

    def test_duplicate_conditions_are_combined_with_or(self):
        result = analyze_duplicate_chapters([
            "第一章 甲", "第二章 乙", "第一章 乙",
        ])

        self.assertEqual(result["duplicate_indices"], [3])
        self.assertEqual(result["duplicate_chapters"][0]["reasons"], [
            "normalized_chapter_number", "chapter_name_without_whitespace",
        ])
        self.assertEqual(result["duplicate_chapters"][0]["original_indices"], [1, 2])

    def test_direct_duplicate_matches_do_not_follow_transitive_links(self):
        titles = [
            "第一章 起點",
            "第七十章 另一條線",
            "第一章 橋接名稱",
            "第七十章 橋接名稱",
        ]

        first_matches = find_direct_duplicate_matches(titles, 1)
        seventy_matches = find_direct_duplicate_matches(titles, 2)

        self.assertEqual([item["index"] for item in first_matches], [3])
        self.assertEqual(first_matches[0]["reasons"], ["normalized_chapter_number"])
        self.assertEqual([item["index"] for item in seventy_matches], [4])
        self.assertEqual(seventy_matches[0]["reasons"], ["normalized_chapter_number"])

    def test_direct_duplicate_matches_report_each_enabled_reason(self):
        titles = ["第一章 相同名稱", "第一章 相同名稱", "第二章 相同名稱"]

        matches = find_direct_duplicate_matches(
            titles, 1,
            use_normalized_number=True,
            use_chapter_name=True,
            use_number_and_name=True,
        )

        self.assertEqual(matches, [
            {
                "index": 2,
                "reasons": [
                    "normalized_chapter_number",
                    "chapter_name_without_whitespace",
                    "normalized_chapter_number_and_name_without_whitespace",
                ],
            },
            {"index": 3, "reasons": ["chapter_name_without_whitespace"]},
        ])

    def test_number_and_name_condition_requires_both_values_to_match(self):
        result = analyze_duplicate_chapters(
            ["第一章 甲", "第一章 乙", "第二章 甲", "第一章 甲"],
            use_normalized_number=False,
            use_chapter_name=False,
            use_number_and_name=True,
        )
        self.assertEqual(result["duplicate_indices"], [4])
        self.assertEqual(result["duplicate_chapters"][0]["reasons"], [
            "normalized_chapter_number_and_name_without_whitespace",
        ])

    def test_empty_duplicate_values_are_ignored(self):
        result = analyze_duplicate_chapters(["", "", "無編號標題", "另一個標題"])

        self.assertEqual(result["duplicate_indices"], [])

    def test_selected_chapters_can_be_renumbered_without_losing_source_indices(self):
        with tempfile.TemporaryDirectory() as directory:
            output = os.path.join(directory, "config.yaml")
            config = generate_config_yaml(
                "https://example.com/catalog", 1, 5, output,
                exclude_chapters=[2, 4], parsed_result=_parsed_catalog(),
                renumber_selected=True,
            )

            self.assertEqual(config["source_indices"], [1, 3, 5])
            self.assertEqual(config["selected_indices"], [1, 2, 3])
            self.assertEqual(config["chapters"], ["/read/1", "/read/3", "/read/5"])
            with open(output, encoding="utf-8") as handle:
                saved = yaml.safe_load(handle)
            self.assertEqual(saved["selected_indices"], [1, 2, 3])

            matrix, _, _ = generate_matrix(
                "https://example.com/catalog", 1, 5, 2,
                exclude_chapters=[2, 4], parsed_result=_parsed_catalog(),
                renumber_selected=True,
            )
            self.assertEqual(matrix["include"], [
                {"worker_id": 0, "book_title": "測試小說", "start_chap": 1, "end_chap": 2},
                {"worker_id": 1, "book_title": "測試小說", "start_chap": 3, "end_chap": 3},
            ])

    def test_explicit_chapter_order_is_the_actual_production_order(self):
        with tempfile.TemporaryDirectory() as directory:
            config = generate_config_yaml(
                "https://example.com/catalog", 1, 5,
                os.path.join(directory, "config.yaml"),
                exclude_chapters=[2], parsed_result=_parsed_catalog(),
                chapter_order=[3, 1, 5, 2, 4],
            )

        self.assertEqual(config["source_indices"], [3, 1, 5, 4])
        self.assertEqual(config["selected_indices"], [1, 2, 3, 4])
        self.assertEqual(config["chapters"], ["/read/3", "/read/1", "/read/5", "/read/4"])
        self.assertEqual(config["chapter_order"], [3, 1, 5, 4])

        matrix, _, _ = generate_matrix(
            "https://example.com/catalog", 1, 5, 2,
            exclude_chapters=[2], parsed_result=_parsed_catalog(),
            chapter_order=[3, 1, 5, 2, 4],
        )
        self.assertEqual(matrix["include"], [
            {"worker_id": 0, "book_title": "測試小說", "start_chap": 1, "end_chap": 2},
            {"worker_id": 1, "book_title": "測試小說", "start_chap": 3, "end_chap": 4},
        ])

    def test_large_book_creates_no_more_workers_than_can_run_in_parallel(self):
        matrix, _, chapters_per_worker = generate_matrix(
            "https://example.test/catalog",
            start_chap=1,
            end_chap=1486,
            chapters_per_worker=10,
            parsed_result=parsed_catalog(1486),
        )

        workers = matrix["include"]
        self.assertEqual(MAX_PARALLEL_WORKERS, 17)
        self.assertEqual(len(workers), 17)
        self.assertEqual(chapters_per_worker, 88)
        self.assertEqual(workers[0]["start_chap"], 1)
        self.assertEqual(workers[-1]["end_chap"], 1486)

        expected_start = 1
        for worker in workers:
            self.assertEqual(worker["start_chap"], expected_start)
            self.assertGreaterEqual(worker["end_chap"], worker["start_chap"])
            expected_start = worker["end_chap"] + 1
        self.assertEqual(expected_start, 1487)

    def test_small_book_keeps_requested_chapters_per_worker(self):
        matrix, _, chapters_per_worker = generate_matrix(
            "https://example.test/catalog",
            start_chap=1,
            end_chap=25,
            chapters_per_worker=10,
            parsed_result=parsed_catalog(25),
        )

        self.assertEqual(chapters_per_worker, 10)
        self.assertEqual(len(matrix["include"]), 3)


if __name__ == "__main__":
    unittest.main()
