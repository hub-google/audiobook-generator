"""Inspect public reader rendering, never log or save novel text."""
import argparse
import json
from urllib.parse import urlsplit, urljoin

from playwright.sync_api import sync_playwright
from bs4 import BeautifulSoup
import requests

TARGETS = {
    "book8": ("https://8book.com/novelbooks/168909/", "https://8book.com/read/168909/?39876", ("8book.com", "www.8book.com", "m.8book.com", "v.8book.com")),
    "moon17": ("https://www.17moon.com/book/8Kk/", "https://www.17moon.com/book/8Kk/Fv2Bq.html", ("www.17moon.com", "17moon.com")),
    "twkan": ("https://www.twkan.cc/chapter/2T9qchiJD4h31AQnZuI6gI.html", "https://www.twkan.cc/chapter/2T9qchiJD4h31AQnZuI6gI/7GvsnsqCBoyrcdHumKyoZp.html", ("www.twkan.cc", "twkan.cc")),
    "czbooks": ("https://czbooks.net/n/c37492", None, ("czbooks.net", "www.czbooks.net")),
    "shuba69": ("https://www.69shuba.com/book/53782/", None, ("www.69shuba.com", "69shuba.com")),
}

JS = r"""() => {
    const chars = s => (s.match(/[\u3400-\u9fff]/g) || []).length;
    const result = [];
    for (const el of document.querySelectorAll('main,article,section,div[id],div[class]')) {
        const text = el.innerText || '';
        const n = chars(text);
        if (n < 40) continue;
        const css = getComputedStyle(el);
        result.push({tag: el.tagName, id: el.id, cls: typeof el.className === 'string' ? el.className : '', chars:n,
          p:el.querySelectorAll('p').length, links:el.querySelectorAll('a').length,
          direction:css.direction,bidi:css.unicodeBidi});
    }
    const paras = Array.from(document.querySelectorAll('article#article p')).map(el => {
        const css = getComputedStyle(el);
        const t = (el.innerText || '').trim();
        return {cls:el.className, dir:el.getAttribute('dir'), style:el.getAttribute('style'),
          direction:css.direction,bidi:css.unicodeBidi, chars:chars(t),
          startsPunct:/^[。，？！”。」：]/.test(t), endsPunct:/[。，？！”。」：]$/.test(t)};
    });
    const links = Array.from(document.querySelectorAll('a[href]')).filter(el => /開始看|開始閱讀|下一[頁页]|下一章|上一章|目錄|第(?:1|499|500)章/.test(el.innerText || '')).map(el => ({url:el.href,label:(el.innerText||'').trim().slice(0,60)})).slice(0,12);
    return {title:document.title, headings:Array.from(document.querySelectorAll('h1')).map(el=>el.innerText.trim().slice(0,120)),
      text_chars:chars(document.body.innerText||''), containers:result.sort((a,b)=>b.chars-a.chars).slice(0,20),
      paras:paras.slice(0,80), links, scripts:Array.from(document.scripts).filter(s=>s.src).map(s=>s.src).slice(0,30)};
}"""


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("source", choices=TARGETS)
    args = parser.parse_args()
    book, chapter, hosts = TARGETS[args.source]
    result = {"source": args.source, "pages": []}
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()
        # Standard browser rendering; no spoofing, challenge solving or foreign reader pages.
        def route_document(route):
            if route.request.is_navigation_request() and urlsplit(route.request.url).hostname not in hosts:
                route.abort()
            else:
                route.continue_()
        page.route("**/*", route_document)
        for url in [book, chapter]:
            if not url:
                continue
            entry = {"url": url}
            result["pages"].append(entry)
            try:
                response = page.goto(url, wait_until="domcontentloaded", timeout=35000)
                entry["http"] = response.status if response else None
                entry["final_url"] = page.url
                if entry["http"] in (401, 403, 429):
                    entry["status"] = "ACCESS_RESTRICTED"
                    continue
                page.wait_for_timeout(3500)
                entry["rendered"] = page.evaluate(JS)
                entry["frames"] = [frame.url for frame in page.frames]
                if response:
                    raw = BeautifulSoup(response.body(), "html.parser")
                    entry["raw_paragraph_attributes"] = [{"class":p.get("class"),"dir":p.get("dir"),"style":p.get("style"),"children":[{"tag":c.name,"class":c.get("class"),"dir":c.get("dir"),"style":c.get("style")} for c in p.find_all(recursive=False)]} for p in raw.select("article#article p")[:60]]
                    entry["raw_iframes"] = [x.get("src") for x in raw.select("iframe[src]")]
                    entry["raw_chapter_titles"] = [{"href":x.get("href"),"label":x.get_text(" ",strip=True)[:100]} for x in raw.select("#allchapter a[href]")[-5:]]
            except Exception as e:
                entry["error"] = f"{type(e).__name__}: {e}"
        browser.close()
    print(json.dumps(result, ensure_ascii=False, indent=2), flush=True)

if __name__ == "__main__":
    main()
