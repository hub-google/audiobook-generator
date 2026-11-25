"""Decode the AJAX URL built by the site's public reader script.

Only a narrow grammar is accepted. No JavaScript eval and no access to
the third-party sports/finance redirect hosts. The resulting URL is still
validated by the source adapter's approved 8book host list.
"""
import re
from urllib.parse import unquote, urlsplit
from .base import SourceParseError


def resolve_reader_ajax(source, html, reader_url):
    soup = source.soup(html)
    script = "\n".join(tag.get_text() for tag in soup.select("script:not([src])"))
    cid = urlsplit(reader_url).query.split("_", 1)[0]
    if not re.fullmatch(r"\d+", cid):
        raise SourceParseError("無限小說閱讀章節 ID 格式不正確")
    book_id = source.book_id(reader_url)

    arrays = re.search(
        r"(?P<array>[A-Za-z_]\w*)\[\s*(?P=array)\.length\s*-\s*(?P<minus>\w+)\s*\]"
        r"\.substr\(\s*(?P<chapter>\w+)\s*\*\s*(?P<multiplier>\w+)\s*%\s*"
        r"(?P<modulus>\w+)\s*,\s*(?P<length>\w+)\s*\)", script,
    )
    if not arrays:
        raise SourceParseError("無限小說無法辨識官方 AJAX 章節簽章規則")
    m = re.search(
        r"\bvar\s+" + re.escape(arrays["array"]) +
        r"""\s*=\s*["']([\d,]+)["']\.split\(\s*["'],["']\s*\)""", script
    )
    if not m:
        raise SourceParseError("無限小說缺少章節 ID 與簽章列表")
    values = m[1].split(",")
    if len(values) < 3 or len(values[-1]) < 80 or cid not in values[:-1]:
        raise SourceParseError("無限小說章節不在官方閱讀目錄")
    index = values[:-1].index(cid)
    intvars = {name: int(value) for name, value in
               re.findall(r"\bvar\s+([A-Za-z_]\w*)\s*=\s*(\d+)\s*;", script)}
    try:
        multiplier = intvars[arrays["multiplier"]]
        modulus = intvars[arrays["modulus"]]
        length = intvars[arrays["length"]]
        minus = intvars[arrays["minus"]]
        if not (1 <= multiplier <= 100 and 20 <= modulus <= 10000 and 1 <= length <= 20 and minus == 1):
            raise ValueError
    except (KeyError, ValueError, ZeroDivisionError) as exc:
        raise SourceParseError("無限小說閱讀章節簽章變數有誤") from exc
    begin = int(cid) * multiplier % modulus
    salt = values[-1][begin:begin + length]
    if len(salt) != length:
        raise SourceParseError("無限小說閱讀簽章不足")

    prefix = re.search(
        r"""["']%2f%["']\s*\+\s*(?P<k>\w+)\s*\+\s*["']4%78%74%2f(?P<folder>\d+)%["']"""
        r"""\s*\+\s*(?P<h>\w+)\s*\+\s*["']f(?P<bid>\d+)%2f["']""",
        script, re.I,
    )
    if not prefix or prefix["bid"] != book_id or prefix["k"] not in intvars or prefix["h"] not in intvars:
        raise SourceParseError("無限小說官方 AJAX 正文路徑格式不符")
    enc = "%2f%" + str(intvars[prefix["k"]]) + "4%78%74%2f" + prefix["folder"] + "%"
    enc += str(intvars[prefix["h"]]) + "f" + book_id + "%2f"
    path = unquote(enc)
    if not re.fullmatch(rf"/txt/\d+/{re.escape(book_id)}/", path):
        raise SourceParseError("無限小說 AJAX 正文目錄不符")

    title_arrays = re.findall(
        r"""\bvar\s+[A-Za-z_]\w*\s*=\s*["']([^"']{20,})["']\.split\(\s*["'],["']\s*\)""",
        script
    )
    candidates = [val.split(",") for val in title_arrays if "第" in val]
    matching = [x for x in candidates if len(x) == len(values)-1]
    if not matching or index >= len(matching[0]):
        raise SourceParseError("無限小說缺少可核對的章節標題列表")
    title = matching[0][index].strip()
    if not title or len(title) >= 150:
        raise SourceParseError("無限小說章節標題異常")
    url = source.absolute_url(reader_url, "https://8book.com" + path + cid + salt + ".html")
    return title, url
