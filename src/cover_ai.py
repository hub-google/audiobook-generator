"""Strict Gemini cover drafting and review, independent of cover file I/O.

Public functions remain re-exported from metadata_gen for existing callers.
HTTP transport and timing use the shared requests/time modules, preserving
existing tests while keeping the API code behind one service boundary.
"""
import json
import logging
import os
import re
import time

import requests
from .cover_template import build_cover_prompt, validate_visual_brief, COVER_TEMPLATE_VERSION


def finalize_cover_prompt(visual_brief):
    """Only structured story variables may enter the fixed viral template."""
    return build_cover_prompt(visual_brief)


def _validate_plot_source(book_title, text, source):
    cleaned = re.sub(r"\s+", " ", str(text or "")).strip()
    rejected = ("可以指", "消歧義", "同名", "公司、遊戲", "disambiguation")
    if len(cleaned) < 45:
        raise ValueError(f"《{book_title}》封面資料不足：{source} 簡介只有 {len(cleaned)} 字")
    if any(marker.lower() in cleaned.lower() for marker in rejected):
        raise ValueError(f"《{book_title}》封面資料錯誤：{source} 回傳同名／消歧義內容")
    return cleaned



def generate_gemini_art_prompt(book_title, pure_plot, max_attempts=4, retry_base_seconds=15):
    """
    使用 Google AI Studio Gemini 藝術總監引擎，分析全本小說名稱與完整劇情簡介，
    產出高質感、鮮豔震撼、具備核心主角與靈魂法寶/特徵的 8K 影視級英文 AI 生圖提示詞 (Art Prompt)。
    """
    api_key = os.getenv("GEMINI_API_KEY")
    if not api_key:
        raise ValueError("❌ [CRITICAL] 缺少 GEMINI_API_KEY！無法啟動 Gemini LLM 藝術總監引擎，流程中斷。")

    logging.info(f"🎨 啟動 Gemini 藝術總監引擎分析《{book_title}》劇情與視覺風格...")
    
    # URL will be generated dynamically per attempt
    
    sys_instruction = (
        "You are a world-class Hollywood concept artist and master book-cover art director. "
        "Carefully analyze the given book title and synopsis to create a breathtaking, ultra-vibrant, 16:9 cinematic 8K concept art prompt in English.\n"
        "IMPORTANT RULES:\n"
        "1. DYNAMIC STORY ANALYSIS: Fully analyze the unique setting, era, genre, core characters, iconic artifacts/objects, atmosphere, and major plot points from the provided title and synopsis. Customize every visual detail specifically for this book.\n"
        "2. HIGH-AESTHETIC CHARACTER DESIGN: If the scene features a character, they MUST be exceptionally attractive and stylish according to mainstream visual standards (e.g. extremely handsome/stunningly gorgeous face, sharp refined facial features, idol-level facial structure, photogenic, high-end fashionable attire appropriate for the story's setting). NEVER generate plain, ordinary, or generic characters.\n"
        "3. DYNAMIC FOCAL POINT & ENVIRONMENT: Feature the central hero or primary story conflict in a compelling 16:9 focal composition with atmospheric cinematic lighting.\n"
        "4. VIBRANT COLORS & LIGHTING: Use vibrant, vivid color contrasts and dynamic lighting matching the story mood instead of dull, dark, or muddy colors.\n"
        "5. QUALITY & BEAUTY KEYWORDS: MUST include: 'extremely handsome facial features, high aesthetic appealing face, hyperrealistic 8k resolution, crisp focus, epic cinematic lighting, masterpiece composition, highly detailed, trending on ArtStation'.\n"
        "6. DO NOT include any text, title letters, logos, watermarks, or signatures in the visual.\n"
        "7. Output ONLY the raw English prompt text, without any intro/outro, quotes, or markdown backticks."
    )
    
    user_prompt = f"Book Title: 《{book_title}》\nSynopsis:\n{pure_plot}\n\nPlease generate a breathtaking cinematic masterpiece concept art prompt in English."
    
    payload = {
        "contents": [{
            "parts": [{"text": sys_instruction + "\n\n" + user_prompt}]
        }]
    }
    
    # 乒乓交替嘗試
    max_total_attempts = max_attempts
    last_error = None

    for attempt in range(1, max_total_attempts + 1):
        # 奇數次用 latest，偶數次用 3.5-flash
        current_model = "gemini-flash-latest" if attempt % 2 != 0 else "gemini-3.5-flash"
        url = f"https://generativelanguage.googleapis.com/v1beta/models/{current_model}:generateContent?key={api_key}"
        
        try:
            res = requests.post(url, json=payload, timeout=30)
            if res.status_code == 200:
                candidates = res.json().get("candidates", [])
                if candidates and "content" in candidates[0]:
                    prompt_text = candidates[0]["content"]["parts"][0]["text"].strip()
                    prompt_text = re.sub(r"^```[a-zA-Z]*\n?", "", prompt_text).rstrip("`").strip()
                    prompt_text = prompt_text.strip('"').strip("'")
                    logging.info(f"✨ Gemini 藝術總監產出提示詞成功 (使用 {current_model}): {prompt_text[:120]}...")
                    return prompt_text
                last_error = "HTTP 200 但回應中沒有可用的提示詞"
            else:
                last_error = f"HTTP {res.status_code}: {res.text[:300]}"
                
        except requests.RequestException as exc:
            last_error = f"網路錯誤: {exc}"
        except Exception as exc:
            last_error = f"例外錯誤: {exc}"

        if attempt == max_total_attempts:
            break
            
        # 每一輪（2次嘗試）遞增等待時間: 15, 15, 30, 30, 60, 60 (以 retry_base_seconds=15 為例)
        delay = retry_base_seconds * (2 ** ((attempt - 1) // 2))
        next_model = "gemini-3.5-flash" if attempt % 2 != 0 else "gemini-flash-latest"
        logging.warning(
            f"⚠️ 第 {attempt}/{max_total_attempts} 次 ({current_model}) 失敗: {last_error}；"
            f"等待 {delay} 秒後，切換至 {next_model} 重試..."
        )
        time.sleep(delay)

    raise RuntimeError(
        f"❌ Gemini 藝術總監 Prompt 生成失敗，已交替重試 {max_total_attempts} 次全滅；流程中止: {last_error}"
    )


def generate_gemini_book_introduction(book_title, max_attempts=4, retry_base_seconds=15):
    """Ask ordinary Gemini to introduce a book before the strict cover passes."""
    api_key = os.getenv("GEMINI_API_KEY")
    if not api_key:
        raise ValueError("缺少 GEMINI_API_KEY")
    instruction = f"""你是熟悉中文網路小說的編輯。請直接使用模型內建知識介紹指定小說《{book_title}》，供後續封面分析使用。
這一步沒有外部簡介；不要要求呼叫端補資料，也不要使用任何搜尋工具。請辨識這部作品並提供具體內容，禁止只依書名寫通用玄幻描述。
只回傳有效 JSON，固定格式：
{{"book_title":"{book_title}","author":"作者或未確定","genre":"具體類型","synopsis":"完整故事介紹","protagonist":"主角姓名、身分與處境","world_setting":"世界觀與時代","core_plot":"核心劇情與主要衝突","iconic_elements":["代表場景、能力、物件或人物"],"avoid_errors":["容易與其他作品混淆或不應畫入的內容"]}}
synopsis 必須具體說明主角、世界觀、故事開端與核心衝突；iconic_elements 至少三項。不得加入 markdown。"""
    errors = []
    models = ("gemini-flash-latest", "gemini-3.5-flash")
    for attempt in range(1, max_attempts + 1):
        model = models[(attempt - 1) % len(models)]
        try:
            response = requests.post(
                f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={api_key}",
                json={
                    "contents": [{"parts": [{"text": instruction}]}],
                    "generationConfig": {"responseMimeType": "application/json"},
                },
                timeout=60,
            )
            if response.status_code != 200:
                try:
                    message = (response.json().get("error") or {}).get("message") or response.text[:300]
                except ValueError:
                    message = response.text[:300]
                raise RuntimeError(f"Gemini HTTP {response.status_code}: {message}")
            text = response.json()["candidates"][0]["content"]["parts"][0]["text"].strip()
            result = json.loads(re.sub(r"^```(?:json)?\s*|\s*```$", "", text, flags=re.IGNORECASE))
            required = ("synopsis", "protagonist", "world_setting", "core_plot")
            if any(len(str(result.get(key) or "").strip()) < 12 for key in required):
                raise ValueError("Gemini 書本介紹缺少具體故事欄位")
            if len(result.get("iconic_elements") or []) < 3:
                raise ValueError("Gemini 書本介紹缺少三項代表元素")
            introduction = (
                f"書名：《{book_title}》；作者：{result.get('author') or '未確定'}；"
                f"類型：{result.get('genre') or '未確定'}；故事簡介：{result['synopsis']}；"
                f"主角：{result['protagonist']}；世界觀：{result['world_setting']}；"
                f"核心劇情：{result['core_plot']}；代表元素："
                f"{'、'.join(map(str, result['iconic_elements']))}；"
                f"避免畫錯：{'、'.join(map(str, result.get('avoid_errors') or []))}"
            )
            return _validate_plot_source(book_title, introduction, "Gemini 書本介紹")
        except Exception as exc:
            errors.append(f"{model}: {exc}")
            if attempt < max_attempts:
                delay = retry_base_seconds * (2 ** ((attempt - 1) // len(models)))
                logging.warning(
                    "Gemini 書本介紹失敗（第 %d/%d 次，%s）：%s；%d 秒後重試",
                    attempt, max_attempts, model, exc, delay,
                )
                time.sleep(delay)
    raise RuntimeError("Gemini 書本介紹失敗：" + " | ".join(errors))


def generate_gemini_cover_information(book_title, pure_plot, research=None, max_attempts=4, retry_base_seconds=15):
    """One on-demand Gemini call returning both the readable brief and HF prompt."""
    api_key = os.getenv("GEMINI_API_KEY")
    if not api_key:
        raise ValueError("缺少 GEMINI_API_KEY")
    _validate_plot_source(book_title, pure_plot, "送交 Gemini 的簡介")
    research = research or {"mode": "internal_knowledge_fallback", "sources": []}
    research_json = json.dumps(research, ensure_ascii=False)
    instruction = f"""你是熟悉中文網路小說的考據編輯。請依提供的聯網資料與目錄頁身分，嚴格分析《{book_title}》。你只負責填入故事變數，無權改變固定的熱門短劇縮圖構圖。
目錄頁身分與簡介：{pure_plot}
資料模式與來源：{research_json}
只分析指定小說的原著版本。不得混入動畫、漫畫、遊戲、影視改編或其他同名作品新增、修改或特有的角色造型、場景、武器與設定；若改編內容與小說原著不同，一律以小說原著為準。不確定時必須回 insufficient_source，不得猜測。
只回傳有效 JSON，格式：
{{"status":"ok或insufficient_source","story_facts":[{{"fact":"本書具體事實1","source_ids":["S1"]}}],"analysis":{{"故事類型與時代":"...","世界觀":"...","主角外觀與身分":"...","代表性場景":"...","法寶武器或關鍵物件":"...","色彩氣氛與構圖":"...","應避免畫錯的內容":"..."}},"visual_brief":{{"genre":"英文題材","era_and_setting":"英文時代與場景","core_conflict":"英文核心衝突","main_character_identity":"英文主角身分","appearance":"英文外觀","clothing":"英文服裝","expression_and_action":"英文表情與動作","supporting_characters":["英文輔角一","英文輔角二"],"iconic_story_symbol":"英文巨大故事識別符號","iconic_prop_or_power":"英文代表物件或能力","genre_color_palette":"英文題材色盤","lighting_and_mood":"英文光線氣氛","avoid_story_errors":["英文禁畫錯誤"]}}}}
至少五條 story_facts 必須是這一本小說的具體事實，且至少包含主角姓名、核心世界觀、代表場景與代表物件。每條 fact 的 source_ids 都不得為空：來自資料來源時使用其 S 編號，來自模型內建知識時使用 "MODEL_KNOWLEDGE"，同時受兩者支持時可同時列出。web_evidence 模式不得使用 MODEL_KNOWLEDGE；hybrid_web_and_model_knowledge 與 internal_knowledge_fallback 模式可使用資料中已有的 S 編號及 MODEL_KNOWLEDGE。不得捏造來源編號，不確定就必須 insufficient_source。
宣傳文案中的任何誇飾或比喻不得直接畫成法寶或實體事件，除非資料明確證實它是故事中的真實代表物件。
status=ok 時，各分析欄位及 visual_brief 不得填未知、未提供、無法判斷或通用抽象方案。supporting_characters 只能是 0 至 2 項。iconic_story_symbol 必須是資料支持且一眼可辨識的巨大人物、物件、生物、建築或環境標誌，不能只寫神秘力量。不得要求留白、文字安全區、遠景小人物、大片天空或極簡構圖。"""
    errors = []
    models = ("gemini-flash-latest", "gemini-3.5-flash")
    if os.environ.get("COVER_REVIEW_OUTPUT"):
        os.makedirs(os.environ["COVER_REVIEW_OUTPUT"], exist_ok=True)
        with open(os.path.join(os.environ["COVER_REVIEW_OUTPUT"], "gemini_analysis_prompt.txt"), "w", encoding="utf-8") as handle:
            handle.write(instruction)
    for attempt in range(1, max_attempts + 1):
        model = models[(attempt - 1) % len(models)]
        try:
            response = requests.post(
                f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={api_key}",
                json={"contents": [{"parts": [{"text": instruction}]}], "generationConfig": {"responseMimeType": "application/json"}},
                timeout=60,
            )
            if response.status_code != 200:
                try:
                    api_error = (response.json().get("error") or {}).get("message") or response.text[:300]
                except ValueError:
                    api_error = response.text[:300]
                raise RuntimeError(f"Gemini HTTP {response.status_code}: {api_error}")
            candidate = response.json()["candidates"][0]
            text = candidate["content"]["parts"][0]["text"].strip()
            text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text, flags=re.IGNORECASE)
            result = json.loads(text)
            if result.get("status") != "ok":
                raise ValueError("Gemini 判定來源不足，拒絕產生 Prompt")
            analysis = result.get("analysis")
            required = {"故事類型與時代", "世界觀", "主角外觀與身分", "代表性場景", "法寶武器或關鍵物件", "色彩氣氛與構圖", "應避免畫錯的內容"}
            if not isinstance(analysis, dict) or not required.issubset(analysis):
                raise ValueError("Gemini JSON 缺少必要分析欄位")
            bad_words = ("未知", "未提供", "未提及", "無法判斷", "查無", "通用")
            if any(not str(analysis[key]).strip() or any(word in str(analysis[key]) for word in bad_words) for key in required):
                raise ValueError("Gemini 分析包含未知或通用敷衍內容")
            facts = result.get("story_facts") or []
            if len(facts) < 5 or any(not isinstance(item, dict) or len(str(item.get("fact") or "").strip()) < 8 for item in facts[:5]):
                raise ValueError("Gemini 沒有提供五條具體故事事實")
            valid_source_ids = {item["id"] for item in research.get("sources") or []}
            mode = research.get("mode")
            for item in facts:
                ids = set(item.get("source_ids") or [])
                if mode == "internal_knowledge_fallback":
                    if not ids or not ids.issubset(valid_source_ids | {"MODEL_KNOWLEDGE"}):
                        raise ValueError("內建知識備援使用了空白或無效來源編號")
                elif mode == "hybrid_web_and_model_knowledge":
                    if not ids or not ids.issubset(valid_source_ids | {"MODEL_KNOWLEDGE"}):
                        raise ValueError("混合資料模式使用了無效來源編號")
                elif not ids or not ids.issubset(valid_source_ids):
                    raise ValueError("Gemini 故事事實缺少有效聯網來源編號")
            visual_brief = validate_visual_brief(result.get("visual_brief"))
            result["prompt"] = finalize_cover_prompt(visual_brief)
            result["template_version"] = COVER_TEMPLATE_VERSION
            result["research"] = research
            return result
        except Exception as exc:
            errors.append(f"{model}: {exc}")
            if attempt < max_attempts:
                delay = retry_base_seconds * (2 ** ((attempt - 1) // len(models)))
                logging.warning(
                    "Gemini 封面資訊失敗（第 %d/%d 次，%s）：%s；%d 秒後重試",
                    attempt, max_attempts, model, exc, delay,
                )
                time.sleep(delay)
    raise RuntimeError("Gemini 封面資訊產生失敗；已停止且未產生 Prompt：" + " | ".join(errors))


def review_cover_information(book_title, pure_plot, research, draft, max_attempts=4, retry_base_seconds=15):
    """A separate Gemini pass must audit and rewrite the draft before acceptance."""
    api_key = os.getenv("GEMINI_API_KEY")
    prompt = f"""你是嚴格的小說考據編輯與熱門縮圖規格審核員。請審核下列《{book_title}》封面草稿，使用你對該小說的知識以及提供的資料逐項糾錯，最後只輸出完整 JSON。
目錄身分：{pure_plot}
資料：{json.dumps(research, ensure_ascii=False)}
草稿：{json.dumps(draft, ensure_ascii=False)}
輸出格式與草稿相同，status 只能 ok 或 insufficient_source。只審核指定小說的原著版本，不得混入動畫、漫畫、遊戲、影視改編或其他同名作品新增、修改或特有的設定；若改編內容與小說原著不同，一律以小說原著為準。刪除所有無根據的年齡、髮型、服裝、武器與場景；辨識宣傳文案中的誇飾與比喻，不得把比喻直接畫成實體法寶或事件。只採用該作品有資料支持、公認且具辨識度的角色、地點與物件，不確定就回 insufficient_source。保留至少五條 story_facts 及合法來源編號。visual_brief 必須完整、最多兩位輔角，且不得要求遠景小人物、大片留白、文字安全區或極簡構圖。不要輸出或改寫最終英文 prompt，Python 會套用固定模板。"""
    errors = []
    models = ("gemini-flash-latest", "gemini-3.5-flash")
    for attempt in range(1, max_attempts + 1):
        model = models[(attempt - 1) % len(models)]
        try:
            response = requests.post(
                f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={api_key}",
                json={"contents": [{"parts": [{"text": prompt}]}], "generationConfig": {"responseMimeType": "application/json"}},
                timeout=60,
            )
            if response.status_code != 200:
                message = (response.json().get("error") or {}).get("message") or response.text[:300]
                raise RuntimeError(f"Gemini HTTP {response.status_code}: {message}")
            text = response.json()["candidates"][0]["content"]["parts"][0]["text"].strip()
            result = json.loads(re.sub(r"^```(?:json)?\s*|\s*```$", "", text, flags=re.IGNORECASE))
            if result.get("status") != "ok" or len(result.get("story_facts") or []) < 5:
                raise ValueError("審核器拒絕草稿或缺少五條事實")
            valid_ids = {item["id"] for item in research.get("sources") or []} | {"MODEL_KNOWLEDGE"}
            for fact in result.get("story_facts") or []:
                ids = set(fact.get("source_ids") or []) if isinstance(fact, dict) else set()
                if not isinstance(fact, dict) or not fact.get("fact") or not ids or not ids.issubset(valid_ids):
                    raise ValueError("審核後故事事實缺少合法來源")
            analysis = result.get("analysis") or {}
            if len(analysis) < 7 or any(word in json.dumps(analysis, ensure_ascii=False) for word in ("未知", "未提供", "通用")):
                raise ValueError("審核後分析仍不完整")
            visual_brief = validate_visual_brief(result.get("visual_brief"))
            final_prompt = finalize_cover_prompt(visual_brief)
            if re.search(r"(?:blade|weapon|sword).{0,20}(?:grass|dust)|(?:grass|dust).{0,20}(?:blade|weapon|sword)", final_prompt, re.I):
                raise ValueError("審核後仍把宣傳比喻誤畫成實體武器")
            result["prompt"] = final_prompt
            result["template_version"] = COVER_TEMPLATE_VERSION
            result["research"] = research
            return result
        except Exception as exc:
            errors.append(f"{model}: {exc}")
            if attempt < max_attempts:
                delay = retry_base_seconds * (2 ** ((attempt - 1) // len(models)))
                logging.warning(
                    "Gemini 二次品質審核失敗（第 %d/%d 次，%s）：%s；%d 秒後重試",
                    attempt, max_attempts, model, exc, delay,
                )
                time.sleep(delay)
    raise RuntimeError("Gemini 二次品質審核失敗；已停止且未產生 Prompt：" + " | ".join(errors))


