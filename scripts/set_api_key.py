#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
set_api_key.py — 貼上一次 API key,存成使用者環境變數。

## 為什麼要有這支

`config/providers.toml` 只宣告「這家的 key 放在哪個環境變數」(`api_key_env`),
**不放 key 本身** —— key 進檔案就會進備份、進同步、進 git 的風險區。
所以正確的位置一直都是環境變數,只是在 Windows 上手動設很煩:

  * `setx GEMINI_API_KEY sk-...` 會把 key 放進命令列 → 落進 PowerShell 歷史檔
    (`ConsoleHost_history.txt`),而且在 process list 裡看得到。
  * 「系統內容 → 環境變數」那個對話框要點七層。

這支把它變成:雙擊 → 貼上(不顯示)→ Enter → 好了。

## 安全性質(以及誠實的邊界)

做到的:
  * key 只走 stdin 的遮蔽輸入,**不經過命令列參數**,不會進 shell 歷史或 process list
  * **不寫進任何檔案** —— 只寫 `HKCU\\Environment` 這個 OS 的使用者環境變數區
  * 不印出、不寫 log;驗證失敗時只回報 HTTP 狀態碼,不回顯 key
  * **連片段都不顯示** —— 只給長度。末四碼那種「仿信用卡」的做法也不做:
    那行字會進終端機、截圖、螢幕錄影和 AI 對話紀錄,而長度已足以確認貼對了

**做不到的(要知道)**:使用者環境變數在登錄檔裡是明文,任何以你的身分執行的程式
都讀得到。這跟 `.env` 檔的信任模型一樣,只是少了「被同步/備份/誤 commit」的風險。
要更強的保護就得上 Windows 認證管理員 / DPAPI,那是另一個層級的工程。

## 用法

    python scripts/set_api_key.py                 # 列出哪幾家要 key、目前有沒有
    python scripts/set_api_key.py gemini_api      # 設某一家
    python scripts/set_api_key.py gemini_api --no-verify
"""
from __future__ import annotations

import argparse
import getpass
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from config import load_providers  # noqa: E402


def providers_needing_keys() -> dict[str, dict]:
    """只挑有宣告 api_key_env 的供應商。"""
    out = {}
    for name, spec in sorted(load_providers().items()):
        env = spec.get("api_key_env")
        if env:
            out[name] = spec
    return out


def key_status(env_name: str) -> tuple[bool, str]:
    """回傳 (是否已設, 給人看的描述)。永遠不回傳 key 本身,連片段也不回。

    第一版有顯示末四碼(仿信用卡的做法),已拿掉:那仍然是 secret 的一部分,
    而這行字會出現在終端機、螢幕錄影、貼給別人看的截圖、以及 AI 對話紀錄裡。
    長度已足以確認「有設」與「大小合理」,末四碼買不到額外的判斷力。
    """
    value = os.environ.get(env_name) or read_user_env(env_name)
    if not value:
        return False, "未設"
    return True, f"已設(長度 {len(value)})"


def read_user_env(name: str) -> str:
    """讀使用者環境變數(這個 process 沒繼承到的也讀得到)。"""
    if os.name != "nt":
        return ""
    import winreg
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment") as k:
            value, _ = winreg.QueryValueEx(k, name)
            return str(value)
    except (FileNotFoundError, OSError):
        return ""


def write_user_env(name: str, value: str) -> None:
    """寫進 HKCU\\Environment,並廣播 WM_SETTINGCHANGE 讓新開的程式看得到。

    刻意不用 `setx`:那會把值放進命令列。
    """
    if os.name != "nt":
        raise RuntimeError(
            "這支只處理 Windows 的使用者環境變數。"
            f"其他系統請自行把 {name} 加進 shell 的設定檔。"
        )
    import ctypes
    import winreg
    with winreg.OpenKey(
        winreg.HKEY_CURRENT_USER, "Environment", 0, winreg.KEY_SET_VALUE
    ) as k:
        winreg.SetValueEx(k, name, 0, winreg.REG_EXPAND_SZ, value)

    # 通知其他程式環境變數變了。失敗不影響已寫入的值,只是要重開才生效。
    HWND_BROADCAST, WM_SETTINGCHANGE, SMTO_ABORTIFHUNG = 0xFFFF, 0x001A, 0x0002
    try:
        ctypes.windll.user32.SendMessageTimeoutW(
            HWND_BROADCAST, WM_SETTINGCHANGE, 0, "Environment",
            SMTO_ABORTIFHUNG, 3000, None,
        )
    except Exception:
        pass


def verify(spec: dict, key: str, timeout: int = 20) -> tuple[bool, str]:
    """打一次最便宜的認證檢查:GET {base_url}/models。

    只證明 key 能通過認證,**不證明**某個 model 名稱存在,也不產生 token。
    ⚠️ 是否計入該家的每日請求配額,官方沒說,本函式也沒實測 —— 若你的免費層
    RPD 很緊(例如 Gemini 免費層是 20 RPD),就用 --no-verify 跳過。
    """
    import urllib.error
    import urllib.request
    base = (spec.get("base_url") or "").rstrip("/")
    if not base:
        return False, "這家沒有 base_url,跳過驗證"
    req = urllib.request.Request(
        base + "/models", headers={"Authorization": f"Bearer {key}"}
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = json.loads(resp.read().decode("utf-8", "replace"))
            n = len(body.get("data") or [])
            return True, f"認證通過(端點回報 {n} 個可用 model)"
    except urllib.error.HTTPError as e:
        hint = {
            400: "請求被拒 —— key 格式可能不對",
            401: "認證失敗 —— key 錯了或已撤銷",
            403: "key 有效但沒有權限 —— 檢查是不是綁錯專案",
            404: "端點不存在 —— 檢查 providers.toml 的 base_url",
            429: "被限流 —— key 是好的,但配額已滿",
        }.get(e.code, "")
        return False, f"HTTP {e.code}" + (f":{hint}" if hint else "")
    except Exception as e:
        return False, f"連線失敗:{type(e).__name__}"


def list_providers() -> int:
    needing = providers_needing_keys()
    if not needing:
        print("config/providers.toml 裡沒有任何需要 API key 的供應商。")
        return 0
    print("需要 API key 的供應商:\n")
    width = max(len(n) for n in needing)
    for name, spec in needing.items():
        env = spec["api_key_env"]
        ok, desc = key_status(env)
        mark = "✅" if ok else "⬜"
        print(f"  {mark} {name:<{width}}  {env:<20} {desc}")
    print(f"\n設定某一家:python {Path(__file__).name} <供應商名稱>")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("provider", nargs="?", help="供應商名稱(省略則列出清單)")
    ap.add_argument("--no-verify", action="store_true",
                    help="設完不要打端點驗證(免費層配額很緊時用)")
    args = ap.parse_args()

    needing = providers_needing_keys()
    if not args.provider:
        return list_providers()

    spec = needing.get(args.provider)
    if spec is None:
        print(f"'{args.provider}' 不需要 API key,或不在 config/providers.toml 裡。",
              file=sys.stderr)
        print(f"可設定的:{', '.join(needing) or '(無)'}", file=sys.stderr)
        return 1

    env_name = spec["api_key_env"]
    already, desc = key_status(env_name)
    print(f"供應商:{args.provider}")
    print(f"環境變數:{env_name}")
    print(f"目前狀態:{desc}\n")
    if already:
        ans = input("已經有值了,要覆蓋嗎?(y/N) ").strip().lower()
        if ans != "y":
            print("沒有變更。")
            return 0

    print("請貼上 API key,然後按 Enter。")
    print("(輸入不會顯示 —— 這是正常的,不是當掉)\n")
    try:
        key = getpass.getpass("金鑰:").strip()
    except (KeyboardInterrupt, EOFError):
        print("\n取消,沒有變更。")
        return 1

    if not key:
        print("沒有輸入內容,沒有變更。", file=sys.stderr)
        return 1
    if len(key) < 8:
        print("這看起來太短,不像一把 key。沒有變更。", file=sys.stderr)
        return 1
    if any(c.isspace() for c in key):
        print("內容含空白字元 —— 可能複製到多餘的東西了。沒有變更。", file=sys.stderr)
        return 1

    try:
        write_user_env(env_name, key)
    except Exception as e:
        print(f"寫入失敗:{e}", file=sys.stderr)
        return 1
    # 讓同一個 process 內的後續驗證讀得到
    os.environ[env_name] = key
    print(f"\n✅ 已存成使用者環境變數 {env_name}(長度 {len(key)})")

    if args.no_verify:
        print("   已略過驗證(--no-verify)。")
    else:
        print("\n驗證中……")
        ok, detail = verify(spec, key)
        print(("   ✅ " if ok else "   ❌ ") + detail)
        if not ok:
            print("   key 已經寫進去了 —— 若上面是認證失敗,重跑這支換一把即可。")

    key = None
    del key
    print("\n⚠️ 已經開著的程式(Claude Code、終端機)不會自動看到新值,")
    print("   要重開才會繼承。重開後可跑:python scripts/dispatch.py --doctor 確認。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
