from __future__ import annotations

import gc, hashlib, importlib.util, json, os, subprocess, sys, time
from datetime import datetime, timezone
from pathlib import Path


MODULE_START = time.perf_counter()


def ensure_dependencies():
    # Kaggle's current Python 3.12 image may ship a PyTorch build without sm_60;
    # the assigned Tesla P100 requires the CUDA 12.1 build that still includes it.
    subprocess.check_call([sys.executable, "-m", "pip", "install", "--quiet", "--upgrade",
                           "torch==2.5.1", "torchvision==0.20.1", "--index-url", "https://download.pytorch.org/whl/cu121"])
    required = {"diffusers": "diffusers>=0.31,<0.37", "transformers": "transformers>=4.45,<5",
                "accelerate": "accelerate>=1,<2", "safetensors": "safetensors>=0.4,<1",
                "huggingface_hub": "huggingface-hub>=0.27,<1", "bitsandbytes": "bitsandbytes>=0.43,<1"}
    missing = [pkg for module, pkg in required.items() if importlib.util.find_spec(module) is None]
    if missing: subprocess.check_call([sys.executable, "-m", "pip", "install", "--quiet", *missing])


ensure_dependencies()
import torch
from diffusers import EulerDiscreteScheduler, StableDiffusionXLPipeline, UNet2DConditionModel
from huggingface_hub import CommitOperationAdd, HfApi, hf_hub_download
from kaggle_secrets import UserSecretsClient
from safetensors.torch import load_file
from transformers import AutoModelForCausalLM, AutoTokenizer, BitsAndBytesConfig

ROOT, INPUT_ROOT, OUTPUT_ROOT = Path(__file__).resolve().parent, Path("/kaggle/input"), Path("/kaggle/working/chapter_images")


def input_asset(name):
    local = ROOT / name
    matches = [local] if local.exists() else list(INPUT_ROOT.rglob(name))
    if len(matches) != 1: raise RuntimeError(f"預期恰好一份 {name}，實際找到 {len(matches)} 份")
    return matches[0]


CONFIG = json.loads(input_asset("pipeline_config.json").read_text(encoding="utf-8"))
CHARACTERS = json.loads(input_asset("characters.json").read_text(encoding="utf-8"))
WEIGHTS = {"narrative": 25, "visual": 20, "character": 15, "emotion": 15, "coverage": 15, "evidence": 10}


def load_chapters():
    matches = list(INPUT_ROOT.rglob("chapters.jsonl"))
    if len(matches) != 1: raise RuntimeError(f"預期恰好一份 chapters.jsonl，實際找到 {len(matches)} 份")
    rows = [json.loads(line) for line in matches[0].read_text(encoding="utf-8").splitlines() if line.strip()]
    if len({row["chapter"] for row in rows}) != len(rows): raise RuntimeError("chapters.jsonl 含重複章號")
    return rows


def extract_object(text):
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end <= start: raise ValueError("模型沒有回傳 JSON object")
    value = json.loads(text[start:end + 1])
    if not isinstance(value, dict): raise ValueError("模型輸出不是 object")
    return value


def model_json(model, tokenizer, system, user, max_tokens=1800):
    messages = [{"role": "system", "content": system}, {"role": "user", "content": user}]
    for attempt in range(3):
        encoded = tokenizer.apply_chat_template(messages, add_generation_prompt=True, return_tensors="pt", return_dict=True).to(model.device)
        input_length = encoded["input_ids"].shape[-1]
        with torch.inference_mode(): generated = model.generate(**encoded, max_new_tokens=max_tokens, do_sample=False)
        response = tokenizer.decode(generated[0][input_length:], skip_special_tokens=True)
        try: return extract_object(response)
        except (ValueError, json.JSONDecodeError):
            if attempt == 2: raise
            messages += [{"role": "assistant", "content": response}, {"role": "user", "content": "只回傳一個合法 JSON object，保留原資料。"}]


def analyze_chapter(item, model, tokenizer):
    chunks = [{k: c[k] for k in ("chunk_id", "start_char", "end_char", "text")} for c in item["chunks"]]
    system = "你是小說分鏡分析器，不是續寫者。所有事件須有本章原文支持，不得捏造外觀；區分真人、遊戲、回憶及轉述；每個候選是一個可見瞬間；只輸出合法 JSON object。"
    user = f"""BOOK_ID:{item['book_id']} CHAPTER:{item['chapter']} TITLE:{item['title']}
輸出 core_summary_zh、two_scene_feasibility、candidates。列 4 至 8 個候選，不足就少列。每個候選含 event_id、position(0到1)、event_type、summary_zh、characters(姓名陣列)、location、visible_action、visible_emotion、key_objects、evidence(含 chunk_id/start_char/end_char/support_summary)、scores(narrative/visual/character/emotion/coverage/evidence，各0到5整數)、uncertainties、not_depicted。
CHUNKS:{json.dumps(chunks, ensure_ascii=False)}"""
    result = model_json(model, tokenizer, system, user); result["chapter"] = item["chapter"]
    return result


def candidate_score(c):
    return sum(max(0, min(5, float(c.get("scores", {}).get(k, 0)))) / 5 * w for k, w in WEIGHTS.items())


def evidence_ids(c): return {e.get("chunk_id", "") for e in c.get("evidence", [])}


def select_pair(analysis, item):
    ids = {c["chunk_id"] for c in item["chunks"]}
    candidates = [c for c in analysis.get("candidates", []) if c.get("evidence") and
                  all(e.get("chunk_id") in ids for e in c["evidence"]) and candidate_score(c) >= 60]
    best = None
    for i, first in enumerate(candidates):
        for second in candidates[i + 1:]:
            if float(first.get("position", 0)) >= float(second.get("position", 0)) or evidence_ids(first) == evidence_ids(second): continue
            temporal = min(100, (float(second["position"]) - float(first["position"])) * 140)
            complement = 100 if first.get("event_type") != second.get("event_type") else 65
            variety = 100 if first.get("location") != second.get("location") else 70
            score = .35*candidate_score(first)+.35*candidate_score(second)+.12*temporal+.10*complement+.08*variety
            if best is None or score > best[0]: best = (score, first, second)
    if best is None or best[0] < 65: return {"chapter": item["chapter"], "status": "needs_story_review"}
    return {"chapter": item["chapter"], "status": "selected", "pair_score": round(best[0], 2), "scenes": [best[1], best[2]]}


def character_blocks(names):
    versions, blocks = {}, []
    for character in CHARACTERS["characters"]:
        if any(name in character["names"] for name in names):
            form_id, form = next(iter(character["forms"].items()))
            versions[character["character_id"]], blocks = form_id, blocks + [form["immutable_prompt_block"]]
    return versions, "; ".join(blocks)


def compose_storyboard(item, selection, model, tokenizer):
    board = {"book_id": item["book_id"], "chapter": item["chapter"], "title": item["title"],
             "source_sha256": item["source_sha256"], "pair_score": selection["pair_score"], "scenes": []}
    system = "你是忠實的靜態分鏡設計器，只整理已選事件，不增加原文沒有的關鍵動作或身份。只輸出合法 JSON object。"
    for index, event in enumerate(selection["scenes"], 1):
        names = event.get("characters") or []
        if names and isinstance(names[0], dict): names = [x.get("name_in_text", "") for x in names]
        versions, identities = character_blocks(names)
        user = f"""把事件整理為單張16:9畫面。輸出 scene_summary_zh、explicit_details、safe_inferences、unknown_keep_neutral、forbidden_inventions、positive_prompt_en。事件:{json.dumps(event, ensure_ascii=False)} 角色固定外觀:{identities}。英文prompt只描述內容，不寫畫風。"""
        refined = model_json(model, tokenizer, system, user, 900)
        prompt = f"{refined.get('positive_prompt_en', event.get('summary_zh',''))}. Character identity: {identities}. Style: {CONFIG['immutable_style_block']}. no written text"
        board["scenes"].append({"scene": index, "source_event_id": event.get("event_id"), "evidence": event.get("evidence", []),
                                "character_versions": versions, "summary_zh": refined.get("scene_summary_zh", event.get("summary_zh")),
                                "prompt": prompt, "negative_prompt": CONFIG["negative_prompt"]})
    return board


def load_image_pipeline():
    pipe = StableDiffusionXLPipeline.from_pretrained(CONFIG["image_base_model"], torch_dtype=torch.float16, variant="fp16", use_safetensors=True)
    checkpoint = hf_hub_download(CONFIG["image_acceleration_model"], CONFIG["image_acceleration_file"])
    unet = UNet2DConditionModel.from_config(pipe.unet.config).to("cuda", torch.float16)
    unet.load_state_dict(load_file(checkpoint, device="cuda")); pipe.unet = unet
    pipe.scheduler = EulerDiscreteScheduler.from_config(pipe.scheduler.config, timestep_spacing="trailing")
    pipe.to("cuda"); pipe.enable_vae_slicing()
    return pipe


def file_sha(path): return hashlib.sha256(path.read_bytes()).hexdigest()


def publish_hf(files, run_id):
    token = UserSecretsClient().get_secret("HF_TOKEN")
    repo_id = os.getenv("HF_REPO_ID", CONFIG["hf_repo_id"]); api = HfApi(token=token)
    api.create_repo(repo_id, repo_type="dataset", private=True, exist_ok=True)
    operations = [CommitOperationAdd(path_in_repo=remote, path_or_fileobj=str(local)) for local, remote in files]
    return api.create_commit(repo_id, repo_type="dataset", operations=operations, commit_message=f"chapter images demo {run_id}").oid


def main():
    if not torch.cuda.is_available(): raise RuntimeError("沒有偵測到 GPU；請確認 Kaggle Kernel 已設定 GPU")
    timings = {"bootstrap_dependencies_seconds": round(time.perf_counter()-MODULE_START, 3)}
    OUTPUT_ROOT.mkdir(parents=True, exist_ok=True); chapters = load_chapters()
    run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    started = time.perf_counter()
    quant = BitsAndBytesConfig(load_in_4bit=True, bnb_4bit_compute_dtype=torch.float16)
    tokenizer = AutoTokenizer.from_pretrained(CONFIG["prompt_model"])
    model = AutoModelForCausalLM.from_pretrained(CONFIG["prompt_model"], quantization_config=quant, device_map="auto", low_cpu_mem_usage=True)
    timings["text_model_load_seconds"] = round(time.perf_counter()-started, 3)
    boards, chapter_rows = [], []
    for item in chapters:
        if item["status"] != "ready_for_analysis":
            chapter_rows.append({"book_id": item["book_id"], "chapter": item["chapter"], "title": item["title"], "source_sha256": item["source_sha256"], "effective_chars": item["effective_chars"], "status": item["status"], "scene_paths": []}); continue
        chapter_started = time.perf_counter(); analysis = analyze_chapter(item, model, tokenizer)
        timings[f"chapter_{item['chapter']:04d}_analysis_seconds"] = round(time.perf_counter()-chapter_started, 3)
        (OUTPUT_ROOT/f"analysis_{item['chapter']:04d}.json").write_text(json.dumps(analysis, ensure_ascii=False, indent=2), encoding="utf-8")
        selection = select_pair(analysis, item)
        if selection["status"] != "selected":
            chapter_rows.append({"book_id": item["book_id"], "chapter": item["chapter"], "title": item["title"], "source_sha256": item["source_sha256"], "effective_chars": item["effective_chars"], "status": selection["status"], "scene_paths": []}); continue
        compose_started = time.perf_counter(); board = compose_storyboard(item, selection, model, tokenizer); boards.append(board)
        timings[f"chapter_{item['chapter']:04d}_compose_seconds"] = round(time.perf_counter()-compose_started, 3)
        (OUTPUT_ROOT/f"storyboard_{item['chapter']:04d}.json").write_text(json.dumps(board, ensure_ascii=False, indent=2), encoding="utf-8")
    del model, tokenizer; gc.collect(); torch.cuda.empty_cache(); image_model_started = time.perf_counter(); pipe = load_image_pipeline()
    timings["image_model_load_seconds"] = round(time.perf_counter()-image_model_started, 3)
    image_rows, uploads, root = [], [], CONFIG["hf_version_root"]
    for board in boards:
        scene_paths = []
        for scene in board["scenes"]:
            number, index = board["chapter"], scene["scene"]; folder = OUTPUT_ROOT/f"chapter_{number:04d}"; folder.mkdir(exist_ok=True)
            output = folder/f"scene_{index:02d}.webp"; seed = CONFIG["seed"] + number*10 + index
            if not output.exists():
                image_started = time.perf_counter()
                image = pipe(prompt=scene["prompt"], negative_prompt=scene["negative_prompt"], width=CONFIG["width"], height=CONFIG["height"], num_inference_steps=CONFIG["inference_steps"], guidance_scale=CONFIG["guidance_scale"], generator=torch.Generator(device="cuda").manual_seed(seed)).images[0]
                image.save(output, "WEBP", quality=93, method=6)
                timings[f"chapter_{number:04d}_scene_{index:02d}_generation_seconds"] = round(time.perf_counter()-image_started, 3)
            remote = f"{root}/images/chapter_{number:04d}/scene_{index:02d}.webp"; scene_paths.append(remote); uploads.append((output, remote))
            image_rows.append({"book_id": board["book_id"], "chapter": number, "scene": index, "relative_path": remote, "status": CONFIG["publish_status"], "source_event_id": scene["source_event_id"], "character_versions": scene["character_versions"], "width": CONFIG["width"], "height": CONFIG["height"], "sha256": file_sha(output), "source_sha256": board["source_sha256"], "prompt_template_version": CONFIG["prompt_template_version"], "model_revisions": {"text": CONFIG["prompt_model"], "image": CONFIG["image_base_model"], "acceleration": CONFIG["image_acceleration_model"]}, "seed": seed})
        chapter_rows.append({"book_id": board["book_id"], "chapter": board["chapter"], "title": board["title"], "source_sha256": board["source_sha256"], "status": CONFIG["publish_status"], "scene_paths": scene_paths})
        uploads.append((OUTPUT_ROOT/f"storyboard_{board['chapter']:04d}.json", f"{root}/storyboards/chapter_{board['chapter']:04d}.json"))
    manifests = OUTPUT_ROOT/"manifests"; manifests.mkdir(exist_ok=True)
    for name, rows in (("chapters.jsonl", chapter_rows), ("images.jsonl", image_rows)):
        path = manifests/name; path.write_text("".join(json.dumps(row, ensure_ascii=False)+"\n" for row in sorted(rows, key=lambda x:(x["chapter"],x.get("scene",0)))), encoding="utf-8"); uploads.append((path, f"{root}/manifests/{name}"))
    uploads += [(input_asset("characters.json"), f"{root}/characters/characters.json"), (input_asset("style_bible.json"), f"{root}/style_bible.json")]
    latest = OUTPUT_ROOT/"latest.json"; latest.write_text(json.dumps({"version":"v1","status":"needs_review","updated_at":run_id}, indent=2), encoding="utf-8"); uploads.append((latest,"chapter_images/latest.json"))
    commit = publish_hf(uploads, run_id) if CONFIG.get("publish_from_kaggle") else None
    timings["kernel_total_seconds"] = round(time.perf_counter()-MODULE_START, 3)
    run_report = {"run_id":run_id,"hf_repo":CONFIG["hf_repo_id"],"hf_commit":commit,
                  "chapters":len(chapter_rows),"images":len(image_rows),"status":"generated","timings":timings}
    (OUTPUT_ROOT/"run_report.json").write_text(json.dumps(run_report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(run_report, ensure_ascii=False))


if __name__ == "__main__": main()
