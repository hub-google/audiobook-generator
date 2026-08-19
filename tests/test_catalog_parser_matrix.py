import unittest

from src.catalog_parser import MAX_PARALLEL_WORKERS, generate_matrix


def parsed_catalog(chapter_count):
    return {
        "success": True,
        "book_title": "測試小說",
        "total_chapters": chapter_count,
        "chapters": [f"https://example.test/chapter/{index}" for index in range(1, chapter_count + 1)],
    }


class CatalogParserMatrixTests(unittest.TestCase):
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
