from .hjwzw import HJWZWSource
from .shuba69 import Shuba69Source
from .czbooks import CZBooksSource
from .sixk import SixKSource
from .xszj import XszjSource
from .metanovel import MetaNovelSource
from .twkan import TwkanSource
from .base import SourceParseError

SOURCES = {source.source_id: source for source in (HJWZWSource(), Shuba69Source(), CZBooksSource(), SixKSource(), XszjSource(), MetaNovelSource(), TwkanSource())}


def resolve_source(url, source_id=None):
    for source in SOURCES.values():
        if source.matches(url):
            if source_id and source.source_id != source_id:
                raise SourceParseError('設定的來源與網址不一致')
            return source
    raise SourceParseError(f'尚未支援此小說來源：{url}')
