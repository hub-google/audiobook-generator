from __future__ import annotations

import json
from pathlib import Path


def main() -> None:
    root = Path(__file__).resolve().parent
    config = json.loads((root / "pipeline_config.json").read_text(encoding="utf-8"))
    metadata = json.loads((root / "kernel-metadata.json").read_text(encoding="utf-8"))
    assert config["images_per_chapter"] == 2
    assert config["inference_steps"] == 4
    assert (config["width"], config["height"]) == (1280, 720)
    assert config["prompt_model"] == "Qwen/Qwen2.5-7B-Instruct"
    assert config["publish_status"] == "needs_review"
    assert metadata["is_private"] is True
    assert metadata["enable_gpu"] is True
    assert metadata["enable_internet"] is True
    assert metadata["id"] == "cit5055/quanzhigaoshou-chapter-images"
    assert Path(root / metadata["code_file"]).is_file()
    for required in ("characters.json", "style_bible.json"):
        assert Path(root / required).is_file()
    print("設定檔驗證通過：私人 Kernel、GPU、網路、7B 分鏡模型、每章兩張 1280×720 圖。")


if __name__ == "__main__":
    main()
