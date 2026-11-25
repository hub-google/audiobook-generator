"""Compare legitimate publicly served 69Shuba URLs/transports once each.

Do not solve access challenges, rotate identities or retrieve chapter content.
"""
import json
import time
from bs4 import BeautifulSoup
import requests

try:
    from curl_cffi import requests as curl_requests
except ImportError:
    curl_requests=None

ROUTES=[
 "https://www.69shuba.com/book/53782/",
 "https://www.69shuba.com/book/53782.htm",
 "https://69shuba.com/book/53782/",
 "https://69shuba.com/book/53782.htm",
]
UA={"User-Agent":"Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"}

def one(get,url):
    out={"url":url}
    try:
        r=get(url,headers=UA,timeout=16,allow_redirects=False)
        out["status"]=r.status_code
        out["redirect"]=r.headers.get("Location","")[:150]
        if r.status_code==200:
            soup=BeautifulSoup(r.content,"html.parser")
            out["has_catalog"]=bool(soup.select_one("#catalog"))
            out["catalog_links"]=len(soup.select("#catalog a[href]"))
            out["title"]=soup.title.get_text(" ",strip=True)[:100] if soup.title else ""
    except Exception as e:
        out["error"]=f"{type(e).__name__}: {e}"[:180]
    return out

def main():
    reports=[]
    sessions=[("requests",requests.get)]
    if curl_requests:
        session=curl_requests.Session(impersonate="chrome120")
        sessions.append(("curl_cffi",session.get))
    for transport,get in sessions:
        for url in ROUTES:
            reports.append({"transport":transport,**one(get,url)})
            time.sleep(1.8)
    print(json.dumps({"source":"shuba69","probes":reports},ensure_ascii=False,indent=2),flush=True)

if __name__=="__main__":
    main()
