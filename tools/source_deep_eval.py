"""Final structure details of public readers. Do not save or print chapter text."""
import argparse, json, re
from io import BytesIO
from urllib.parse import urljoin
from bs4 import BeautifulSoup
from fontTools.ttLib import TTFont
import requests

def retrieve(url):
    r=requests.get(url,timeout=20,headers={"User-Agent":"AUDIO-source-validation/1.0"},allow_redirects=False)
    return r

def run(source):
    result={"source":source}
    if source=="moon17":
        url="https://www.17moon.com/static/modern/css/fonts/Stncs0M9d.ttf"
        r=retrieve(url)
        result["ttf_status"]=r.status_code
        if r.status_code==200:
            f=TTFont(BytesIO(r.content))
            cmap=f.getBestCmap()
            result["font_cmap"]={"size":len(cmap),"sample":list(cmap.items())[:12],"names":[{"id":n.nameID,"value":n.toUnicode()[:90]} for n in f["name"].names[:15]]}
            result["font_glyph_count"]=len(f.getGlyphOrder())
        r=retrieve("https://www.17moon.com/static/modern/css/fonts/Stncs0M9d.svg")
        result["svg_status"]=r.status_code
        if r.status_code==200:
            s=r.text
            result["svg_head"]=s[:1900]
            names=[]
            for tag in re.findall(r"<glyph\b[^>]*>",s)[:12]:
                names.append(re.sub(r"\s+"," ",tag)[:300])
            result["svg_glyph_examples"]=names
    else:
        url="https://8book.com/read/168909/?39876"
        r=retrieve(url)
        result["read_status"]=r.status_code
        soup=BeautifulSoup(r.content,"html.parser")
        scripts=[s.get_text() for s in soup.select("script:not([src])")]
        matches=[]
        for s in scripts:
            idx=s.find("$.get(unescape(")
            if idx>=0:
                matches.append(s[max(0,idx-2500):idx+1600])
                result["script_length"]=len(s)
            if "var bids" in s or "var td" in s or "var e=" in s:
                matches.append(s[:1300])
        result["inline_chunks"]=matches[:4]
        for url in [
            "https://8book.com/txt/168909/39876.html",
            "https://8book.com/txt/11f168909/39876.html",
        ]:
            try:
                r=retrieve(url)
                soup=BeautifulSoup(r.content,"html.parser")
                result.setdefault("sample_endpoints",[]).append({"url":url,"status":r.status_code,"length":len(r.content),"title":soup.title.get_text(" ",strip=True)[:100] if soup.title else "","chinese_chars":len(re.findall(r"[\u3400-\u9fff]",soup.get_text(" ",strip=True)))})
            except Exception as e:
                result.setdefault("sample_endpoints",[]).append({"url":url,"error":str(e)[:150]})
    print(json.dumps(result,ensure_ascii=False,indent=2),flush=True)

if __name__=="__main__":
    p=argparse.ArgumentParser()
    p.add_argument("source",choices=["moon17","book8"])
    run(p.parse_args().source)
