from __future__ import annotations

import csv
import math
import os
import re
import sys
import threading

from collections import Counter, defaultdict
from dataclasses import dataclass, field
from pathlib import Path

from typing import Callable, Optional

ENCODINGS = ("utf-8-sig", "utf-8", "cp950", "big5", "gb18030")

AD_HINTS = (
    # 廣告與推薦套話 (含簡繁體)
    "喜歡這部作品", "喜欢这部作品", "如果您喜歡", "如果您喜欢", "歡迎您來", "欢迎您来",
    "最大動力", "最大的動力", "最大动力", "最大的动力", "您的支持", "我的動力", "我的动力",
    "加入書架", "加入书架", "方便閱讀", "方便阅读", "方便以后閱讀", "方便以后阅读",
    "最新章節", "最新章节", "免費閱讀", "免费阅读", "免費小說", "免费小说",
    "手機閱讀", "手机阅读", "手機訪問", "手机访问", "手機黨", "手机党",
    "推薦票", "推荐票", "投推薦", "投推荐", "投月票", "月票", "打賞", "打赏",
    "訂閱", "订阅", "收藏", "關注", "关注", "追蹤", "追踪", "下載", "下载",
    "掃碼", "扫码", "公眾號", "公众号", "微信", "qq群", "交流圈", "書友群", "书友群", "讀者群", "读者群",
    "搜尋", "搜索", "首發", "首发", "更新最快", "廣告", "广告", "黃金屋", "黄金屋",
    "本站域名", "域名", "請記住", "请记住", "無彈窗", "无弹窗", "全文字", "手打",
    "轉載請保留", "转载请保留", "百度搜索", "百度搜尋", "本站提供", "整理提供", "發表原創", "发表原创",
    "分享給", "分享给", "貼吧", "贴吧", "更新貼", "更新贴", "主貼", "主贴", "回帖",
    "頂貼", "顶贴", "頂(贊)", "顶(赞)", "點贊", "点赞", "樓主", "楼主", "搶樓", "抢楼",
    "頂樓", "顶楼", "求粉", "求粉必刪", "起點", "起点", "qidian", "創世", "创世",
    "縱橫", "纵横", "17k", "晉江", "晋江", "網址", "网址", "網站", "网站", "官網", "官网",
    "http", "https", "www", ".com", ".net", ".org", ".tw", ".cn", ".cc", ".me"
)

URL_RE = re.compile(r"(?:https?://|www\.|[a-zA-Z0-9-]+\.(?:com|net|org|tw|cn|cc|me|cm|om|co))(?:\S*)", re.I)
OBFUSCATED_DOMAIN_RE = re.compile(r"[a-zA-Z0-9]{8,}\.[a-zA-Z0-9]{2,}\b")
SPACE_RE = re.compile(r"\s+")
DIALOGUE_RE = re.compile(r'^(?:[“"「『][^”"」』]{1,15}[”"」』][。！？!?,，]?)$')
ONOMATOPOEIA_RE = re.compile(r"^[！!。…\s]*[轟轰啪砰哈啊呀哦呵嘻嗤呼咚鏘锵鐺铛]+[！!。…\s]*$")
COMMON_CONNECTIVES = {"但是現在", "可是現在", "不過現在", "與此同時", "與此同時，", "與此同時,", "想到這裡", "想到这里", "下一章", "未完待續", "未完待续"}
CLAUSE_SPLIT_RE = re.compile(r"[\r\n，,。！？!?；;、…—:：\"'“”‘’\(\)（）\[\]【】\s]+")


def extract_balanced_brackets(s: str) -> list[str]:
    """提取最外層成對括號 (支援內部巢狀與全形半形括號)"""
    open_brackets = set("([【（{")
    close_map = {')': '(', '）': '（', ']': '[', '】': '【', '}': '{'}
    results = []
    stack = []
    start = -1
    for i, ch in enumerate(s):
        if ch in open_brackets:
            if not stack:
                start = i
            stack.append(ch)
        elif ch in close_map:
            if stack and stack[-1] == close_map[ch]:
                stack.pop()
                if not stack:
                    results.append(s[start:i + 1])
                    start = -1
    return results



@dataclass
class Result:
    kind: str           # 類型：章節開頭廣告, 章節結尾廣告, 完整重複行, 網址網域, 高頻片語
    text: str           # 疑似詞句
    count: int          # 出現次數
    spread: int         # 涵蓋章節或區段數
    score: float        # 可疑度分數
    reason: str         # 判定理由
    samples: list[str] = field(default_factory=list)  # 前後文範例
    enabled: bool = True  # 是否勾選去除 (True=去除, False=保留)


def read_text_from_file(path: Path) -> tuple[str, str]:
    data = path.read_bytes()
    for encoding in ENCODINGS:
        try:
            return data.decode(encoding), encoding
        except UnicodeDecodeError:
            pass
    return data.decode("utf-8", errors="replace"), "utf-8（含替代字元）"


def normalize_line(line: str) -> str:
    return SPACE_RE.sub(" ", line).strip()


class NovelAdDetector:
    """
    Intelligent Novel Ad / Spam Phrase Detector.
    Accurately detects injected ads, watermarks, header/footer spam, and repetitive phrases.
    """

    def __init__(
        self,
        min_count: int = 4,
        min_len: int = 4,
        max_len: int = 16,
        progress_cb: Optional[Callable[[float, str], None]] = None,
    ):
        self.min_count = min_count
        self.min_len = min_len
        self.max_len = max_len
        self.progress_cb = progress_cb or (lambda pct, msg: None)

    def analyze_source(self, target_path: Path) -> list[Result]:
        if target_path.is_dir():
            return self._analyze_directory(target_path)
        elif target_path.is_file():
            return self._analyze_single_file(target_path)
        else:
            raise FileNotFoundError(f"找不到路徑: {target_path}")

    def _analyze_directory(self, folder: Path) -> list[Result]:
        txt_files = sorted(folder.glob("*.txt"), key=lambda p: [int(x) if x.isdigit() else x for x in re.split(r"(\d+)", p.stem)])
        if not txt_files:
            raise ValueError(f"目錄 {folder} 中沒有找不到 .txt 檔案")

        self.progress_cb(5.0, f"正在讀取 {len(txt_files)} 個章節檔案...")
        chapters_lines: list[list[str]] = []
        full_text_chunks: list[str] = []

        total_files = len(txt_files)
        for i, f in enumerate(txt_files):
            text, _ = read_text_from_file(f)
            lines = [normalize_line(ln) for ln in text.splitlines() if normalize_line(ln)]
            chapters_lines.append(lines)
            full_text_chunks.append(text)
            if i % 100 == 0:
                self.progress_cb(5.0 + (i / total_files) * 20.0, f"已載入 {i}/{total_files} 章...")

        full_text = "\n\n".join(full_text_chunks)
        return self._detect(chapters_lines, full_text, total_units=len(txt_files))

    def _analyze_single_file(self, file_path: Path) -> list[Result]:
        self.progress_cb(5.0, f"正在讀取檔案 {file_path.name}...")
        full_text, enc = read_text_from_file(file_path)

        self.progress_cb(15.0, "解析章節與段落結構...")
        chapter_split_pattern = re.compile(
            r"(?:^|\n)(?:={10,}.*?第\s*\d+\s*章.*?(?:={10,}|\n)|(?:第[0-9一二三四五六七八九十百千萬]+章[^\n]*))",
            re.M
        )
        parts = chapter_split_pattern.split(full_text)
        if len(parts) > 10:
            chapters_lines = [[normalize_line(ln) for ln in p.splitlines() if normalize_line(ln)] for p in parts if p.strip()]
        else:
            all_lines = [normalize_line(ln) for ln in full_text.splitlines() if normalize_line(ln)]
            chunk_size = 100
            chapters_lines = [all_lines[i:i + chunk_size] for i in range(0, len(all_lines), chunk_size)]

        return self._detect(chapters_lines, full_text, total_units=max(len(chapters_lines), 20))

    def _detect(self, chapters_lines: list[list[str]], full_text: str, total_units: int) -> list[Result]:
        self.progress_cb(30.0, "統計跨章節高頻獨立行與章首章尾特徵...")
        results: list[Result] = []

        line_chapter_map: dict[str, set[int]] = defaultdict(set)
        line_count: Counter[str] = Counter()
        head_lines: Counter[str] = Counter()
        tail_lines: Counter[str] = Counter()

        # 1. Whole line stats (Strictly limited to concise lines <= 80 chars)
        for c_idx, lines in enumerate(chapters_lines):
            num_lines = len(lines)
            for l_idx, line in enumerate(lines):
                # Never classify lines longer than 80 chars as whole-line ads (protect paragraphs)
                if len(line) < 2 or len(line) > 80:
                    continue
                line_count[line] += 1
                line_chapter_map[line].add(c_idx)
                if l_idx < 4:
                    head_lines[line] += 1
                elif l_idx >= num_lines - 4:
                    tail_lines[line] += 1

        self.progress_cb(45.0, "識別固定廣告行 (排除小說正文段落)...")
        for line, count in line_count.items():
            spread = len(line_chapter_map[line])

            # Dialogue like "“嗯。”", "“好的。”" is normal
            if bool(DIALOGUE_RE.match(line)):
                continue
            # Pure separator symbols
            if re.match(r"^[\s=\-_*#~./|\\:+]+$", line):
                continue
            # Exclude onomatopoeia and common connectives if they don't have ad hints
            hinted = any(h in line.lower() for h in AD_HINTS)
            has_url = bool(URL_RE.search(line)) or bool(OBFUSCATED_DOMAIN_RE.search(line))
            if not (hinted or has_url):
                if bool(ONOMATOPOEIA_RE.match(line)) or line in COMMON_CONNECTIVES:
                    continue

            # Whole lines MUST appear in >= 2 distinct chapters to prevent single-line novel text capture
            if spread < 2 or count < 2:
                continue

            is_head = head_lines[line] >= max(2, int(count * 0.40))
            is_tail = tail_lines[line] >= max(2, int(count * 0.40))

            if count >= self.min_count or hinted or has_url or is_head or is_tail:
                kind = "章節開頭廣告" if is_head else ("章節結尾廣告" if is_tail else "完整重複行")
                reasons = []
                score = count * (1.5 + math.log2(spread + 1))

                if is_head:
                    reasons.append("章首固定出現")
                    score *= 2.0
                elif is_tail:
                    reasons.append("章尾固定出現")
                    score *= 1.8

                if hinted:
                    reasons.append("含廣告關鍵字")
                    score += 40
                if has_url:
                    reasons.append("含網址或網域名稱")
                    score += 60
                    kind = "網址網域"

                if not reasons:
                    reasons.append(f"跨 {spread} 章完全重複")

                samples = self._extract_samples(full_text, line, max_samples=3)
                results.append(Result(kind, line, count, spread, score, " / ".join(reasons), samples))

        # 2. Extract In-line & Bracketed & Subclause Repeated Phrases
        self.progress_cb(60.0, "拆分子句與括號廣告 (隔離小說正文)...")
        phrase_counts: Counter[str] = Counter()
        phrase_spread: dict[str, set[int]] = defaultdict(set)
        phrase_in_bracket: Counter[str] = Counter()
        phrase_in_tail: Counter[str] = Counter()

        for c_idx, lines in enumerate(chapters_lines):
            num_lines = len(lines)
            for l_idx, line in enumerate(lines):
                is_tail_line = (l_idx >= num_lines - 5)
                is_head_line = (l_idx < 4)

                # A. Extract balanced bracket blocks (author notes, trailing ads, watermark tags)
                brackets = extract_balanced_brackets(line)
                for b in brackets:
                    b_has_ad = any(h in b.lower() for h in AD_HINTS) or bool(URL_RE.search(b))
                    if b_has_ad or is_tail_line or is_head_line:
                        b_clauses = [
                            c.strip(" \t　，,。！？!?；;、:：()（）[]【】\"'“”‘’._-")
                            for c in CLAUSE_SPLIT_RE.split(b)
                        ]
                        for cl in set(b_clauses):
                            if self.min_len <= len(cl) <= self.max_len:
                                phrase_counts[cl] += 1
                                phrase_spread[cl].add(c_idx)
                                phrase_in_bracket[cl] += 1
                                if is_tail_line:
                                    phrase_in_tail[cl] += 1

                # B. Isolate non-bracket text to prevent bracket ads from polluting sentences
                non_bracket = line
                for b in brackets:
                    non_bracket = non_bracket.replace(b, " ")

                # C. Extract inline URLs / domains directly
                for m in URL_RE.finditer(line):
                    url_str = m.group(0).strip(" \t　，,。！？!?；;、:：()（）[]【】\"'“”‘’._-")
                    if len(url_str) >= 4:
                        phrase_counts[url_str] += 1
                        phrase_spread[url_str].add(c_idx)

                # D. Split non-bracket text into shorter clauses
                line_has_ad = any(h in non_bracket.lower() for h in AD_HINTS)
                if line_has_ad or is_tail_line or is_head_line:
                    clauses = [
                        c.strip(" \t　，,。！？!?；;、:：()（）[]【】\"'“”‘’._-")
                        for c in CLAUSE_SPLIT_RE.split(non_bracket)
                    ]
                    for cl in set(clauses):
                        if self.min_len <= len(cl) <= self.max_len:
                            cl_hint = any(h in cl.lower() for h in AD_HINTS)
                            if cl_hint or is_tail_line or is_head_line:
                                phrase_counts[cl] += 1
                                phrase_spread[cl].add(c_idx)
                                if is_tail_line:
                                    phrase_in_tail[cl] += 1

        self.progress_cb(80.0, "評估短語可疑度與過濾碎片...")
        phrase_candidates: list[Result] = []

        # Known ad and forum specific symbols
        AD_SPECIFIC_SYMBOLS = (
            "www", "http", ".com", ".net", ".org", "qq群", "貼吧", "贴吧", "更新貼", "更新贴",
            "主貼", "主贴", "回帖", "頂貼", "顶贴", "頂(贊)", "顶(赞)", "點贊", "点赞", "樓主", "楼主",
            "搶樓", "抢楼", "頂樓", "顶楼", "求粉", "書友群", "讀者群", "手機閱讀", "手機黨", "手機訪問",
            "首發", "首发", "免費閱讀", "免費小說", "本站域名", "黃金屋", "黄金屋", "加入書架", "最新章節",
            "推薦票", "月票", "投推薦", "打賞", "公眾號", "微信", "刷新速度"
        )

        for phrase, count in phrase_counts.items():
            if count < self.min_count and not (bool(URL_RE.search(phrase)) and count >= 2):
                continue
            if bool(DIALOGUE_RE.match(phrase)):
                continue
            if re.match(r"^[\s=\-_*#~./|\\:+0-9]+$", phrase):
                continue

            # Protect novel onomatopoeia & narrative connectives
            if bool(ONOMATOPOEIA_RE.match(phrase)) or phrase in COMMON_CONNECTIVES:
                continue

            spread = len(phrase_spread[phrase])
            hinted = any(h in phrase.lower() for h in AD_HINTS)
            has_domain = bool(OBFUSCATED_DOMAIN_RE.search(phrase)) or bool(URL_RE.search(phrase)) or bool(re.search(r"[a-zA-Z0-9-]+\.[a-zA-Z0-9]+", phrase))
            has_ad_symbol = any(s in phrase for s in AD_SPECIFIC_SYMBOLS)
            in_bracket = phrase_in_bracket[phrase] >= max(2, int(count * 0.25))
            in_tail = phrase_in_tail[phrase] >= max(2, int(count * 0.25))

            if hinted or has_domain or has_ad_symbol or in_bracket:
                kind = "網址網域" if has_domain else "高頻片語廣告"
                score = count * math.sqrt(len(phrase)) * (1.6 + math.log2(spread + 1))
                reasons = []
                if in_bracket:
                    score += 30
                    reasons.append("位於作者/注釋括號內")
                if in_tail:
                    score += 20
                    reasons.append("常出現在章尾")
                if hinted:
                    score += 40
                    reasons.append("含廣告關鍵字")
                if has_domain or has_ad_symbol:
                    score += 50
                    reasons.append("含網址/論壇標記")
                phrase_candidates.append(Result(kind, phrase, count, spread, score, " / ".join(reasons)))

            elif in_tail and count >= max(self.min_count, 5):
                kind = "高頻片語廣告"
                score = count * math.sqrt(len(phrase)) * (1.2 + math.log2(spread + 1)) + 15
                phrase_candidates.append(Result(kind, phrase, count, spread, score, "常出現在章尾固定結構"))

            elif count >= max(self.min_count * 2, 8) and spread >= 4:
                kind = "內文高頻片語"
                score = count * math.sqrt(len(phrase)) * (0.4 + math.log2(spread + 1))
                phrase_candidates.append(Result(kind, phrase, count, spread, score, "小說常見高頻詞彙", enabled=False))

        # Deduplication: Keep generic core phrases when shorter phrase has higher or equal count
        phrase_candidates.sort(key=lambda r: (-r.score, -r.count, -len(r.text)))
        kept_phrases: list[Result] = []
        for item in phrase_candidates:
            # Check if this item is a substring of another item that has strictly higher or equal count
            redundant = any(
                item.text != other.text and item.text in other.text and other.count >= item.count
                for other in phrase_candidates
            )
            if not redundant:
                item.samples = self._extract_samples(full_text, item.text, max_samples=3)
                kept_phrases.append(item)

        results.extend(kept_phrases)

        # 3. Final Deduplication and Ranking
        self.progress_cb(92.0, "彙整最終排名結果...")
        results = [r for r in results if not re.match(r"^[\s=\-_*#~./|\\:+]+$", r.text)]
        results.sort(key=lambda r: (-r.score, -r.count, -len(r.text)))

        # Remove duplicate identical texts
        seen = set()
        final_list = []
        for r in results:
            if r.text not in seen:
                seen.add(r.text)
                final_list.append(r)

        self.progress_cb(100.0, f"分析完成！共抓出 {len(final_list)} 項疑似廣告詞句。")
        return final_list

    def _extract_samples(self, text: str, phrase: str, max_samples: int = 3) -> list[str]:
        samples = []
        pos = 0
        for _ in range(max_samples):
            idx = text.find(phrase, pos)
            if idx == -1:
                break
            start = max(0, idx - 45)
            end = min(len(text), idx + len(phrase) + 45)
            prefix = ("..." if start > 0 else "") + text[start:idx].replace("\r", "").replace("\n", " ")
            match_word = f"【{text[idx:idx + len(phrase)]}】"
            suffix = text[idx + len(phrase):end].replace("\r", "").replace("\n", " ") + ("..." if end < len(text) else "")
            samples.append(f"{prefix}{match_word}{suffix}")
            pos = idx + len(phrase) + 1
        return samples
