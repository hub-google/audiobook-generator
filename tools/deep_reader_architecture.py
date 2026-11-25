"""Inspect source-owned JavaScript, font metadata and explicitly linked reading host."""
import argparse, json, re
from urllib.parse import urljoin, urlsplit
import requests
from bs4 import BeautifulSoup
HEADERS={"User-Agent":"AUDIO-source-validation/1.0"}

def req(url):
    try:
        r=requests.get(url,headers=HEADERS,timeout=18,allow_redirects=False)
        return (r.status_code,r.text,r.headers.get("Location",""))
    except Exception as e:
        return (None,"",str(e))

def lines_with(text,pat,context=180,max_results=14):
    out=[]
    for m in re.finditer(pat,text,re.I):
        s=max(0,m.start()-context)
        e=min(len(text),m.end()+context)
        excerpt=" ".join(text[s:e].split())
        if excerpt not in out:out.append(excerpt)
        if len(out)>=max_results:break
    return out

def main():
    p=argparse.ArgumentParser()
    p.add_argument("source",choices=["moon17","book8"])
    source=p.parse_args().source
    res={"source":source}
    if source=="moon17":
        page="https://www.17moon.com/book/8Kk/Fv2Bq.html"
        st,text,loc=req(page)
        soup=BeautifulSoup(text,"html.parser")
        res["page_status"]=st
        res["reader_scripts"]=[urljoin(page,s["src"]) for s in soup.select("script[src]") if "reader" in s["src"]]
        res["chapter_icon_counts"]={}
        for i in soup.select("article#article i[class]"):
            for class_name in i.get("class",[]):
                if class_name.startswith("icon-"):
                    res["chapter_icon_counts"][class_name]=res["chapter_icon_counts"].get(class_name,0)+1
        for name,url in {
            "reader_js":"https://www.17moon.com/static/modern/js/reader.js?v=20260819a",
            "icon_css":"https://www.17moon.com/static/modern/css/Stncs0M9d.css",
        }.items():
            status,content,loc=req(url)
            obj={"status":status,"bytes":len(content)}
            if name=="reader_js":
                s=content.find("function initReverseParagraphs")
                obj["reverse_fn"]=content[s:s+2300] if s>=0 else "not found"
            else:
                obj["font_face"]=content[:1300]
                obj["mapping_count"]=len(re.findall(r"\.icon-\d+:before",content))
                obj["other"]=lines_with(content,r"font-family|unicode-range",80,6)
            res[name]=obj
        for ext in ("woff2","ttf","svg"):
            url=f"https://www.17moon.com/static/modern/css/fonts/Stncs0M9d.{ext}"
            try:
                r=requests.get(url,headers=HEADERS,timeout=12,allow_redirects=False)
                res["font_"+ext]={"status":r.status_code,"bytes":len(r.content)}
            except Exception as e:
                res["font_"+ext]={"error":str(e)[:100]}
    else:
        page="https://8book.com/read/168909/?39876"
        st,html,loc=req(page)
        soup=BeautifulSoup(html,"html.parser")
        res["reader_status"]=st
        res["meta"]=[{"name":x.get("name"),"value":(x.get("content") or "")[:140]} for x in soup.select("meta[name]") if x.get("name") in ("id","cid","bookid","chapterid","episodeid")]
        res["content"]=[]
        for x in soup.select("#content"):
            res["content"].append({"class":x.get("class"),"data":{k:v for k,v in x.attrs.items() if k.startswith("data-")},"child_tags":[c.name for c in x.find_all(recursive=False)],"text_chinese":len(re.findall(r"[\u3400-\u9fff]",x.get_text("",strip=True)))})
        inline="\n".join(x.get_text() for x in soup.select("script:not([src])"))
        res["inline_reader"]=lines_with(inline,r"content|chapter|itemid|episode|load|read|url|\.get\(|\.post\(|ajax",220,15)
        for name,url in {
            "read_js":"https://8book.com/js/read.js?v=4944346245",
            "item_js":"https://www.8book.com/js/item.js",
        }.items():
            st,js,_=req(url)
            res[name]={"status":st,"bytes":len(js),"relevant":lines_with(js,r"content\b|loadcontent|chapter|episode|eps|epage|domain =|/read/|\.get\(|\.post\(|ajax|query",180,24)}
        # These two URLs are published in the site's own chapter links and JS.
        # Inspect response metadata only, no third-party chapter text is retained.
        for url in ["https://sport.thepaperbooks.com/read/168909/?39876","https://finance.binaccount.com/read/133518/?3999572"]:
            st,html,loc=req(url)
            soup=BeautifulSoup(html,"html.parser")
            res.setdefault("advertised_readers",[]).append({"host":urlsplit(url).hostname,"status":st,"redirect":loc[:140],"title":soup.title.get_text(" ",strip=True)[:140] if soup.title else "","body_chars":len(re.findall(r"[\u3400-\u9fff]",soup.get_text(" ",strip=True))),"reader_chars":len(re.findall(r"[\u3400-\u9fff]",(soup.select_one("#content") or soup).get_text(" ",strip=True)))})
    print(json.dumps(res,ensure_ascii=False,indent=2),flush=True)

if __name__=="__main__":
    main()
