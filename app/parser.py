"""解析 GLM 五段式输出 —— 从 daily-log-agent.html 原型移植,逻辑保持一致。"""
import re

SECTIONS = ["长期背景", "当前目标", "长期偏好", "进行中的项目", "经多次观察得到的模式", "近期变化"]


def grab_section(text, idx):
    m = re.search(r"【\s*%d[\.、\s][^\n]*\n?([\s\S]*?)(?=【|$)" % idx, text)
    return m.group(1).strip() if m else ""


def bullet_lines(block):
    out = []
    for line in block.split("\n"):
        line = line.strip()
        line = re.sub(r"^[-•·]\s*", "", line)
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
    if not seg["polished"] and not seg["advice"] and not seg["memPack"] and not seg["profileDelta"]:
        seg["polished"] = text.strip()
    return {"date": date, "mode": mode or "", "raw": raw, **seg}


def split_mem_line(line):
    """行内若标注了归入节,拆出 (cat, text);否则返回 (None, line)。"""
    m = re.match(r"^\[?(长期背景|当前目标|长期偏好|进行中的项目|经多次观察得到的模式|近期变化)\]?\s*[:::]?\s*(.+)$", line)
    return (m.group(1), m.group(2)) if m else (None, line)


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
