"""Read public HTML structure without storing chapter text."""
import argparse
import json
import re
from urllib.parse import urlsplit

import requests
from bs4 import BeautifulSoup

PAGES = {
    "moon17": [
        "https://www.17moon.com/book/8Kk/Fv2Bq.html",
        "https://www.17moon.com/book/8Kk/Fv2Bq_2.html",
        "https://www.17moon.com/book/8Kk/eHQyA.html",
    ],
    "twkan": [
        "https://www.twkan.cc/chapter/2T9qchiJD4h31AQnZuI6gI.html",
        "https://www.twkan.cc/chapter/2T9qchiJD4h31AQnZuI6gI/7GvsnsqCBoyrcdHumKyoZp.html",
        "https://www.twkan.cc/chapter/2T9qchiJD4h31AQnZuI6gI/6AkjO7YfRD1wcCMYTfBrax.html",
    ],
    "book8": [
        "https://8book.com/novelbooks/168909/",
        "https://8book.com/read/168909/?39876",
    ],
}


def chars(s):
    return len(re.findall(r"[\u3400-\u9fff]", s or ""))


def attributes(tag):
    return {key: value for key, value in tag.attrs.items() if key in ("id", "class", "style", "dir", "href", "src", "role") or key.startswith("data-")}


def inspect(source):
    result = {"source": source, "pages": []}
    for url in PAGES[source]:
        item = {"url": url}
        result["pages"].append(item)
        try:
            r = requests.get(url, timeout=20, headers={"User-Agent": "AUDIO-source-validation/1.0"}, allow_redirects=False)
            item["http"] = r.status_code
            if r.status_code != 200:
                continue
            soup = BeautifulSoup(r.content, "html.parser")
            item["title"] = soup.title.get_text(strip=True) if soup.title else ""
            if source == "moon17":
                body = soup.select_one("article#article")
                item["body_attrs"] = attributes(body) if body else {}
                item["paras"] = []
                for p in body.select("p") if body else []:
                    text = p.get_text("", strip=True)
                    item["paras"].append({"attrs": attributes(p), "chars": chars(text),
                        "starts_punct": bool(re.match(r"^[。，？！”。」：]", text)),
                        "ends_punct": bool(re.search(r"[。，？！”。」：]$", text)),
                        "children": [{"tag":c.name,"attrs":attributes(c)} for c in p.find_all(recursive=False)]})
                item["stylesheets"] = [l.get("href") for l in soup.select("link[rel=stylesheet]")]
                item["inline_bidi_rules"] = [line[:300] for t in soup.select("style") for line in t.get_text().splitlines() if re.search(r"bidi|direction|reverse|rtl",line,re.I)][:20]
            elif source == "twkan":
                body = soup.select_one("#content.readcontent")
                item["body_chars"] = chars(body.get_text("", strip=True)) if body else None
                item["chapter_headings"] = [h.get_text(" ",strip=True) for h in soup.select("h1")]
                item["last_chapters"] = [{"href":a.get("href"),"label":a.get_text(" ",strip=True)} for a in soup.select("#allchapter a[href]")[-8:]]
                item["navigation"] = [{"href":a.get("href"),"label":a.get_text(" ",strip=True)} for a in soup.select(".mulu-box a[href]")]
                item["body_children"] = [{"tag":p.name,"attrs":attributes(p),"chars":chars(p.get_text("",strip=True))} for p in body.find_all(recursive=False)[:30]] if body else []
            elif source == "book8":
                item["catalog_link_attributes"] = [attributes(a) for a in soup.select("#chapters a[href]")[:4]]
                item["scripts"] = [s.get("src") for s in soup.select("script[src]")]
                item["iframe"] = [attributes(s) for s in soup.select("iframe")]
                item["ids"] = [{"tag":s.name,"id":s.get("id"),"class":s.get("class"),"chars":chars(s.get_text("",strip=True))} for s in soup.select("[id]")[:70]]
        except Exception as e:
            item["error"] = f"{type(e).__name__}: {e}"
    print(json.dumps(result, ensure_ascii=False, indent=2),flush=True)

if __name__ == "__main__":
    p=argparse.ArgumentParser()
    p.add_argument("source", choices=PAGES)
    inspect(p.parse_args().source)
