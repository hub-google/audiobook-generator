"""Real Cloudflare image-only verification, never falls back to paid inference."""
import os
from src.metadata_gen import download_ai_image

PROMPT = "Atmospheric landscape illustration of a fictional lighthouse in a blue night sky, no text."


def main():
    os.environ["COVER_IMAGE_PROVIDER"] = "cloudflare"
    image = download_ai_image(PROMPT, max_attempts=1)
    assert image.size == (1280, 720)
    image.save("cloudflare-cover-probe.jpg", "JPEG", quality=95)
    summary = os.getenv("GITHUB_STEP_SUMMARY")
    if summary:
        with open(summary, "a", encoding="utf-8") as handle:
            handle.write("Cloudflare Workers AI：實際產圖成功（1280×720）\n")
    print("Cloudflare: 成功（1280×720）")


if __name__ == "__main__":
    main()
