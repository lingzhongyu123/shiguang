"""解析 GLM 输出 —— 优先 JSON 结构化(v1.2),回退六段式(v1.1 及更早)。"""
import json
import re

SECTIONS = ["长期背景", "当前目标", "长期偏好", "进行中的项目", "经多次观察得到的模式", "近期变化"]


def grab_section(text, idx):
    m = re.search(r"【\s*%d[\.、\s][^\n]*\n?([\s\S]*?)(?=【|$)" % idx, text)
    return m.group(1).strip() if m else ""


def bullet_lines(block):
    out = []
    for line in block.split("\n"):
        line = line.strip()
        line = re.sub(r"^[-•·>\s]+", "", line)
        line = re.sub(r"^\d+[\.、)]\s*", "", line)
        line = line.strip("*").strip()
        if len(line) > 1:
            out.append(line)
    return out


def guess_cat(line):
    rules = [
        ("目标|计划|想要|希望成为", "当前目标"),
        ("习惯|偏好|喜欢|不喜欢|方式", "长期偏好"),
        ("项目|进展|课题|论文|任务", "进行中的项目"),
        ("总是|经常|反复|模式|倾向", "经多次观察得到的模式"),
        ("最近|近期|转为|开始|调整", "近期变化"),
    ]
    for pat, cat in rules:
        if re.search(pat, line):
            return cat
    return "长期背景"


def _loads_repair(s):
    """json.loads + 常见修复(尾逗号);仍失败返回 None。"""
    try:
        return json.loads(s)
    except ValueError:
        pass
    try:
        return json.loads(re.sub(r",\s*([}\]])", r"\1", s))
    except ValueError:
        return None


def _extract_json(text):
    """从模型输出中抠出第一个可解析的平衡 JSON 对象;失败返回 None。"""
    text = re.sub(r"^```(?:json)?|```$", "", text.strip(), flags=re.M).strip()
    pos = 0
    while True:
        start = text.find("{", pos)
        if start < 0:
            return None
        depth = 0
        in_str = False
        esc = False
        end = None
        for i, ch in enumerate(text[start:], start):
            if in_str:
                if esc:
                    esc = False
                elif ch == "\\":
                    esc = True
                elif ch == '"':
                    in_str = False
                continue
            if ch == '"':
                in_str = True
            elif ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    end = i + 1
                    break
        if end is None:
            return None
        d = _loads_repair(text[start:end])
        if isinstance(d, dict):
            return d
        pos = start + 1  # 这个 { 不是合法对象,从下一个继续找


def _unesc(s):
    try:
        return json.loads('"' + s + '"')
    except ValueError:
        return (s.replace("\\n", "\n").replace('\\"', '"')
                .replace("\\/", "/").replace("\\t", "\t"))


_NEXT_KEY = ('summary', 'advice', 'memories', 'profile_delta', 'evidence',
             'events', 'skip_note')


def _salvage_string(text, key):
    """抠出 "key": "..." 的字符串值。先严格匹配(不含裸引号),
    再尝试贪婪匹配到 '", "下一键":' 边界 —— 闪存模型常输出含裸引号/裸换行的正文,
    第一种匹配会提前截断。"""
    # 优先用 ", 下一键": 边界匹配(非贪婪,取最近的边界,不会吞掉后续字段)
    # —— 闪存模型输出常含裸引号,严格匹配会在第一个裸引号处提前截断
    m = re.search(r'"%s"\s*:\s*"(.*?)"\s*,\s*"(?:%s)"\s*:' % (key, "|".join(_NEXT_KEY)),
                  text, re.S)
    if m:
        return _unesc(m.group(1))
    m = re.search(r'"%s"\s*:\s*"((?:[^"\\]|\\.)*)"' % key, text)
    if m:
        return _unesc(m.group(1))
    return None


def _salvage_fields(text):
    """JSON 整体解析失败时逐字段打捞,避免把整坨原始 JSON 当正文存档。"""
    out = {}
    v = _salvage_string(text, "polished")
    if v:
        out["polished"] = v
    v = _salvage_string(text, "theme")
    if v:
        out["theme"] = v
    m = re.search(r'"theme"\s*:\s*"((?:[^"\\]|\\.)*)"', text)
    if m:
        out["theme"] = _unesc(m.group(1))
    for key in ("advice", "memories", "events"):
        m = re.search(r'"%s"\s*:\s*\[' % key, text)
        if not m:
            continue
        i = m.end() - 1
        depth = 0
        in_str = False
        esc = False
        end = None
        for j, ch in enumerate(text[i:], i):
            if in_str:
                if esc:
                    esc = False
                elif ch == "\\":
                    esc = True
                elif ch == '"':
                    in_str = False
                continue
            if ch == '"':
                in_str = True
            elif ch == "[":
                depth += 1
            elif ch == "]":
                depth -= 1
                if depth == 0:
                    end = j + 1
                    break
        if end:
            v = _loads_repair(text[i:end])
            if isinstance(v, list):
                out[key] = v
    return out


VALID_CATS = set(SECTIONS)


def parse_json_result(text, raw, date, mode):
    """优先解析 JSON 结构化输出;整体失败先打捞字段,再回退旧的六段式解析。"""
    d = _extract_json(text)
    if not isinstance(d, dict) and ('"polished"' in text or text.lstrip().startswith("{")):
        salv = _salvage_fields(text)
        if salv.get("polished"):
            d = {"polished": salv["polished"],
                 "summary": {"theme": salv.get("theme", ""), "points": []},
                 "advice": salv.get("advice", []),
                 "memories": salv.get("memories", []),
                 "events": salv.get("events", [])}
    if not isinstance(d, dict):
        # 回退旧六段式,并归一成 memories 结构
        legacy = parse_result(text, raw, date, mode)
        memories = []
        for line in legacy["memPack"]:
            cat, txt = split_mem_line(line)
            memories.append({"text": txt, "cat": cat or guess_cat(txt),
                             "confidence": None, "note": ""})
        legacy["memories"] = memories
        legacy["skipNote"] = ""
        legacy["format"] = "legacy"
        return legacy

    polished = str(d.get("polished") or "").strip()
    summ = d.get("summary") or {}
    if isinstance(summ, str):
        summary = summ.strip()
    else:
        parts = []
        if summ.get("theme"):
            parts.append("主题:" + str(summ["theme"]))
        for p in (summ.get("points") or []):
            parts.append("- " + str(p))
        summary = "\n".join(parts)

    advice = []
    for a in (d.get("advice") or []):
        if isinstance(a, str):
            if a.strip():
                advice.append(a.strip())
            continue
        line = ""
        if a.get("observation"):
            line += "观察:" + str(a["observation"]).strip()
        if a.get("suggestion"):
            line += ("\n" if line else "") + "建议:" + str(a["suggestion"]).strip()
        if a.get("question"):
            line += ("\n" if line else "") + "思考:" + str(a["question"]).strip()
        if line:
            advice.append(line)

    mem = []
    for m in (d.get("memories") or []):
        if isinstance(m, str):
            m = {"text": m}
        txt = str(m.get("text") or "").strip()
        if not txt:
            continue
        cat = m.get("category") or m.get("cat") or ""
        if cat not in VALID_CATS:
            cat = guess_cat(txt)
        conf = m.get("confidence")
        try:
            conf = round(float(conf), 2)
        except (TypeError, ValueError):
            conf = None
        mem.append({"text": txt, "cat": cat, "confidence": conf,
                    "note": str(m.get("note") or "").strip()})

    evi = str(d.get("evidence") or "").strip()
    if evi not in ("高", "中", "低"):
        evi = "低" if not d.get("profile_delta") else "中"

    events = []
    for ev in (d.get("events") or []):
        if isinstance(ev, str):
            ev = {"title": ev}
        title = str(ev.get("title") or "").strip()
        if not title:
            continue
        etype = str(ev.get("type") or "life").lower()
        if etype not in EVENT_TYPES:
            etype = "life"
        try:
            imp = max(1, min(5, int(ev.get("importance") or 1)))
        except (TypeError, ValueError):
            imp = 1
        events.append({"title": title, "type": etype, "importance": imp,
                       "description": str(ev.get("description") or "").strip()})

    if not polished and not advice and not mem and not d.get("profile_delta"):
        polished = text.strip()

    return {"date": date, "mode": mode or "", "raw": raw,
            "polished": polished, "summary": summary, "advice": advice,
            "memories": mem, "profileDelta": str(d.get("profile_delta") or "").strip(),
            "evi": evi, "events": events,
            "skipNote": str(d.get("skip_note") or "").strip(),
            "format": "json"}


def parse_result(text, raw, date, mode):
    seg = {"polished": "", "summary": "", "advice": [], "memPack": [], "profileDelta": "", "evi": ""}
    s1 = grab_section(text, 1)
    if s1:
        seg["polished"] = s1
    seg["summary"] = grab_section(text, 2)
    s3 = grab_section(text, 3)
    if s3:
        seg["advice"] = bullet_lines(s3)
    s4 = grab_section(text, 4)
    if s4:
        pm = re.search(r"(记忆更新包|更新包)[:::]?\s*([\s\S]*?)(?=本次不保存|$)", s4)
        if pm:
            seg["memPack"] = bullet_lines(pm.group(2))
        else:
            am = re.search(r"(新增记忆|修订记忆)[:::]?\s*([\s\S]*?)(?=本次不保存|记忆更新包|$)", s4)
            seg["memPack"] = bullet_lines(am.group(2)) if am else bullet_lines(s4)
    s5 = grab_section(text, 5)
    if s5:
        seg["profileDelta"] = s5
        em = re.search(r"证据强度[:::]\s*(高|中|低)", s5)
        if em:
            seg["evi"] = em.group(1)
    seg["events"] = parse_events(grab_section(text, 6))
    if not seg["polished"] and not seg["advice"] and not seg["memPack"] and not seg["profileDelta"]:
        seg["polished"] = text.strip()
    return {"date": date, "mode": mode or "", "raw": raw, **seg}


EVENT_TYPES = ["research", "study", "life", "decision", "relation"]


def parse_events(block):
    """解析第6段 '- 标题 | 类型 | 重要度 | 描述' 行。"""
    out = []
    for line in block.split("\n"):
        line = re.sub(r"^[-•·]\s*", "", line.strip())
        if not line or line == "无" or "|" not in line:
            continue
        parts = [p.strip() for p in line.split("|")]
        if len(parts) < 2 or not parts[0]:
            continue
        etype = parts[1].lower() if len(parts) > 1 else "life"
        if etype not in EVENT_TYPES:
            etype = "life"
        try:
            imp = max(1, min(5, int(re.sub(r"\D", "", parts[2]) or 1)))
        except (ValueError, IndexError):
            imp = 1
        desc = parts[3] if len(parts) > 3 else ""
        out.append({"title": parts[0], "type": etype, "importance": imp, "description": desc})
    return out


def split_mem_line(line):
    """行内若标注了归入节,拆出 (cat, text);否则返回 (None, line)。"""
    line = re.sub(r"^[-•·>\s]+", "", line)  # 去掉 "- " "> " 等前缀
    # 兼容 "【长期背景|已确认事实】" / "[进行中的项目 | 待观察]" 这类前置标注
    m = re.match(r"^[【\[]?(长期背景|当前目标|长期偏好|进行中的项目|经多次观察得到的模式|近期变化)(?:[】\]]|\s*[|｜][^】\]]*[】\]])?\s*[:::]?\s*(.+)$", line)
    if m:
        return m.group(1), m.group(2)
    return (None, line)


def parse_profile_textarea(txt):
    """解析画像编辑框的 '## 节名' 格式,规则与原型一致。"""
    sections = {s: [] for s in SECTIONS}
    for block in re.split(r"\n(?=## )", txt):
        m = re.match(r"^##\s*(.+)\n?([\s\S]*)$", block)
        if not m:
            continue
        sec = m.group(1).strip()
        if sec not in sections:
            continue
        for line in m.group(2).split("\n"):
            line = re.sub(r"^[-•·]\s*", "", line.strip())
            if line and line != "(暂无)":
                sections[sec].append(line)
    return sections
