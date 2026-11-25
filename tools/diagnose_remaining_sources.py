"""Inspect the public page and its own JS/CSS for three source-specific defects.

Diagnostics only: never store novel body, never solve challenges and never
fetch an external reading host from untrusted markup.
"""
import argparse, json, re
from urllib.parse import urljoin, urlsplit
import requests
from bs4 import BeautifulSoup

TARGETS={
 "czbooks":["https://czbooks.net/n/c37492","https://www.czbooks.net/n/c37492"],
 "moon17":["https://www.17moon.com/book/8Kk/Fv2Bq.html"],
 "book8":["https://www.8book.com/novelbooks/168909/","https://8book.com/read/168909/?39876"],
}
KEYWORDS = re.compile(r"reverse|reversed|icon-|font-face|content|read\b|chapter|fetch\(|ajax|axios|api/|reader|https?://",re.I)
UA = {"User-Agent":"AUDIO-source-validation/1.0"}


def summary(url):
    entry={"url":url}
    try:
        r=requests.get(url,timeout=15,headers=UA,allow_redirects=False)
        entry["status"]=r.status_code
        entry["bytes"]=len(r.content)
        entry["redirect"]=r.headers.get("Location")
        if r.status_code!=200:
            return entry
        typ=r.headers.get("Content-Type","")
        if "javascript" in typ or "css" in typ or urlsplit(url).path.endswith((".js",".css")):
            text=r.text
            entry["length"]=len(text)
            entry["matches"]=[]
            for match in list(KEYWORDS.finditer(text))[:120]:
                excerpt=text[max(0,match.start()-110):min(len(text),match.end()+160)]
                excerpt=re.sub(r"\s+"," ",excerpt)
                if len(entry["matches"])>=28: break
                entry["matches"].append(excerpt)
            return entry
        soup=BeautifulSoup(r.content,"html.parser")
        entry["title"]=soup.title.get_text(" ",strip=True)[:120] if soup.title else ""
        entry["scripts"]=[urljoin(url,s["src"]) for s in soup.select("script[src]")][:32]
        entry["styles"]=[urljoin(url,s["href"]) for s in soup.select('link[rel="stylesheet"][href]')][:32]
        entry["reader_counts"]={s:len(soup.select(s)) for s in ("#article","#content",".js-reversed-text","i[class^=icon-]","#chapters",".chapter-content")}
        if "book8" in url:
            entry["reader_ids"]=[{"id":x.get("id"),"class":x.get("class"),"chars":len(re.findall(r"[\u3400-\u9fff]",x.get_text("",strip=True)))} for x in soup.select("div[id],article[id]") if x.get("id") in ("content","chapter","chapters","readcontent","txtnav")]
            entry["chapter_hosts"]=sorted({urlsplit(urljoin(url,x["href"])).hostname for x in soup.select("#chapters a[href]")})
        if "17moon" in url:
            entry["sample_reversed"]=[]
            for p in soup.select(".js-reversed-text")[:8]:
                entry["sample_reversed"].append({"length":len(p.get_text("",strip=True)),"children":[{"tag":c.name,"class":c.get("class")} for c in p.find_all(recursive=False) if c.name=="i"]})
        return entry
    except Exception as exc:
        entry["error"]=f"{type(exc).__name__}: {exc}"
        return entry


def main():
    p=argparse.ArgumentParser()
    p.add_argument("source",choices=TARGETS)
    source=p.parse_args().source
    pages=[summary(u) for u in TARGETS[source]]
    assets=[]
    for page in pages:
        if page.get("status")!=200: continue
        for u in page.get("scripts",[]):
            if urlsplit(u).hostname != urlsplit(page["url"]).hostname: continue
            if source=="moon17" and not re.search(r"(read|book|chapter|main|global|article)",u,re.I): continue
            if source=="book8" and not re.search(r"(read|item|8book)",u,re.I): continue
            assets.append(u)
        if source=="moon17":
            assets+=page.get("styles",[])
    assets=list(dict.fromkeys(assets))[:12]
    for url in assets:
        if urlsplit(url).hostname not in {urlsplit(x).hostname for x in TARGETS[source]}:
            continue
        pages.append(summary(url))
    print(json.dumps({"source":source,"pages":pages},ensure_ascii=False,indent=2),flush=True)

if __name__=="__main__":
    main()
