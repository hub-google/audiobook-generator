import json
import tempfile
from pathlib import Path
from unittest.mock import Mock, patch

import pytest

from src.catalog_parser import parse_catalog, generate_config_yaml
from src.source_identity import source_fingerprint, workspace_name
from src.sources import resolve_source, SourceParseError, SourceAccessError
from src.sources.http_client import fetch_page
from src.huggingface_archiver import HuggingFaceArchiver
from src.youtube_upload.playlists import get_or_create_playlist
from tools.download_novel_txt import download


URL = 'https://www.69shuba.com/book/29590/'
CATALOG = '''<div id="catalog"><h1>測試小說最新章节</h1>
<a href="/txt/29590/30">3.第3章 尾聲</a>
<a href="/txt/29590/20">2.第2章 中間</a>
<a href="/txt/29590/10">1.第1章 開始</a></div>'''


def test_registry_rejects_unknown_and_host_spoofing():
    for url in ('https://example.com/read/1', 'https://www.69shuba.com.evil.test/book/1/'):
        with pytest.raises(SourceParseError):
            resolve_source(url)
    with pytest.raises(SourceParseError):
        resolve_source(URL, 'hjwzw')


def test_shuba_catalog_order_ids_and_original_labels():
    result = parse_catalog(URL, html=CATALOG)
    assert result['book_title'] == '測試小說'
    assert result['chapters'][0] == 'https://www.69shuba.com/txt/29590/10'
    assert result['chapter_titles'][0] == '第1章 開始'
    assert result['chapter_records'][0]['original_title'] == '1.第1章 開始'
    assert result['chapter_records'][0]['source_chapter_id'] == '10'


def test_hjwzw_keeps_full_url_query_and_relative_resolution():
    url = 'https://tw.hjwzw.com/Book/Chapter/1'
    result = parse_catalog(url, html='<h1>書</h1><a href="/Book/Read/1,2?edition=a">第二章</a>')
    assert result['chapters'] == ['https://tw.hjwzw.com/Book/Read/1,2?edition=a']


@pytest.mark.parametrize('html', ['<h1>新版網站</h1>', '<title>Just a moment...</title>'])
def test_bad_markup_is_not_missing_chapter(html):
    with pytest.raises((SourceParseError, SourceAccessError)):
        resolve_source(URL).parse_chapter(html, URL)


def test_chapter_removes_ui_but_keeps_novel_paragraphs():
    title, body = resolve_source(URL).parse_chapter('''<div class="txtnav"><h1>1405.第1395章 旅程</h1>
    <div class="txtinfo">日期 作者</div><script>advert()</script>
    第一段正文。<br>第二段正文。<div class="contentadv">廣告</div><br>(本章完)</div>''', URL)
    assert title == '第1395章 旅程'
    assert body == '第一段正文。\n第二段正文。'


def test_shuba_metadata_does_not_point_to_hjwzw():
    from src.metadata_gen import _catalog_book_url
    assert _catalog_book_url(URL) == 'https://www.69shuba.com/book/29590.htm'


def test_same_title_different_urls_are_isolated(tmp_path):
    configs = []
    for url in (URL, URL+'?edition=2', URL.rstrip('/')):
        result = parse_catalog(url, html=CATALOG)
        configs.append(generate_config_yaml(url, 1, 2, str(tmp_path/'config.yaml'), parsed_result=result))
    assert len({c['source_fingerprint'] for c in configs}) == 3
    assert len({c['book_profile_id'] for c in configs}) == 3
    assert len({workspace_name(c) for c in configs}) == 3
    assert all(c['chapter_records'][0]['source_chapter_id'] == '10' for c in configs)
    configs[0]['catalog_url'] = URL+'?changed=1'
    with pytest.raises(ValueError):
        workspace_name(configs[0])


def test_legacy_workspace_stays_accessible():
    assert workspace_name({'book_title': 'old book'}) == 'old book'


def test_http_restriction_has_explicit_error():
    response = Mock(status_code=403)
    with patch('src.sources.http_client.requests.get', return_value=response), patch('src.sources.http_client.time.sleep'):
        with pytest.raises(SourceAccessError):
            fetch_page(URL, resolve_source(URL))


def test_archive_keys_are_source_specific():
    archive = HuggingFaceArchiver.__new__(HuggingFaceArchiver)
    archive.project = 'project'
    archive.source_fingerprint = source_fingerprint(URL)
    first = archive._book_root('same title')
    archive.source_fingerprint = source_fingerprint(URL+'?other=1')
    assert archive._book_root('same title') != first


def test_playlist_does_not_reuse_same_title_from_other_source():
    youtube = Mock()
    youtube.playlists().list().execute.return_value = {'items': [{'id':'old','snippet':{'title':'same','description':'other source'}}]}
    youtube.playlists().insert().execute.return_value = {'id':'new'}
    assert get_or_create_playlist(youtube, 'same', 'description', source_fingerprint='abc') == ('new', True)


def test_three_txt_exports_use_production_parser(tmp_path):
    saved = tmp_path/'html'; saved.mkdir()
    (saved/'catalog.html').write_text(CATALOG, encoding='utf-8')
    for number, cid in enumerate(('10','20','30'), 1):
        (saved/(cid+'.html')).write_text(f'<div class="txtnav"><h1>{number}.第{number}章 測試</h1><div class="txtinfo">作者</div>這是測試用的小說正文，應保留足夠文字供驗證，不能包含廣告及作者欄位。<br>下一段正文。</div>', encoding='utf-8')
    folder = download(URL, tmp_path/'out', html_dir=saved)
    assert len(list(folder.glob('*.txt'))) == 3
    manifest = json.loads((folder/'manifest.json').read_text(encoding='utf-8'))
    assert manifest['transport'] == 'user_saved_html'
    assert all('作者' not in f.read_text(encoding='utf-8').split('\n\n')[0] for f in folder.glob('*.txt'))


def test_catalog_url_normalization():
    shuba = resolve_source('https://www.69shuba.com/book/29590')
    assert shuba.catalog_url('https://www.69shuba.com/book/29590') == 'https://www.69shuba.com/book/29590/'
    assert shuba.catalog_url('https://www.69shuba.com/book/29590/') == 'https://www.69shuba.com/book/29590/'
    assert shuba.catalog_url('https://www.69shuba.com/book/29590.htm') == 'https://www.69shuba.com/book/29590/'
    assert shuba.catalog_url('https://www.69shuba.com/txt/29590/10') == 'https://www.69shuba.com/book/29590/'
    assert shuba.catalog_url('https://www.69shuba.com/book/29590?edition=2') == 'https://www.69shuba.com/book/29590/?edition=2'

    hjwzw = resolve_source('https://tw.hjwzw.com/Book/35120')
    assert hjwzw.catalog_url('https://tw.hjwzw.com/Book/35120') == 'https://tw.hjwzw.com/Book/Chapter/35120'
    assert hjwzw.catalog_url('https://tw.hjwzw.com/Book/Chapter/35120') == 'https://tw.hjwzw.com/Book/Chapter/35120'


def test_parse_catalog_accepts_url_without_trailing_slash():
    result = parse_catalog('https://www.69shuba.com/book/29590', html=CATALOG)
    assert result['catalog_url'] == 'https://www.69shuba.com/book/29590/'
    assert result['total_chapters'] == 3


def test_http_404_raises_source_parse_error():
    response = Mock(status_code=404)
    with patch('src.sources.http_client.requests.get', return_value=response), patch('src.sources.http_client.time.sleep'):
        with pytest.raises(SourceParseError, match='HTTP 404'):
            fetch_page(URL, resolve_source(URL))

