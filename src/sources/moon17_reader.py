"""Reconstruct the site's ordinary public reader text from its DOM.

Matches the published reader.js transformation: split text nodes into
Unicode code points, keep element nodes intact, reverse units. The public
icon font contributes a single visible Chinese character per <i> element.
"""
import re
from bs4.element import NavigableString, Tag
from .base import SourceParseError
from .moon17_glyphs import ICON_GLYPHS


def _icon_character(node):
    classes = node.get("class", ())
    ids = [re.fullmatch(r"icon-(\d+)", cls) for cls in classes]
    numbers = [int(x.group(1)) for x in ids if x]
    if len(numbers) != 1 or numbers[0] not in ICON_GLYPHS:
        raise SourceParseError("月亮小說網正文含未知字型字元，不能猜字")
    return ICON_GLYPHS[numbers[0]]


def render_article(article):
    for paragraph in article.select("p"):
        reversed_paragraph = "js-reversed-text" in paragraph.get("class", ())
        units = []
        for child in list(paragraph.contents):
            if isinstance(child, NavigableString):
                units.extend(str(child))
            elif isinstance(child, Tag):
                if child.name == "i" and any(c.startswith("icon-") for c in child.get("class", ())):
                    units.append(_icon_character(child))
                elif child.select_one("i[class^='icon-'], i[class*=' icon-']"):
                    raise SourceParseError("月亮小說網正文含巢狀未知字元")
                else:
                    units.append(child.get_text("", strip=False))
            else:
                raise SourceParseError("月亮小說網正文包含無法辨識的節點")
        if reversed_paragraph:
            units.reverse()
        paragraph.clear()
        paragraph.append("".join(units))
        paragraph.attrs.pop("class", None)
    if article.select_one("i[class^='icon-'], i[class*=' icon-']"):
        raise SourceParseError("月亮小說網正文仍有未還原字型字元")
