"""Nine-site GUI source selection and strict, site-specific URL normalization.

All parsing rules remain in src/sources; this layer does not guess page paths,
trust a user-entered host suffix, or fetch the network on the Tk main thread.
"""
from urllib.parse import urlsplit

try:
    from src.sources.registry import SOURCES, resolve_source
    from src.sources.base import SourceParseError
except ImportError:
    from sources.registry import SOURCES, resolve_source
    from sources.base import SourceParseError

AUTO_SOURCE_LABEL = "自動辨識網站"
SOURCE_NAMES = {
    "hjwzw": "黃金屋",
    "shuba69": "69 書吧",
    "czbooks": "小說狂人",
    "twkan": "台灣小說網",
    "sixk": "6K 小說網",
    "xszj": "小說之家",
    "metanovel": "元小說",
    "moon17": "月亮小說網",
    "book8": "無限小說",
}
# All examples have been used in the source-specific live validation.
SOURCE_EXAMPLES = {
    "hjwzw": "https://tw.hjwzw.com/Book/1889",
    "shuba69": "https://69shuba.com/book/53782/",
    "czbooks": "https://czbooks.net/n/c37492",
    "twkan": "https://www.twkan.cc/book/5xqpGOcl46z9RzbASUJWgx.html",
    "sixk": "https://www.6ksu.app/novel/5050292.html",
    "xszj": "https://xszj.tw/book/491274",
    "metanovel": "https://m.metanovel.org/book/480384",
    "moon17": "https://www.17moon.com/book/8Kk/",
    "book8": "https://8book.com/novelbooks/168909/",
}
SOURCE_NOTES = {
    "hjwzw": "直接讀取完整目錄",
    "shuba69": "自動切換可讀的官方網址",
    "czbooks": "閱讀章節網址可轉目錄",
    "twkan": "自動合併分頁；來源缺章會報錯",
    "sixk": "暫時連線逾時自動重試",
    "xszj": "自動合併多頁目錄",
    "metanovel": "自動合併多頁目錄與正文",
    "moon17": "自動還原閱讀頁文字與分頁",
    "book8": "由本站閱讀器取得 AJAX 正文",
}
SOURCE_OPTIONS = (AUTO_SOURCE_LABEL,) + tuple(SOURCE_NAMES[key] for key in SOURCE_NAMES)


def source_label(source_id):
    return SOURCE_NAMES.get(str(source_id), AUTO_SOURCE_LABEL)


def source_id_from_label(label):
    for source_id, name in SOURCE_NAMES.items():
        if name == label:
            return source_id
    return None


def detect_source(raw_url):
    """Return the registered adapter for an approved host, or None."""
    try:
        return resolve_source(str(raw_url or "").strip())
    except (SourceParseError, ValueError):
        return None


def normalize_source_url(raw_url):
    """Return (source, canonical catalog URL), never a guessed URL."""
    raw_url = str(raw_url or "").strip()
    if not raw_url:
        raise SourceParseError("請貼上小說書籍、目錄或章節網址")
    try:
        source = resolve_source(raw_url)
        canonical = source.catalog_url(raw_url)
        source.absolute_url(raw_url, canonical)
        source.book_id(canonical)
        return source, canonical
    except ValueError as exc:
        raise SourceParseError(f"無法辨識此小說網址：{raw_url}") from exc


def source_hint(source):
    label = source_label(source.source_id)
    note = SOURCE_NOTES.get(source.source_id, "自動解析來源目錄")
    return f"{label}｜{note}｜同站最多 {source.max_parallel} 個抓文工作"


def catalog_matches_current(raw_url, catalog_data):
    """Do not queue chapters from a stale book after the URL is edited."""
    if not isinstance(catalog_data, dict) or not catalog_data.get("success"):
        return False
    try:
        source, canonical = normalize_source_url(raw_url)
    except (SourceParseError, ValueError):
        return False
    return (canonical == catalog_data.get("catalog_url") and
            source.source_id == catalog_data.get("source_id"))


def normalize_batch_urls(raw_urls):
    """Normalize and deduplicate approved books in user order.

    The same site's alternate hostname, chapter links or pages are one book.
    A query remains part of the identity so editions are never merged.
    """
    normalized = []
    seen = set()
    for raw in raw_urls:
        source, canonical = normalize_source_url(raw)
        identity = (source.source_id, source.book_id(canonical), urlsplit(canonical).query)
        if identity not in seen:
            seen.add(identity)
            normalized.append(canonical)
    return normalized


assert set(SOURCE_NAMES) == set(SOURCES), "GUI source options must match production adapters"
assert set(SOURCE_EXAMPLES) == set(SOURCES), "Missing GUI source example"
