from __future__ import annotations

import argparse
import hashlib
import json
import re
import unicodedata
from pathlib import Path

CHAPTER_RE = re.compile(r"^第0*(\d+)章")
TITLE_RE = re.compile(r"^第\s*0*\d+\s*章[_\s]*(?:第\s*\d+\s*章\s*)?(.*?)\.txt$", re.I)
SEPARATOR_RE = re.compile(r"^[=＊*—\-]{8,}$")
AD_PATTERNS = (re.compile(r"請記住本站域名"), re.compile(r"^黃金屋$"),
               re.compile(r"^(新書公眾版|公眾期間呢|推薦票什么的|書評區大家).*$"))


def chapter_number(path: Path) -> int:
    match = CHAPTER_RE.match(path.name)
    if not match:
        raise ValueError(f"無法從檔名取得章號：{path.name}")
    return int(match.group(1))


def read_text(path: Path) -> tuple[str, str, str]:
    raw = path.read_bytes()
    for encoding in ("utf-8-sig", "utf-8", "gb18030"):
        try:
            return raw.decode(encoding), encoding, hashlib.sha256(raw).hexdigest()
        except UnicodeDecodeError:
            pass
    raise UnicodeDecodeError("unknown", raw, 0, 1, f"無法判斷編碼：{path}")


def title_from_path(path: Path) -> str:
    match = TITLE_RE.match(path.name)
    return (match.group(1).strip(" _") if match else path.stem).strip()


def normalize(text: str) -> str:
    text = unicodedata.normalize("NFC", text.replace("\r\n", "\n").replace("\r", "\n"))
    lines: list[str] = []
    for raw_line in text.split("\n"):
        line = re.sub(r"[ \t]+", " ", raw_line).strip()
        if not line or SEPARATOR_RE.match(line) or any(p.search(line) for p in AD_PATTERNS):
            continue
        if not lines and re.match(r"^第\s*\d+\s*章", line):
            continue
        if not lines and re.match(r"^第[一二三四五六七八九十百千]+章", line):
            continue
        lines.append(line)
    return "\n".join(lines)


def effective_chars(text: str) -> int:
    return len(re.sub(r"\s+", "", text))


def make_chunks(text: str, chapter: int, target: int = 600) -> list[dict]:
    paragraphs = [p.strip() for p in text.split("\n") if p.strip()]
    chunks, buf = [], []
    cursor = buf_start = end = 0
    for paragraph in paragraphs:
        pos = text.find(paragraph, cursor)
        if not buf:
            buf_start = pos
        buf.append(paragraph)
        end, cursor = pos + len(paragraph), pos + len(paragraph)
        if sum(map(len, buf)) >= target:
            chunks.append({"start_char": buf_start, "end_char": end, "text": "\n".join(buf)})
            buf = []
    if buf:
        chunks.append({"start_char": buf_start, "end_char": end, "text": "\n".join(buf)})
    for i, chunk in enumerate(chunks, 1):
        chunk.update(chunk_id=f"ch{chapter:04d}_p{i:03d}",
                     previous_chunk_id=f"ch{chapter:04d}_p{i-1:03d}" if i > 1 else None,
                     next_chunk_id=f"ch{chapter:04d}_p{i+1:03d}" if i < len(chunks) else None)
    return chunks


def discover(source: Path) -> list[Path]:
    files = sorted(source.glob("*.txt"), key=chapter_number)
    numbers = [chapter_number(p) for p in files]
    duplicates = sorted({n for n in numbers if numbers.count(n) > 1})
    if duplicates:
        raise ValueError(f"章號重複：{duplicates}")
    return files


def build_batch(source: Path, output: Path, start: int, count: int, min_chars: int = 600) -> int:
    selected = [p for p in discover(source) if chapter_number(p) >= start][:count]
    if not selected:
        raise SystemExit(f"找不到第 {start} 章起的章節")
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("w", encoding="utf-8", newline="\n") as handle:
        for path in selected:
            number = chapter_number(path)
            decoded, encoding, source_sha = read_text(path)
            body = normalize(decoded)
            chars = effective_chars(body)
            record = {"book_id": "quanzhigaoshou", "chapter": number, "title": title_from_path(path),
                      "source_file": path.name, "source_encoding": encoding, "source_sha256": source_sha,
                      "normalized_sha256": hashlib.sha256(body.encode()).hexdigest(), "effective_chars": chars,
                      "status": "ready_for_analysis" if chars >= min_chars else "skipped_short",
                      "skip_reason": None if chars >= min_chars else f"effective_chars<{min_chars}",
                      "text": body, "chunks": make_chunks(body, number)}
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")
    return len(selected)


def main() -> None:
    parser = argparse.ArgumentParser(description="準備 Kaggle 章節批次，不修改來源檔")
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--output", type=Path, default=Path("batch/chapters.jsonl"))
    parser.add_argument("--start", type=int, default=1)
    parser.add_argument("--count", type=int, default=20)
    parser.add_argument("--min-chars", type=int, default=600)
    args = parser.parse_args()
    print(f"已準備 {build_batch(args.source, args.output, args.start, args.count, args.min_chars)} 章：{args.output.resolve()}")


if __name__ == "__main__":
    main()
