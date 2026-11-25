"""Download a small chapter selection without invoking TTS or publishing.

For a site restricting HTTP access, --html-dir accepts pages saved by the user:
catalog.html plus <source_chapter_id>.html. The same production parsers are used.
"""
import argparse
import hashlib
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from src.catalog_parser import parse_catalog
from src.crawler import fetch_chapter_text
from src.cleaner import clean_text_content
from src.artifact_validation import validate_text
from src.source_identity import source_fingerprint
from src.sources import resolve_source


def download(url, output_dir, count=3, start=1, html_dir=None):
    if count < 1 or start < 1:
        raise ValueError('count and start must be positive')
    html_dir = Path(html_dir) if html_dir else None
    catalog = parse_catalog(url, html=(html_dir / 'catalog.html').read_bytes() if html_dir else None)
    adapter = resolve_source(url)
    folder = Path(output_dir) / source_fingerprint(url)
    folder.mkdir(parents=True, exist_ok=True)
    records = catalog['chapter_records'][start-1:start-1+count]
    if len(records) != count:
        raise ValueError('Requested chapter range exceeds catalog')
    manifest = {'catalog_url': url, 'source_fingerprint': source_fingerprint(url),
                'source_id': adapter.source_id, 'parser_version': adapter.version,
                'transport': 'user_saved_html' if html_dir else 'http', 'chapters': []}
    for i, chapter in enumerate(records, start):
        html = (html_dir / (chapter['source_chapter_id'] + '.html')).read_bytes() if html_dir else None
        title, body = fetch_chapter_text(chapter['url'], source_id=adapter.source_id, html=html)
        body = clean_text_content(body, title, catalog['book_title'], source_labels=adapter.opening_labels)
        target = folder / f'{i:03d}.txt'
        content = title + '\n\n' + body + '\n'
        temporary = target.with_suffix('.txt.tmp')
        temporary.write_text(content, encoding='utf-8')
        validate_text(str(temporary), clean=False)
        temporary.replace(target)
        manifest['chapters'].append({**chapter, 'file': target.name,
                                    'characters': len(body), 'sha256': hashlib.sha256(target.read_bytes()).hexdigest()})
    (folder / 'manifest.json').write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding='utf-8')
    return folder


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('url')
    parser.add_argument('--output-dir', default='Samples')
    parser.add_argument('--count', type=int, default=3)
    parser.add_argument('--start', type=int, default=1)
    parser.add_argument('--html-dir')
    args = parser.parse_args()
    print(download(args.url, args.output_dir, args.count, args.start, args.html_dir))
