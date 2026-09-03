"""Stable, capped Part-to-merge-worker assignment helpers."""

MAX_MERGE_WORKERS = 17


def build_merge_matrix(part_numbers, max_workers=MAX_MERGE_WORKERS):
    numbers = [int(number) for number in part_numbers]
    if not numbers:
        return {"include": []}
    worker_count = min(int(max_workers), MAX_MERGE_WORKERS, len(numbers))
    assignments = [[] for _ in range(worker_count)]
    for index, number in enumerate(numbers):
        assignments[index % worker_count].append(number)
    return {"include": [
        {"merge_worker_id": index, "part_numbers": ",".join(map(str, assigned))}
        for index, assigned in enumerate(assignments)
    ]}
