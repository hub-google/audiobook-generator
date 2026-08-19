import unittest

import os
import tempfile

import yaml

from src.catalog_parser import MAX_PARALLEL_WORKERS, generate_config_yaml, generate_matrix


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
