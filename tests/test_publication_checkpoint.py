import os
import tempfile
import unittest

from src.publication_checkpoint import PART_STEPS, PublicationCheckpoint


class PublicationCheckpointTests(unittest.TestCase):
    def _plan(self, split=2):
        return [
            {"part_num": 1, "start_chap": 1, "end_chap": split,
             "chapters": list(range(1, split + 1)), "duration": 100.0, "title": "Part 1"},
            {"part_num": 2, "start_chap": split + 1, "end_chap": 4,
             "chapters": list(range(split + 1, 5)), "duration": 100.0, "title": "Part 2"},
        ]

    def test_locked_plan_is_reused_and_drift_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            state = os.path.join(directory, "state.json")
            checkpoint = PublicationCheckpoint(state)
            checkpoint.lock_plan(self._plan(), run_id="1", book_title="Book")

            restored = PublicationCheckpoint(state)
            restored.lock_plan(self._plan(), run_id="1", book_title="Book")
            with self.assertRaisesRegex(RuntimeError, "refusing to repartition"):
                restored.lock_plan(self._plan(split=3), run_id="1", book_title="Book")

    def test_part_resume_point_advances_one_step_at_a_time(self):
        with tempfile.TemporaryDirectory() as directory:
            checkpoint = PublicationCheckpoint(os.path.join(directory, "state.json"))
            checkpoint.lock_plan(self._plan(), run_id="1", book_title="Book")
            checkpoint.complete(1, PART_STEPS[0])
            checkpoint.mark(1, PART_STEPS[1], "running")
            checkpoint.fail(1, PART_STEPS[1], RuntimeError("subtitle failed"))

            part = checkpoint.data["parts"]["1"]
            self.assertEqual(part["resume_from"], PART_STEPS[1])
            self.assertEqual(part["steps"][PART_STEPS[1]]["attempts"], 1)
            self.assertIn("subtitle failed", checkpoint.markdown_summary())


if __name__ == "__main__":
    unittest.main()
