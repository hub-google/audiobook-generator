"""Temporary visual diagnostics for a public site's icon font.

Font bytes stay inside the runner. Only labeled raster image base64 appears
in private diagnostics, and no font is committed or shared.
"""
import base64, io
from PIL import Image, ImageDraw, ImageFont
import requests

URL="https://www.17moon.com/static/modern/css/fonts/Stncs0M9d.ttf"
def main():
    r=requests.get(URL,timeout=20,headers={"User-Agent":"AUDIO-source-validation/1.0"})
    r.raise_for_status()
    font=ImageFont.truetype(io.BytesIO(r.content),64)
    labelfont=ImageFont.truetype("DejaVuSans.ttf",17)
    n=195
    for base in range(0,n,50):
        sheet=Image.new("RGB",(1050,620),"white")
        d=ImageDraw.Draw(sheet)
        for off in range(min(50,n-base)):
            idx=base+off
            x=(off%10)*105
            y=(off//10)*124
            d.text((x+8,y+2),f"{idx+1:02d}",font=labelfont,fill="#111111")
            d.text((x+13,y+25),chr(0xE900+idx),font=font,fill="#000000")
        o=io.BytesIO()
        sheet.save(o,format="PNG",optimize=True)
        print(f"SHEET_{base//50}="+base64.b64encode(o.getvalue()).decode(),flush=True)
if __name__=="__main__":
    main()
