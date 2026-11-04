# -*- coding: utf-8 -*-
"""
GitHub Desktop 繁體中文化 - 互動式 AI 協同翻譯工作台
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
from collections import Counter
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TEMP_DIR = ROOT / "Temp"
ORIG_DIR = ROOT / "Orig"
WIN_DIR = ROOT / "Windows"
BACKUP_DIR = ROOT / "Backups"
USER_ZH = TEMP_DIR / "UserTemp.zh"

PROMPT_FILE = TEMP_DIR / "ai_prompt.txt"
RESPONSE_TXT = TEMP_DIR / "ai_response.txt"
RESPONSE_JSON = TEMP_DIR / "ai_response.json"
CACHE_SENT = TEMP_DIR / ".cache_sent_records.json"

DELIM = ">*.*<"

AI_SYSTEM_INSTRUCTION = """你是一位專業的繁體中文在地化翻譯專家（台灣的軟體與 Git 術語習慣）。
請將以下軟體介面字串翻譯為精準流暢的台灣繁體中文。

翻譯規範：
1. 嚴格遵守台灣 Git / 開發常見用語：
   - Repository -> 儲存庫
   - Commit -> 提交
   - Branch -> 分支
   - Checkout -> 簽出
   - Fetch -> 擷取
   - Pull -> 拉取
   - Push -> 推送
   - Merge -> 合併
   - Rebase -> 變基（重新定基底）
   - Stash -> 暫存
   - Cherry-pick -> 揀選
   - Issue -> 議題
   - Tag -> 標籤
   - Remote -> 遠端
2. 保留代碼變數、佔位符、跳脫符號（例如 ${...}、%s、\\n、& 等快速鍵底線）原樣不動。
3. 請直接以 JSON 陣列格式回傳（最穩定且不易出錯），格式範例：
[
  {
    "original": "英文原文",
    "translation": "繁體中文譯文",
    "category": "分類說明",
    "file": "main.js 或 renderer.js"
  }
]
"""

SKIP_SUBSTR = (
    "http://",
    "https://",
    "github.com",
    "atom://",
    "data:",
    "webpack",
    "${",
    "\\n",
    "rgba(",
    "translate3d",
    "SourceMap",
    "Error.stack",
    "process.",
    "require(",
    "Object.",
    "function",
    "return ",
    "var ",
    "const ",
    "let ",
    "prototype",
    "throw new",
    "typeof ",
    "instanceof",
    "=>",
    "!=",
    "==",
    "===",
    "!==",
    ");",
    "()",
    "{}",
    "[]",
    "undefined",
    "__esModule",
    "exports.",
    "node_modules",
)


def has_chinese(text: str) -> bool:
    """檢查字串是否包含中文字元。"""
    return any("\u4e00" <= c <= "\u9fff" for c in text)


def looks_like_ui_english(t: str) -> bool:
    t_strip = t.strip()
    if len(t_strip) < 3 or len(t_strip) > 200:
        return False
    # 若以 & 開頭，必須是選單快速鍵 (例如 &File, &Edit)，不得為 && 或實體
    if t_strip.startswith("&"):
        if not re.match(r"^&[a-zA-Z]", t_strip):
            return False
    elif not t_strip[0].isalnum():
        return False
    if not any(c.isalpha() for c in t_strip):
        return False
    if any(ord(c) > 127 for c in t_strip):
        return False
    if any(s in t_strip for s in SKIP_SUBSTR):
        return False
    if re.search(r"^[a-zA-Z0-9_\-\.]+$", t_strip):
        return False
    if re.match(r"^[a-z]+[A-Z0-9][a-zA-Z0-9]*$", t_strip) or re.match(r"^[a-z0-9_]+$", t_strip):
        return False
    if re.search(r"\b(px|em|rem|vh|vw|ms|s|deg)\b", t_strip):
        return False
    if t_strip.startswith("/") or t_strip.startswith(".") or t_strip.endswith(".js") or t_strip.endswith(".exe") or t_strip.startswith("application/"):
        return False
    if "monospace" in t_strip or "Software\\\\" in t_strip:
        return False
    if " " not in t_strip and not t_strip.startswith("&"):
        return False
    # 過濾程式邏輯、運算子與正規表示式殘留
    if re.search(r"(&&|\|\||===|!==|=>|\+\+|--)", t_strip):
        return False
    if re.search(r"\b(return|typeof|void|delete|throw|new Error)\b", t_strip):
        return False
    if re.search(r"\bin\s+[a-zA-Z0-9_\.]+(\&\&|\|\||\?|\:|\))", t_strip):
        return False
    if re.search(r"(opts=%|%d|%j|nexttick|id\+\+|input\[type=|read 0|replaceCaret|replaceTilde|Run the trap)", t_strip):
        return False
    if re.search(r"['\":][a-zA-Z0-9_\$]+\.[a-zA-Z0-9_\$]+", t_strip) or re.search(r"\(\[[^\]]+\]\)", t_strip):
        return False
    # 過濾 HTML/CSS 類別列表 (例如 "branch-name-warning branch-name-error", "icon spin")
    if re.match(r"^[a-z0-9\-]+(\s+[a-z0-9\-]+)+$", t_strip) and ("-" in t_strip or re.search(r"\b(spin|blankslate|selectable|expanded|tooltip|item)\b", t_strip)):
        return False
    # 過濾常見日期時間格式字串 (例如 "h:mm:ss a z")
    if re.match(r"^[hHmsaAyYMdpP\s\:\.\,\/zZ\']+$", t_strip) or t_strip in ("eeee 'at' p",):
        return False
    if t_strip in ("use strict", "rune <null>", "&quot;"):
        return False
    return True


def scan_file_strings(path: Path) -> set[str]:
    results = set()
    if not path.exists():
        return results
    text = path.read_text(encoding="utf-8")
    for match in re.finditer(r'"([^"\\]*(?:\\.[^"\\]*)*)"', text):
        s = match.group(1)
        if looks_like_ui_english(s):
            results.add(s)
    return results


def get_all_scanned_strings() -> tuple[list[tuple[str, str, str]], int]:
    """取得 Orig 內所有掃描到的字串，以及其目標檔案。"""
    m_in = ORIG_DIR / "main.js"
    r_in = ORIG_DIR / "renderer.js"
    all_items: list[tuple[str, str, str]] = []
    if not m_in.exists() or not r_in.exists():
        return all_items, 0

    main_strings = scan_file_strings(m_in)
    ren_strings = scan_file_strings(r_in)

    for s in sorted(main_strings):
        all_items.append((s, "主程序介面", "main.js"))
    for s in sorted(ren_strings):
        all_items.append((s, "渲染器介面", "renderer.js"))

    return all_items, len(all_items)


def load_existing_mappings(path: Path) -> dict[str, tuple[str, str, str]]:
    mappings: dict[str, tuple[str, str, str]] = {}
    if not path.exists():
        return mappings
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.rstrip("\r\n")
        if not line.strip() or line.strip().startswith("#"):
            continue
        i = line.find(DELIM)
        if i < 0:
            continue
        source = line[:i]
        rest = line[i + len(DELIM) :]
        parts = rest.split(DELIM, 2)
        if len(parts) == 3:
            mappings[source] = (parts[0], parts[1], parts[2].strip())
    return mappings


def save_mappings(path: Path, mappings: dict[str, tuple[str, str, str]]) -> None:
    lines = []
    for src, (tgt, cat, fn) in mappings.items():
        lines.append(f"{src}{DELIM}{tgt}{DELIM}{cat}{DELIM}{fn}")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def calculate_progress() -> dict[str, int | float]:
    """精確計算中文化進度統計。"""
    mappings = load_existing_mappings(USER_ZH)
    all_scanned, total_scanned = get_all_scanned_strings()

    translated_count = 0
    untranslated_count = 0

    for s, cat, fn in all_scanned:
        if s in mappings and has_chinese(mappings[s][0]):
            translated_count += 1
        else:
            untranslated_count += 1

    pct = (translated_count / total_scanned * 100) if total_scanned > 0 else 100.0
    return {
        "total": total_scanned,
        "translated": translated_count,
        "remaining": untranslated_count,
        "percent": round(pct, 1),
        "dict_total": len(mappings),
    }


def parse_any_ai_content(text: str) -> list[tuple[str, str, str, str]]:
    results: list[tuple[str, str, str, str]] = []
    text_clean = text.strip()

    json_str = None
    if "```json" in text_clean:
        m = re.search(r"```json\s*([\s\S]*?)\s*```", text_clean)
        if m:
            json_str = m.group(1).strip()
    elif "```" in text_clean:
        m = re.search(r"```\s*(\[[\s\S]*?\]|\{[\s\S]*?\})\s*```", text_clean)
        if m:
            json_str = m.group(1).strip()

    if not json_str:
        m = re.search(r"(\[[\s\S]*\]|\{[\s\S]*\})", text_clean)
        if m:
            json_str = m.group(1).strip()

    if json_str:
        try:
            data = json.loads(json_str)
        except Exception as json_err:
            # 容錯處理 1: 清理常見的未跳脫引號問題（如 """ 或未轉義的單獨雙引號）
            cleaned_json = re.sub(r':\s*""",?', ': "\\"",', json_str)
            try:
                data = json.loads(cleaned_json)
            except Exception:
                data = None
                print(f"⚠️ JSON 結構解析失敗 ({json_err})，啟動容錯正則提取機制...", file=sys.stderr)
                # 容錯處理 2: 正則抽取每個 JSON 條目物件
                pattern = re.compile(
                    r'\{\s*"original"\s*:\s*(?:"((?:\\.|[^"\\])*)"|"""\s*,)\s*,\s*'
                    r'"translation"\s*:\s*(?:"((?:\\.|[^"\\])*)"|"""\s*,)',
                    re.DOTALL
                )
                for m_obj in pattern.finditer(json_str):
                    src_val = m_obj.group(1) or '"'
                    tgt_val = m_obj.group(2) or '"'
                    src_val = src_val.encode('utf-8').decode('unicode_escape', errors='replace')
                    tgt_val = tgt_val.encode('utf-8').decode('unicode_escape', errors='replace')
                    if src_val and tgt_val:
                        results.append((src_val, tgt_val.strip(), "AI翻譯", "renderer.js"))
                if results:
                    return results

        if data:
            if isinstance(data, list):
                for item in data:
                    if isinstance(item, dict):
                        src = item.get("original") or item.get("src") or item.get("source") or item.get("en")
                        tgt = item.get("translation") or item.get("translated") or item.get("tgt") or item.get("target") or item.get("zh")
                        cat = item.get("category") or "AI翻譯"
                        fn = item.get("file") or "renderer.js"
                        if src and tgt:
                            results.append((str(src), str(tgt).strip(), str(cat).strip(), str(fn).strip()))
            elif isinstance(data, dict):
                for k, v in data.items():
                    if isinstance(v, str):
                        results.append((k, v.strip(), "AI翻譯", "renderer.js"))
                    elif isinstance(v, dict):
                        tgt = v.get("translation") or v.get("target") or v.get("zh")
                        cat = v.get("category") or "AI翻譯"
                        fn = v.get("file") or "renderer.js"
                        if tgt:
                            results.append((k, str(tgt).strip(), str(cat).strip(), str(fn).strip()))
            if results:
                return results

    lines = text_clean.splitlines()
    ignore_keys = {"original", "translation", "category", "file", "target", "source", "zh", "en"}
    for raw in lines:
        line = raw.rstrip("\r\n")
        if not line.strip() or line.strip().startswith("#"):
            continue
        if DELIM in line:
            parts = line.split(DELIM)
            if len(parts) >= 2:
                src = parts[0]
                tgt = parts[1]
                cat = parts[2].strip() if len(parts) > 2 else "AI翻譯"
                fn = parts[3].strip() if len(parts) > 3 else "renderer.js"
                if src and tgt and tgt != "(待翻譯)":
                    results.append((src, tgt, cat, fn))
                    continue

        if line.startswith("|") and line.endswith("|") and not "---" in line:
            cells = [c.strip() for c in line.strip("|").split("|")]
            if len(cells) >= 2 and cells[0].lower() not in ignore_keys:
                src = cells[0]
                tgt = cells[1]
                cat = cells[2] if len(cells) > 2 else "AI表格翻譯"
                fn = cells[3] if len(cells) > 3 else "renderer.js"
                results.append((src, tgt, cat, fn))
                continue

        m_kv = re.match(r'^["\']?(.*?)["\']?\s*[:=→]\s*["\']?(.*?)["\']?$', line)
        if m_kv:
            src = m_kv.group(1).strip()
            tgt = m_kv.group(2).strip()
            if src and tgt and src.lower() not in ignore_keys and not src.startswith("http"):
                results.append((src, tgt, "對照翻譯", "renderer.js"))
                continue

    return results


def generate_prompt(skip_existing: bool = True, batch_size: int = 150, input_file: str | None = None) -> int:
    TEMP_DIR.mkdir(parents=True, exist_ok=True)
    mappings = load_existing_mappings(USER_ZH)
    targets: list[tuple[str, str, str]] = []

    if input_file:
        in_path = Path(input_file)
        if not in_path.exists():
            print(f"❌ 找不到輸入檔案: {in_path}", file=sys.stderr)
            return 1
        for raw in in_path.read_text(encoding="utf-8").splitlines():
            line = raw.strip()
            if line and not line.startswith("#"):
                parts = line.split("\t") if "\t" in line else [line]
                src = parts[0]
                cat = parts[1].strip() if len(parts) > 1 else "自訂清單"
                fn = parts[2].strip() if len(parts) > 2 else "renderer.js"
                is_translated = src in mappings and has_chinese(mappings[src][0])
                if not skip_existing or not is_translated:
                    targets.append((src, cat, fn))
    else:
        print("🔍 正在掃描 Orig/ 原版檔案中的 UI 英文句子...")
        all_items, total = get_all_scanned_strings()
        if total == 0:
            print("❌ Orig/ 內缺少原版檔案，請先放入官方原版 main.js 與 renderer.js", file=sys.stderr)
            return 1

        for s, cat, fn in all_items:
            is_translated = s in mappings and has_chinese(mappings[s][0])
            if not skip_existing or not is_translated:
                targets.append((s, cat, fn))

    if not targets:
        print("🎉 太棒了！已無任何未翻譯項目（現有字典已完整涵蓋所有介面字串）！")
        return 0

    actual_size = len(targets) if batch_size <= 0 else batch_size
    batch = targets[:actual_size]
    prompt_content = [
        AI_SYSTEM_INSTRUCTION,
        f"【請翻譯以下項目（本批共 {len(batch)} 筆，請完整回傳勿省略）】：",
        "---",
    ]
    for src, cat, fn in batch:
        prompt_content.append(f"{src}{DELIM}(待翻譯){DELIM}{cat}{DELIM}{fn}")
    prompt_content.append("---")

    PROMPT_FILE.write_text("\n".join(prompt_content), encoding="utf-8")
    
    # 保存 sent 條目快取（供匯入時智慧還原首尾空格）
    CACHE_SENT.write_text(json.dumps([t[0] for t in batch], ensure_ascii=False, indent=2), encoding="utf-8")

    if not RESPONSE_TXT.exists() and not RESPONSE_JSON.exists():
        RESPONSE_TXT.write_text(
            "# 請將 AI 回覆的文字或 JSON 直接貼在下方，儲存後執行匯入\n",
            encoding="utf-8",
        )

    prog = calculate_progress()
    print("\n" + "=" * 60)
    print(f"✅ 【提示詞已生成】：{PROMPT_FILE}")
    print(f"📄 【回填檔案路徑】：{RESPONSE_TXT}")
    print(f"📊 本批次匯出：{len(batch)} 筆 | 目前待翻譯總剩餘：{prog['remaining']} 筆 (已完成 {prog['percent']}%)")
    print("=" * 60)
    print("👉 請開啟 ai_prompt.txt 複製內容給 AI，再將結果貼入 ai_response.txt 存檔！\n")
    return 0


def import_response(file_path: str | None = None) -> int:
    in_file = None
    if file_path:
        in_file = Path(file_path)
    elif RESPONSE_TXT.exists() and RESPONSE_TXT.stat().st_size > 70:
        in_file = RESPONSE_TXT
    elif RESPONSE_JSON.exists() and RESPONSE_JSON.stat().st_size > 10:
        in_file = RESPONSE_JSON
    elif (ROOT / "ai_response.txt").exists():
        in_file = ROOT / "ai_response.txt"
    elif (ROOT / "ai_response.json").exists():
        in_file = ROOT / "ai_response.json"
    else:
        in_file = RESPONSE_TXT

    if not in_file or not in_file.exists() or in_file.stat().st_size < 10:
        print(f"\n❌ 找不到有效的 AI 回覆內容！")
        print(f"👉 請確認您已將 AI 回覆的內容貼入並存檔至：{RESPONSE_TXT}\n")
        return 1

    content = in_file.read_text(encoding="utf-8", errors="replace")
    parsed_items = parse_any_ai_content(content)

    if not parsed_items:
        print(f"\n❌ 未能在 {in_file} 中解析出翻譯項目！", file=sys.stderr)
        print("💡 請確認 AI 回覆內容包含 JSON 陣列或對照文字。\n")
        return 1

    mappings = load_existing_mappings(USER_ZH)
    all_scanned, _ = get_all_scanned_strings()
    scanned_stripped_map = {s.strip(): s for s, _, _ in all_scanned}
    
    sent_items: list[str] = []
    sent_stripped_map: dict[str, str] = {}
    if CACHE_SENT.exists():
        try:
            sent_items = json.loads(CACHE_SENT.read_text(encoding="utf-8"))
            for s in sent_items:
                sent_stripped_map[s.strip()] = s
        except Exception:
            pass

    added_count = 0
    updated_count = 0
    imported_sources = set()

    for src, tgt, cat, fn in parsed_items:
        real_src = src
        # 智慧對齊原文空格（優先比對發送快取與掃描原版）
        if real_src not in mappings:
            if real_src.strip() in sent_stripped_map:
                real_src = sent_stripped_map[real_src.strip()]
            elif real_src.strip() in scanned_stripped_map:
                real_src = scanned_stripped_map[real_src.strip()]
            elif real_src.strip() in mappings:
                real_src = real_src.strip()

        imported_sources.add(real_src)
        imported_sources.add(real_src.strip())

        # 智慧對齊譯文的首尾空格
        if real_src.startswith(" ") and not tgt.startswith(" "):
            tgt = " " + tgt
        if real_src.endswith(" ") and not tgt.endswith(" "):
            tgt = tgt + " "

        if has_chinese(tgt):
            if real_src in mappings:
                updated_count += 1
            else:
                added_count += 1
            mappings[real_src] = (tgt, cat, fn)

    save_mappings(USER_ZH, mappings)
    prog = calculate_progress()

    print("\n" + "=" * 60)
    print(f"✅ 翻譯已成功合併至字典 {USER_ZH}！")
    print(f"   - 本次新增中文化：{added_count} 筆")
    print(f"   - 本次更新譯文：{updated_count} 筆")
    print(f"   - 字典累積總詞條：{prog['dict_total']} 筆")
    print("-" * 60)
    print(f"📈 【最新進度大盤點】：")
    print(f"   - 已完成翻譯：{prog['translated']} / {prog['total']} 筆")
    print(f"   - 剩餘待翻譯：{prog['remaining']} 筆")
    print(f"   - 目前完成度：{prog['percent']} %")
    print("=" * 60)

    # 未翻項目直接保留在未翻譯清單中，下次產提示詞自動抓取，不另外產缺漏檔案
    if sent_items:
        uncovered = [item for item in sent_items if item not in imported_sources and item.strip() not in imported_sources]
        if uncovered:
            print(f"\nℹ️ 本次回覆未涵蓋原發送中的 {len(uncovered)} 筆項目，已自動保留在待翻譯清單。")
            print("👉 下次執行 [1] 產生提示詞時會自動包含，無須另外手動補翻！\n")
        else:
            print("\n🎉 太棒了！本批次所有項目皆已完整翻譯入庫！\n")

    CACHE_SENT.unlink(missing_ok=True)
    RESPONSE_TXT.write_text("# 請將下一批 AI 回覆貼在此處並存檔\n", encoding="utf-8")
    return 0


def check_and_clean_dictionary() -> None:
    """字典健檢與過時/無效詞條清理工具（提供數據並由使用者選擇保留或刪除）。"""
    if not USER_ZH.exists():
        print("❌ 字典檔案不存在！")
        return

    m_in = ORIG_DIR / "main.js"
    r_in = ORIG_DIR / "renderer.js"
    if not m_in.exists() or not r_in.exists():
        print("❌ Orig/ 內缺少官方原版檔案，無法比對有效性！")
        return

    m_orig = m_in.read_text(encoding="utf-8")
    r_orig = r_in.read_text(encoding="utf-8")

    raw_lines = USER_ZH.read_text(encoding="utf-8").splitlines()
    total_lines = 0
    entries: list[tuple[str, str, str, str]] = []
    sources: list[str] = []

    for raw in raw_lines:
        line = raw.rstrip("\r\n")
        if not line.strip() or line.strip().startswith("#") or DELIM not in line:
            continue
        total_lines += 1
        i = line.find(DELIM)
        if i < 0:
            continue
        source = line[:i]
        rest = line[i + len(DELIM) :]
        parts = rest.split(DELIM, 2)
        if len(parts) == 3:
            target, category, target_file = parts[0], parts[1], parts[2].strip()
            entries.append((source, target, category, target_file))
            sources.append(source)

    counts = Counter(sources)
    dup_keys = {k: v for k, v in counts.items() if v > 1}
    dup_lines_count = sum(v - 1 for v in dup_keys.values())

    # 去重保留最後一筆
    unique_dict: dict[str, tuple[str, str, str]] = {}
    for src, tgt, cat, fn in entries:
        unique_dict[src] = (tgt, cat, fn)

    # 檢測在當前 Orig 中找不到的詞條
    obsolete_items: list[tuple[str, str, str, str]] = []
    for src, (tgt, cat, fn) in unique_dict.items():
        if fn == "main.js" and src not in m_orig:
            obsolete_items.append((src, tgt, cat, fn))
        elif fn == "renderer.js" and src not in r_orig:
            obsolete_items.append((src, tgt, cat, fn))

    print("\n" + "=" * 65)
    print("        🧹 字典健檢與過時詞條管理工具")
    print("=" * 65)
    print(f"  📊 字典收錄總行數：{total_lines} 筆 (去重後不重複鍵值：{len(unique_dict)} 筆)")
    print(f"  🔁 發現重複定義項：{len(dup_keys)} 組 (共溢出 {dup_lines_count} 行)")
    print(f"  🗑️  官方原版已無此代碼 (過時/無效詞條)：{len(obsolete_items)} 筆")
    if obsolete_items:
        print("     (主要來自 GitHub Desktop 官方升級改版後移除或重構的字串)")
    print("-" * 65)

    if not obsolete_items and dup_lines_count == 0:
        print("🎉 恭喜！目前字典完全健康，無重複且 100% 匹配當前官方原版代碼！\n")
        return

    print("針對上述檢測結果，請選擇處理方式：")
    print(f"  [1] 刪除無效詞條（清理 {len(obsolete_items)} 筆過時條目 + 自動合一重複項，使套用達到 0 警告）")
    print(f"  [2] 繼續保留無效詞條（僅自動合一重複項，完整保留 {len(obsolete_items)} 筆過時歷史詞條）")
    print("  [0] 返回主選單（不對字典做任何更動）")
    print("=" * 65)

    try:
        ans = input("👉 請輸入選項 [0-2]: ").strip()
    except (EOFError, KeyboardInterrupt):
        return

    if ans == "1":
        # 刪除無效詞條
        bak_file = USER_ZH.with_suffix(".zh.bak")
        shutil.copy2(USER_ZH, bak_file)
        
        obsolete_srcs = {src for src, _, _, _ in obsolete_items}
        cleaned_dict = {src: val for src, val in unique_dict.items() if src not in obsolete_srcs}
        save_mappings(USER_ZH, cleaned_dict)

        print("\n" + "=" * 65)
        print(f"✅ 清理完成！")
        print(f"   - 已移除過時無效詞條：{len(obsolete_items)} 筆")
        print(f"   - 已合一重複定義行：{dup_lines_count} 行")
        print(f"   - 字典現存精華詞條：{len(cleaned_dict)} 筆")
        print(f"   - 原始檔案已安全備份至：{bak_file.name}")
        print("=" * 65 + "\n")

    elif ans == "2":
        # 保留無效詞條，僅去重
        bak_file = USER_ZH.with_suffix(".zh.bak")
        shutil.copy2(USER_ZH, bak_file)
        save_mappings(USER_ZH, unique_dict)

        print("\n" + "=" * 65)
        print(f"✅ 已完成重複項合一！")
        print(f"   - 保留全部過時詞條：{len(obsolete_items)} 筆")
        print(f"   - 已合一重複定義行：{dup_lines_count} 行")
        print(f"   - 字典收錄詞條數：{len(unique_dict)} 筆")
        print(f"   - 原始檔案已安全備份至：{bak_file.name}")
        print("=" * 65 + "\n")
    else:
        print("\n↩️ 已取消，字典維持原狀。\n")


def parse_version_tuple(folder_name: str) -> tuple[int, ...]:
    nums = re.findall(r"\d+", folder_name)
    return tuple(map(int, nums)) if nums else (0,)


def get_github_desktop_app_dir() -> tuple[Path | None, str | None]:
    """自動偵測本機最新安裝的 GitHub Desktop resources/app 路徑與版本。"""
    local_app_data = os.environ.get("LOCALAPPDATA")
    if not local_app_data:
        return None, None
    gh_dir = Path(local_app_data) / "GitHubDesktop"
    if not gh_dir.exists():
        return None, None
    try:
        app_dirs = [d for d in gh_dir.iterdir() if d.is_dir() and d.name.startswith("app-")]
    except Exception:
        return None, None
    if not app_dirs:
        return None, None
    app_dirs.sort(key=lambda d: parse_version_tuple(d.name), reverse=True)
    latest_app = app_dirs[0]
    version = latest_app.name.replace("app-", "")
    target = latest_app / "resources" / "app"
    if not target.exists():
        return None, None
    return target, version


def is_github_desktop_running() -> bool:
    """檢查 GitHubDesktop.exe 是否正在執行。"""
    try:
        res = subprocess.run(
            ["tasklist", "/FI", "IMAGENAME eq GitHubDesktop.exe", "/NH"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        return "GitHubDesktop.exe" in res.stdout
    except Exception:
        return False


def close_github_desktop_process() -> bool:
    """自動關閉正在執行的 GitHub Desktop 處理序。"""
    try:
        subprocess.run(["taskkill", "/F", "/IM", "GitHubDesktop.exe"], capture_output=True, timeout=5)
        return True
    except Exception:
        return False


def launch_github_desktop() -> None:
    """啟動 GitHub Desktop。"""
    local_app_data = os.environ.get("LOCALAPPDATA")
    if not local_app_data:
        return
    gh_exe = Path(local_app_data) / "GitHubDesktop" / "GitHubDesktop.exe"
    if gh_exe.exists():
        try:
            flags = getattr(subprocess, "DETACHED_PROCESS", 0) if sys.platform == "win32" else 0
            subprocess.Popen([str(gh_exe)], creationflags=flags)
            print("🚀 已為您啟動 GitHub Desktop！")
        except Exception as e:
            print(f"⚠️ 啟動失敗：{e}")


def install_to_github_desktop() -> int:
    """一鍵將 Windows/ 內的繁中成品安裝套用至 GitHub Desktop，安裝前自動備份原檔。"""
    main_win = WIN_DIR / "main.js"
    ren_win = WIN_DIR / "renderer.js"

    if not main_win.exists() or not ren_win.exists():
        print("\n⚠️ 尚未在 Windows/ 目錄找到編譯成品！")
        ans = input("👉 是否立即執行 [3] 一鍵編譯套用？[Y/n]: ").strip().lower()
        if ans in ("", "y", "yes"):
            code = apply_patch()
            if code != 0:
                print("❌ 編譯失敗，終止安裝！")
                return 1
        else:
            print("↩️ 安裝已取消。")
            return 1

    app_target, version = get_github_desktop_app_dir()
    if not app_target or not version:
        print("\n❌ 自動偵測失敗：找不到 GitHub Desktop 安裝目錄！")
        print(r"💡 請確認您已安裝 GitHub Desktop，預設路徑應為：%LOCALAPPDATA%\GitHubDesktop\app-*\resources\app")
        return 1

    print("\n" + "=" * 65)
    print("      🚀 一鍵安裝套用繁體中文至 GitHub Desktop")
    print("=" * 65)
    print(f"  🔍 偵測到本機版本：GitHub Desktop v{version}")
    print(f"  📁 目標安裝路徑：{app_target}")
    print("-" * 65)

    if is_github_desktop_running():
        print("⚠️ 偵測到 GitHub Desktop 正在執行中！")
        ans = input("👉 為了避免檔案鎖定導致寫入失敗，是否由程式自動關閉 GitHub Desktop？[Y/n]: ").strip().lower()
        if ans in ("", "y", "yes"):
            close_github_desktop_process()
            print("🛑 已成功結束 GitHub Desktop 處理序。")
        else:
            print("⚠️ 未關閉處理序，若覆蓋時出現權限錯誤請手動關閉後重試。")

    # 1. 建立備份
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    backup_folder = BACKUP_DIR / f"backup_{version}_{timestamp}"
    backup_folder.mkdir(parents=True, exist_ok=True)

    dest_main = app_target / "main.js"
    dest_ren = app_target / "renderer.js"

    backed_files = []
    if dest_main.exists():
        shutil.copy2(dest_main, backup_folder / "main.js")
        backed_files.append("main.js")
    if dest_ren.exists():
        shutil.copy2(dest_ren, backup_folder / "renderer.js")
        backed_files.append("renderer.js")

    meta = {
        "version": version,
        "created_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "app_dir": str(app_target),
        "files": backed_files,
    }
    (backup_folder / "backup_meta.json").write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")

    print(f"📦 【步驟 1/2】原版檔案已安全備份至：\n   👉 {backup_folder}")

    # 2. 替換檔案
    try:
        shutil.copy2(main_win, dest_main)
        shutil.copy2(ren_win, dest_ren)
    except Exception as e:
        print(f"\n❌ 覆蓋寫入時發生錯誤：{e}")
        print("💡 請確認 GitHub Desktop 已完全關閉，或以系統管理員身分執行。")
        return 1

    print(f"✅ 【步驟 2/2】繁中化檔案已成功替換覆蓋至安裝目錄！")
    print("=" * 65)
    print(f"🎉 恭喜！GitHub Desktop (v{version}) 繁體中文化套用完成！")
    print("💡 日後若想還原官方英文原版，隨時可在工作台選單執行 [5] 還原原版檔案。")
    print("=" * 65)

    ans = input("\n👉 是否立即為您開啟 GitHub Desktop 檢視繁中介面？[Y/n]: ").strip().lower()
    if ans in ("", "y", "yes"):
        launch_github_desktop()
    return 0


def restore_from_backup() -> int:
    """從 Backups/ 目錄下的備份還原官方原版檔案至 GitHub Desktop。"""
    if not BACKUP_DIR.exists():
        print("\n❌ 尚未找到任何備份資料夾 (Backups/)！")
        return 1

    backups = [d for d in BACKUP_DIR.iterdir() if d.is_dir() and (d / "main.js").exists() and (d / "renderer.js").exists()]
    if not backups:
        print("\n❌ Backups/ 資料夾中沒有找到包含完整 main.js 與 renderer.js 的備份！")
        return 1

    backups.sort(key=lambda d: d.stat().st_mtime, reverse=True)

    print("\n" + "=" * 65)
    print("        🔄 還原官方原版檔案 (從備份還原)")
    print("=" * 65)
    print(f"  📦 共找到 {len(backups)} 筆可用備份紀錄：")
    for idx, b in enumerate(backups, 1):
        meta_file = b / "backup_meta.json"
        ver_info = ""
        date_info = datetime.fromtimestamp(b.stat().st_mtime).strftime("%Y-%m-%d %H:%M:%S")
        if meta_file.exists():
            try:
                m = json.loads(meta_file.read_text(encoding="utf-8"))
                ver_info = f" (v{m.get('version', '未知')})"
                date_info = m.get("created_at", date_info)
            except Exception:
                pass
        rec_tag = " [最新備份 - 推薦]" if idx == 1 else ""
        print(f"  [{idx}] {date_info}{ver_info} - {b.name}{rec_tag}")
    print("  [0] 取消並返回主選單")
    print("-" * 65)

    sel = input(f"👉 請選擇欲還原的備份編號 [預設 1]: ").strip()
    if sel == "0":
        print("↩️ 已取消還原。")
        return 0
    if not sel:
        chosen = backups[0]
    elif sel.isdigit() and 1 <= int(sel) <= len(backups):
        chosen = backups[int(sel) - 1]
    else:
        print("⚠️ 無效的選擇，已取消。")
        return 1

    app_target, version = get_github_desktop_app_dir()
    if not app_target:
        print("\n❌ 自動偵測失敗：找不到 GitHub Desktop 安裝目錄！")
        return 1

    if is_github_desktop_running():
        print("⚠️ 偵測到 GitHub Desktop 正在執行中！")
        ans = input("👉 是否由程式自動為您關閉 GitHub Desktop 以進行還原？[Y/n]: ").strip().lower()
        if ans in ("", "y", "yes"):
            close_github_desktop_process()
            print("🛑 已結束 GitHub Desktop 處理序。")

    try:
        shutil.copy2(chosen / "main.js", app_target / "main.js")
        shutil.copy2(chosen / "renderer.js", app_target / "renderer.js")
    except Exception as e:
        print(f"\n❌ 還原時發生錯誤：{e}")
        return 1

    print("\n" + "=" * 65)
    print(f"✅ 還原成功！已成功將備份 ({chosen.name}) 還原至 GitHub Desktop！")
    print(f"📁 目標路徑：{app_target}")
    print("=" * 65)

    ans = input("\n👉 是否立即啟動 GitHub Desktop？[Y/n]: ").strip().lower()
    if ans in ("", "y", "yes"):
        launch_github_desktop()
    return 0


def apply_patch() -> int:
    patch_script = ROOT / "scripts" / "apply_zh_patch.py"
    print("\n🚀 正在套用翻譯至 Windows/ 目錄...")
    res = subprocess.run([sys.executable, str(patch_script)])
    if res.returncode == 0:
        print("✅ 套用成功！成品已輸出至 Windows/main.js 與 Windows/renderer.js！\n")
    return res.returncode


def interactive_menu() -> None:
    while True:
        prog = calculate_progress()
        print("\n" + "=" * 60)
        print("      🇹🇼 GitHub Desktop 繁體中文化 - 自動化工作台")
        print("=" * 60)
        print(f"  📊 翻譯進度：[已完成 {prog['translated']} / {prog['total']} 筆] ({prog['percent']}%)")
        print(f"  ⏳ 待翻譯剩餘：{prog['remaining']} 筆  |  📚 字典累積收錄：{prog['dict_total']} 條")
        print("-" * 60)
        print("  [1] 掃描並產生 AI 提示詞 (自動抓取未翻譯，可自訂數量或 all)")
        print("  [2] 貼上/匯入 AI 翻譯結果 (智慧對齊空格與即時進度更新)")
        print("  [3] 一鍵編譯繁中檔案 (輸出至 Windows/ 目錄)")
        print("  [4] 🚀 一鍵安裝至 GitHub Desktop (自動偵測版本、備份原檔並套用)")
        print("  [5] 🔄 還原官方原版檔案 (從工作目錄備份還原)")
        print("  [6] 字典健檢與過時詞條清理 (顯示無效筆數，提供刪除/保留選擇)")
        print("  [7] 開啟專案工作資料夾 (Temp / Windows / Orig / Backups)")
        print("  [0] 離開工作台")
        print("=" * 60)

        try:
            choice = input("👉 請輸入選項數字 [0-7]: ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\n👋 已退出工作台！")
            break

        if choice == "1":
            print("\n篩選模式：")
            print("  [1] 僅翻譯「尚未中文化」的項目（推薦，跳過現有已翻譯）")
            print("  [2] 全部重新掃描包含已翻譯項目")
            sub_c = input("請選擇 [1 或 2，預設 1]: ").strip()
            skip = False if sub_c == "2" else True
            
            b_str = input("請輸入本次欲翻譯的項目數量 (直接 Enter 預設 150，輸入 'all' 為全部): ").strip().lower()
            if b_str == "all":
                bs = 0
            elif b_str.isdigit() and int(b_str) > 0:
                bs = int(b_str)
            else:
                bs = 150
            generate_prompt(skip_existing=skip, batch_size=bs)

        elif choice == "2":
            import_response()

        elif choice == "3":
            apply_patch()

        elif choice == "4":
            install_to_github_desktop()

        elif choice == "5":
            restore_from_backup()

        elif choice == "6":
            check_and_clean_dictionary()

        elif choice == "7":
            import os
            os.system(f'explorer "{ROOT}"')

        elif choice == "0" or choice.lower() in ("q", "exit"):
            print("\n👋 感謝使用，已退出工作台！")
            break
        else:
            print("⚠️ 無效的選項，請重新輸入數字！")


def main() -> int:
    parser = argparse.ArgumentParser(description="GitHub Desktop 繁中化 - 互動式 AI 協同翻譯工作台")
    subparsers = parser.add_subparsers(dest="action")

    p_prompt = subparsers.add_parser("prompt", help="產生發送給 AI 的翻譯提示詞")
    p_prompt.add_argument("-s", "--scan", action="store_true", help="自動掃描 Orig/ 尋找未翻譯字串")
    p_prompt.add_argument("-a", "--all", action="store_true", help="包含已翻譯項目（不跳過）")
    p_prompt.add_argument("-i", "--input", help="手動指定待翻譯純文字檔")
    p_prompt.add_argument("-b", "--batch-size", type=int, default=150, help="每批次翻譯數量 (預設 150，設為 0 代表全部)")

    p_import = subparsers.add_parser("import", help="匯入 AI 翻譯結果")
    p_import.add_argument("-f", "--file", help="指定 AI 回覆檔案")

    p_apply = subparsers.add_parser("apply", help="將字典檔套用產生 Windows/ 翻譯檔")

    p_install = subparsers.add_parser("install", help="一鍵安裝至本機 GitHub Desktop (自動備份原檔)")

    p_restore = subparsers.add_parser("restore", help="從 Backups/ 備份還原官方原版檔案")

    p_clean = subparsers.add_parser("clean", help="檢查並清理字典中的過時或重複詞條")

    p_menu = subparsers.add_parser("menu", help="啟動全自動互動數字選單")

    args = parser.parse_args()

    if not args.action or args.action == "menu":
        interactive_menu()
        return 0

    if args.action == "prompt":
        skip = not args.all
        return generate_prompt(skip_existing=skip, batch_size=args.batch_size, input_file=args.input)
    elif args.action == "import":
        return import_response(file_path=args.file)
    elif args.action == "apply":
        return apply_patch()
    elif args.action == "install":
        return install_to_github_desktop()
    elif args.action == "restore":
        return restore_from_backup()
    elif args.action == "clean":
        check_and_clean_dictionary()
        return 0
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
