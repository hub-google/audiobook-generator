
"""Cross-language clean-text parity: browser preview MUST match Python TTS input."""
from pathlib import Path
import json
import subprocess
import pytest
from src.cleaner import clean_text_content, chunk_text

ROOT=Path(__file__).resolve().parents[1]
JS=ROOT/"docs/site/cleaner-parity.js"

@pytest.mark.parametrize("title,book,raw,patterns",[
    ("第1章 開始","作品","第1章 開始\n第一段文學內容，第二句也在這裡。\n尾聲。",[]),
    ("第2章 走入森林","小說名","小說名\n黃金屋\n第二章 走入森林\n這是一段內容，但主角仍不想離開。",[]),
    ("第8章 聯盟","冒險","广告测试以及作者的话。\n段落，還有。\n",["广告测试"]),
    ("第10章 帳本","奇案","故事開始……但是下一段：非常精彩！\n\n不能停，還有後續。",[]),
    ("第5章 更新","小說"," \u200b全形　字與空格 \n請記住本站域名\n說了很多很多很多話，不過事情另有蹊蹺。\n",[]),
    ("第105章 美好","作品","本書是測試語句，ABCDABCD\n\n廣告詞ABCABCD",["AB","ABC","書是"]),
    ("第3章 內容","作品","第一回\n第3章 內容\n手機閱讀\n這裡，才是真正的故事。",[]),
    ("第6章 完","作品","段落中間，寫著：『這是真的。』而後繼續。\n最後就結束！",[]),
    ("第7章","作品","文章……因此接下來會有劇情發展，結果讓大家驚訝。",[]),
    ("第18章 這章","小說","這是🌠星空與心靈。\n這裡的結局也不一樣！",[]),
])
def test_browser_clean_preview_equals_actual_python(title,book,raw,patterns):
    expected=chunk_text(clean_text_content(raw,title,book,remove_patterns=patterns))
    javascript="const fs=require('fs');const f=require(process.argv[1]);const x=JSON.parse(fs.readFileSync(0,'utf8'));process.stdout.write(JSON.stringify(f.chunkText(f.cleanTextContent(x.raw,x.title,x.book,x.patterns))));"
    result=subprocess.run(["node","-e",javascript,str(JS)],input=json.dumps(dict(title=title,book=book,raw=raw,patterns=patterns),ensure_ascii=False),capture_output=True,text=True,check=True)
    assert json.loads(result.stdout)==expected

def test_js_cleaner_has_only_pure_processing_and_correct_page_order():
    js=JS.read_text(encoding="utf-8")
    html=(ROOT/"docs/site/index.html").read_text(encoding="utf-8")
    review=(ROOT/"docs/site/review-inspector.js").read_text(encoding="utf-8")
    assert 'src="./cleaner-parity.js"' in html
    assert html.index('src="./cleaner-parity.js"')<html.index('src="./review-inspector.js"')
    assert 'AudioCleaner.cleanTextContent' in review
    assert "fetch(" not in js and "localStorage" not in js
