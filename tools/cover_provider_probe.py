"""One real HF image request, then one Gemini request only on fallback."""
import os
from src.metadata_gen import download_ai_image

PROMPT="Atmospheric landscape illustration of a fictional lighthouse in a blue night sky, no text."

def main():
    lines=[]
    hf=bool(os.getenv("HF_TOKEN","").startswith("hf_"))
    gemini=bool(os.getenv("GEMINI_API_KEY","").strip())
    if not hf and not gemini:
        raise RuntimeError("未設定 HF_TOKEN / GEMINI_API_KEY，無法進行真實生圖測試")
    if hf:
        try:
            os.environ["COVER_IMAGE_PROVIDER"]="hf"
            image=download_ai_image(PROMPT,max_attempts=1)
            assert image.size==(1280,720)
            lines.append("HF: 成功（1280×720）")
        except Exception as exc:
            lines.append("HF: 失敗（"+type(exc).__name__+": "+str(exc)[:250]+"）")
    else:
        lines.append("HF: 未設定 Token")
    if not lines[0].startswith("HF: 成功"):
        if not gemini:
            lines.append("Gemini: 未設定 API Key")
        else:
            os.environ["COVER_IMAGE_PROVIDER"]="gemini"
            image=download_ai_image(PROMPT,max_attempts=1)
            assert image.size==(1280,720)
            lines.append("Gemini: 成功（1280×720）")
    else:
        lines.append("Gemini: "+("Key 已設定，備援未觸發" if gemini else "未設定"))
    print("\n".join(lines))
    summary=os.getenv("GITHUB_STEP_SUMMARY")
    if summary:
        with open(summary,"a",encoding="utf-8") as f:
            f.write("## AUDIO 真實生圖驗證\n\n"+ "\n".join("- "+s for s in lines)+"\n")
    if not any("成功（1280×720）" in x for x in lines):
        raise RuntimeError("HF 與 Gemini 均未產出可用封面")
if __name__=="__main__":main()
