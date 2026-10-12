"""Markdown / JSON 备份导出。"""
import json

from .parser import SECTIONS


def to_markdown(data):
    rows = data["rows"]
    L = ["# 拾光 · 个人成长日志备份", "", "导出时间:" + data["now"], ""]
    L += ["## 个人核心记忆档案", ""]
    for s in SECTIONS:
        L.append("### " + s)
        items = data["profile"]["sections"].get(s) or []
        L += (["- " + x for x in items] if items else ["(暂无)"])
        L.append("")
    L += ["## 待审核记忆", ""]
    L += (["- [%s] %s" % (p["cat"], p["text"]) for p in data["pending"]] or ["(无)"])
    L += ["", "## 行动事项", ""]
    L += (["- [%s] %s%s" % ("x" if t["done"] else " ", t["text"],
                            "(截止 " + t["due"] + ")" if t["due"] else "")
           for t in data["todos"]] or ["(无)"])
    L += ["", "## 目标", ""]
    L += (["- [%s] %s%s" % (g["status"], g["title"],
                            "(截止 " + g["deadline"] + ")" if g["deadline"] else "")
           for g in data.get("goals", [])] or ["(无)"])
    L += ["", "## 事件时间线", ""]
    L += (["- [%s][%s|%d] %s%s" % (ev["date"] or "", ev["type"], ev["importance"],
                                    ev["title"], " — " + ev["description"] if ev["description"] else "")
           for ev in data.get("events", [])] or ["(无)"])
    L += ["", "## 日志全文", ""]
    for e in rows:
        L.append("### " + e["date"] + (" " + e["mode"] if e["mode"] else ""))
        L.append("")
        if e["polished"]:
            L += [e["polished"], ""]
        if e["summary"]:
            L += ["**内容提炼**", e["summary"], ""]
        if e["advice"]:
            L += ["**观察与建议**"] + ["- " + a.replace("\n", "\n  ") for a in e["advice"]] + [""]
        if e["profileDelta"]:
            L += ["**画像变化**", e["profileDelta"], ""]
        if e["raw"]:
            L += ["<details><summary>原始口述</summary>", "", e["raw"], "", "</details>", ""]
    return "\n".join(L)


def to_json(data):
    return json.dumps({
        "exportedAt": data["now"],
        "profile": data["profile"],
        "pending": data["pending"],
        "todos": data["todos"],
        "goals": data.get("goals", []),
        "events": data.get("events", []),
        "entries": data["rows"],
    }, ensure_ascii=False, indent=2)
