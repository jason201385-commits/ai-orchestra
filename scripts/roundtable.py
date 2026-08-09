#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""多 AI 協作流程執行器 —— 把「多輪討論」從手拼 prompt 變成可重複、可稽核的流程。

靈感來源:teddashh/multi-ai-chat-desktop(MIT)的 workflow preset 設計。
我們移植的是規則,不是程式碼:

  1. **平行角色必須不同家**;序列角色可以重用同一家
  2. **供應商出錯就停整個流程** —— 錯誤訊息絕不當成答案傳給下一輪
     (呼應本 skill 的「rc=0 不是模型的工作證明」教訓)
  3. **流程有版本號**;快照重播時版本對不上就明確失敗,不靜默換路由

用法:
  python roundtable.py --preset roundtable --topic "要不要自建 CRM?" \\
      --roles "codex=技術可行性,grok=市場與使用者,agy=設計與體驗" \\
      --facts facts.md --limit 400

  python roundtable.py --list                 # 看有哪些流程
  python roundtable.py --preflight            # 只檢查供應商就緒,不燒額度
  python roundtable.py --replay data/runs/... # 重播(驗版本、印全文)

產出:data/runs/<時間>-<preset>/
  manifest.json     流程版本、角色配置、每輪成敗、耗時
  rNN-<provider>.md 每一輪每一家的完整回覆(**全文,不截斷**)
"""
import argparse
import concurrent.futures as cf
import json
import os
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
RUNS = ROOT / "data" / "runs"
DISPATCH = HERE / "dispatch.py"

try:
    import tomllib
except ModuleNotFoundError:  # Python < 3.11
    import tomli as tomllib  # type: ignore

# 同一份 Claude 登入不能並行(見 SKILL.md「Claude/Fable 不並行」)
SERIAL_ONLY = {"claude", "fable"}


def load_workflows():
    with open(ROOT / "config" / "workflows.toml", "rb") as f:
        return tomllib.load(f)


def load_providers():
    # fresh clone 沒有 config/providers.toml → 退回附帶的 example(與 config.py 同邏輯)
    path = ROOT / "config" / "providers.toml"
    if not path.exists():
        path = ROOT / "providers.example.toml"
    with open(path, "rb") as f:
        return tomllib.load(f)


def preflight(providers):
    """只檢查就緒,不呼叫任何模型(零 token)。回傳 {provider: (ok, msg)}"""
    out = {}
    try:
        r = subprocess.run([sys.executable, str(DISPATCH), "--doctor"],
                           capture_output=True, text=True, timeout=90,
                           encoding="utf-8", errors="replace")
        doctor = (r.stdout or "") + (r.stderr or "")
    except Exception as e:
        doctor = f"(doctor 失敗: {e})"
    for p in providers:
        line = next((l for l in doctor.splitlines() if l.strip().startswith(p)), "")
        bad = any(k in line for k in ("NO KEY", "NOT FOUND", "missing", "MISSING"))
        out[p] = (not bad and bool(line), line.strip() or "(doctor 沒有這家的資訊)")
    return out


def call(provider, prompt, label, timeout):
    """呼叫單一供應商。回傳 (ok, text, seconds)。錯誤絕不當成答案。"""
    t0 = time.time()
    try:
        r = subprocess.run(
            [sys.executable, str(DISPATCH), provider, "--label", label, "--timeout", str(timeout)],
            input=prompt, capture_output=True, text=True, timeout=timeout + 90,
            encoding="utf-8", errors="replace")
        text = (r.stdout or "").strip()
        ok = r.returncode == 0 and len(text) > 0
        if not ok:
            text = f"[dispatch 失敗 rc={r.returncode}]\n{(r.stderr or '')[:600]}\n{text[:600]}"
        return ok, text, round(time.time() - t0, 1)
    except Exception as e:
        return False, f"[執行器例外] {e}", round(time.time() - t0, 1)


def build_prompt(tmpl, **kw):
    for k, v in kw.items():
        tmpl = tmpl.replace("{" + k + "}", str(v))
    return tmpl


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--preset")
    ap.add_argument("--topic")
    ap.add_argument("--roles", help='格式:"codex=技術,grok=市場,agy=設計"')
    ap.add_argument("--facts", help="事實檔路徑(會原樣附進每輪 prompt,避免各家亂猜)")
    ap.add_argument("--limit", default="400", help="每家字數上限提示")
    ap.add_argument("--timeout", type=int, default=420)
    ap.add_argument("--list", action="store_true")
    ap.add_argument("--preflight", action="store_true")
    ap.add_argument("--replay")
    a = ap.parse_args()

    wf = load_workflows()

    if a.list:
        print("可用流程:")
        for k, v in wf.items():
            print(f"  {k:<12} v{v['version']}  {v['title']} —— {v['desc']}")
            print(f"{'':14}輪次: " + " → ".join(r["name"] for r in v["rounds"]) )
        return 0

    if a.replay:
        d = Path(a.replay)
        man = json.loads((d / "manifest.json").read_text(encoding="utf-8"))
        cur = wf.get(man["preset"], {}).get("version")
        if cur != man["workflow_version"]:
            print(f"❌ 版本不符:快照是 v{man['workflow_version']},目前 {man['preset']} 是 v{cur}")
            print("   流程語意已改變,重播會失真 —— 明確失敗,不靜默沿用。")
            return 2
        print(f"✅ 版本相符 v{cur}。{man['preset']} · {man['topic']}")
        for f in sorted(d.glob("r*.md")):
            print("\n" + "=" * 70 + f"\n{f.name}\n" + "=" * 70)
            print(f.read_text(encoding="utf-8"))
        return 0

    if not a.roles:
        print("需要 --roles,例如: --roles \"codex=技術,grok=市場,agy=設計\"")
        return 1
    roles = {}
    for pair in a.roles.split(","):
        p, _, r = pair.partition("=")
        roles[p.strip()] = r.strip()

    provs = list(roles)
    if a.preflight or True:  # 一律先 preflight,燒額度前先知道誰不在
        pf = preflight(provs)
        print("就緒檢查(零 token):")
        for p, (ok, msg) in pf.items():
            print(f"  {'✅' if ok else '⚠️ '} {p:<10} {msg}")
        missing = [p for p, (ok, _) in pf.items() if not ok]
        if missing:
            print(f"\n⚠️  這幾家可能不可用:{', '.join(missing)}。要換角色就改 --roles 再跑一次。")
        if a.preflight:
            return 0

    if not a.preset or not a.topic:
        print("需要 --preset 與 --topic(用 --list 看有哪些流程)")
        return 1
    if a.preset not in wf:
        print(f"沒有這個流程:{a.preset}。用 --list 看清單")
        return 1

    flow = wf[a.preset]
    # 規則:single 輪次依 --roles 宣告順序輪流指派(第 1 個 single 輪→第 1 個 role,
    # 以此類推)。roles 不夠就明確中止 —— 不靜默重用第一家(那會變成同家自問自答)。
    role_order = list(roles)  # dict 保插入序 = --roles 宣告序
    n_single = sum(1 for rd in flow["rounds"] if rd["mode"] != "parallel")
    if n_single > len(role_order):
        print(f"❌ {a.preset} 有 {n_single} 個單人輪次,但 --roles 只宣告了 "
              f"{len(role_order)} 家({', '.join(role_order)})。")
        print("   單人輪次依 --roles 宣告順序輪流指派,不會靜默重用第一家 —— 請補足角色再跑。")
        return 1

    facts = ""
    if a.facts and Path(a.facts).exists():
        facts = "【已確認的事實(以此為準,不要推翻也不要編造)】\n" + \
                Path(a.facts).read_text(encoding="utf-8")

    run = RUNS / f"{datetime.now():%Y%m%d-%H%M%S}-{a.preset}"
    run.mkdir(parents=True, exist_ok=True)
    man = {"preset": a.preset, "workflow_version": flow["version"], "topic": a.topic,
           "roles": roles, "started": datetime.now().isoformat(timespec="seconds"),
           "rounds": []}

    print(f"\n▶ {flow['title']} (v{flow['version']}) · {len(flow['rounds'])} 輪 · {len(roles)} 家")
    print(f"  議題:{a.topic}\n  紀錄:{run}\n")

    prior = ""
    single_idx = 0  # 下一個 single 輪次該輪到 role_order 的第幾家
    for i, rd in enumerate(flow["rounds"], 1):
        if rd["mode"] == "parallel":
            names = list(roles)
        else:
            names = [role_order[single_idx]]
            single_idx += 1
        # 規則 1:平行角色必須不同家(同一家平行送會互相排隊/汙染 context)
        if rd["mode"] == "parallel" and len(set(names)) != len(names):
            print("❌ 平行輪次的角色必須是不同供應商"); return 1
        print(f"── 第 {i} 輪:{rd['name']}({rd['mode']},{len(names)} 家)")

        jobs = {}
        for p in names:
            prompt = build_prompt(rd["prompt"], round=i, total=len(flow["rounds"]),
                                  topic=a.topic, facts=facts, role=roles[p], prior=prior,
                                  limit=f"\n\n({a.limit} 字內。)")
            jobs[p] = prompt

        results = {}
        parallel = [p for p in names if p not in SERIAL_ONLY]
        serial = [p for p in names if p in SERIAL_ONLY]
        if parallel:
            with cf.ThreadPoolExecutor(max_workers=len(parallel)) as ex:
                futs = {ex.submit(call, p, jobs[p], f"{a.preset}-r{i}", a.timeout): p for p in parallel}
                for f in cf.as_completed(futs):
                    results[futs[f]] = f.result()
        for p in serial:  # Claude/Fable 同帳號不能並行
            results[p] = call(p, jobs[p], f"{a.preset}-r{i}", a.timeout)

        rec = []
        for p in names:
            ok, text, secs = results[p]
            (run / f"r{i:02d}-{p}.md").write_text(
                f"# 第 {i} 輪 · {rd['name']} · {p}({roles[p]})\n\n{text}\n", encoding="utf-8")
            print(f"   {'✅' if ok else '❌'} {p:<8} {secs:>6}s  {len(text):>6} 字")
            rec.append({"provider": p, "ok": ok, "seconds": secs, "chars": len(text)})
            # 規則 2:出錯就停,錯誤訊息絕不當答案傳下去
            if not ok:
                man["rounds"].append({"round": i, "name": rd["name"], "results": rec,
                                      "aborted": True})
                (run / "manifest.json").write_text(json.dumps(man, ensure_ascii=False, indent=2),
                                                   encoding="utf-8")
                print(f"\n❌ {p} 失敗 → 中止整個流程(錯誤不會被當成答案傳給下一輪)")
                print(f"   已完成的部分都在:{run}")
                return 3

        man["rounds"].append({"round": i, "name": rd["name"], "results": rec, "aborted": False})
        prior = "\n\n".join(f"■ {p}({roles[p]}):\n{results[p][1]}" for p in names)

    man["finished"] = datetime.now().isoformat(timespec="seconds")
    (run / "manifest.json").write_text(json.dumps(man, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n✅ 全部完成 → {run}")
    print("   最後一輪(收斂)的全文請直接讀該目錄;總結與裁決由 Claude 做,不外派。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
