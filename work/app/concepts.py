"""Small, explicit bilingual vocabulary for offline fallback, not a semantic model."""
import re
import jieba
import jieba.analyse

# Chinese display label, category, bilingual aliases. Expandable without changing the index.
CONCEPTS = [
    ("求最大值", "计算机/算法与数据结构", ["求最大值", "求最大数", "求四者最大", "maximum value", "find maximum"]),
    ("二分查找", "计算机/算法与数据结构", ["二分搜索", "binary search"]),
    ("动态规划", "计算机/算法与数据结构", ["dynamic programming"]),
    ("管理信息系统", "管理/信息系统", ["management information system", "management information systems", "社会技术系统", "管理信息系统"]),
    ("信息系统", "管理/信息系统", ["information system", "information systems"]),
    ("C语言", "计算机/编程语言/C语言", ["qsort", "stdlib.h", "malloc", "C语言", "c language", "c programming", "printf", "scanf"]),
    ("排序算法", "计算机/算法与数据结构", ["sorting algorithm", "排序算法", "快速排序", "比较函数"]),
    ("检索增强生成", "计算机/人工智能", ["rag", "retrieval augmented generation", "retrieval-augmented generation", "检索增强"]),
    ("对话式检索", "计算机/信息检索", ["conversational search", "对话搜索", "对话式搜索"]),
    ("信息检索", "计算机/信息检索", ["information retrieval", "搜索引擎"]),
    ("检索评估", "计算机/信息检索", ["evaluation metrics", "meta-evaluation", "检索评价", "评估指标"]),
    ("法律咨询", "法律/法律研究", ["legal consultation", "法律问答", "法律咨询"]),
    ("机器学习", "计算机/人工智能", ["machine learning", "机器学习"]),
    ("大语言模型", "计算机/人工智能", ["large language model", "llm", "大模型"]),
    ("经济", "经济管理/经济学", ["economics", "economic", "economy", "经济学"]),
    ("管理", "经济管理/管理学", ["management", "管理学"]),
    ("金融", "经济管理/金融", ["finance", "financial", "投资", "金融"]),
    ("艺术教育", "教育/艺术教育", ["art education", "艺术学", "艺术教育"]),
    ("教育", "教育/教育研究", ["education", "教学", "课程", "教育"]),
    ("国家安全", "社会政治/国家安全", ["national security", "国家安全"]),
    ("数据库", "计算机/数据库", ["database", "sqlite", "数据库"]),
    ("项目管理", "工作/项目管理", ["project management", "项目管理"]),
]
STOP = set("""the of and is to a an in on for with from as at by be are was were this that it its
or not can will have has had do does into our we you they their these those using used use
一个 一种 一些 这个 那个 这些 那些 我们 你们 他们 可以 进行 通过 其中 以及 因此 但是
内容 文档 信息 问题 研究 相关 介绍 使用 方法 方面 具有 需要 应该 目前 实际 主要
请 帮我 查找 找到 搜索 检索 关于 有关 想要 我 的 了 和 与 是 有 在 find search
please about show me documents papers""".split())


def contains(text, term):
    if re.fullmatch(r"[a-z0-9 -]+", term):
        return bool(re.search(r"(?<![a-z0-9])" + re.escape(term) + r"(?![a-z0-9])", text, re.I))
    return term.casefold() in text.casefold()


def detected(text):
    ranked = []
    lower = text.casefold()
    for label, category, aliases in CONCEPTS:
        found = [a for a in [label, *aliases] if contains(lower, a)]
        if found:
            # Weight early thematic evidence; cap repetition so repeated boilerplate cannot dominate.
            found=list(dict.fromkeys(a.casefold() for a in found))
            score = sum(min(lower.count(a), 3) + (2 if contains(lower[:400],a) else 0) for a in found)
            # Specific concepts matter more than generic discipline names.
            score *= 2 if len(label) >= 4 else 1
            ranked.append((score, label, category))
    return sorted(ranked, reverse=True)


def clean_tags(names):
    out = []
    for n in names:
        n = str(n).strip()
        if n.casefold() in STOP or len(n) < 2 or n.isdigit():
            continue
        # Convert known English concepts to Chinese, preserve acronyms only as aliases in search.
        for label, _, aliases in CONCEPTS:
            if n.casefold() in [a.casefold() for a in aliases]:
                n = label
                break
        if n not in out:
            out.append(n)
    return out[:8]


def tags(text):
    named = [x[1] for x in detected(text)]
    extracted = jieba.analyse.extract_tags(text, topK=30, allowPOS=("n", "nz", "vn"))
    # Unknown English words cannot be reliably translated offline; do not invent labels.
    extracted = [x for x in extracted if re.search(r"[\u4e00-\u9fff]", x)]
    return clean_tags(named + extracted)[:5]


# Query intent differs from tag quality: explicit subjects such as 信息、研究、方法
# must remain searchable even when too generic to be useful automatic labels.
QUERY_STOP = set("""请 帮我 查找 找到 搜索 检索 关于 有关 想要 我 的 了 和 与 是 有 在
一下 请问 相关 文档 资料 find search please about show me documents papers
the a an of and or is are to for with""".split())


SEARCH_TOPICS = {
    "算法": ["algorithm", "algorithms", "排序", "qsort", "二分查找", "binary search", "递归", "recursion", "动态规划", "dynamic programming", "求最大值", "求最大数", "求四者最大", "最短路径"],
    "排序": ["sorting", "sort", "qsort", "快速排序", "冒泡排序", "归并排序", "堆排序"],
    "信息系统": ["information system", "information systems", "管理信息系统", "management information systems"],
}


def query_groups(query, extra=None):
    remaining = query.casefold().strip()
    # Remove request framing, not substantive terms such as 信息/研究/方法.
    remaining=re.sub(r"^(?:请)?(?:帮我|给我)(?:找一下|查找|找找|找到|搜索|检索)?", "", remaining)
    remaining=re.sub(r"(?:相关的?|有关的?)(?:内容|资料|文档)$", "", remaining)
    remaining=re.sub(r"^(?:与|和|关于|有关)", "", remaining).strip()
    groups = []
    # Longest aliases first, so "project management" doesn't become unrelated "management".
    vocab = [(label, aliases) for label, _, aliases in CONCEPTS]
    vocab.extend((extra or {}).items())
    # Parent topics get a group only when the query names the parent or its English name.
    for label,terms in SEARCH_TOPICS.items():
        triggers=[label]+(["algorithm","algorithms"] if label=="算法" else [])
        for term in triggers:
            if contains(remaining,term):
                if term=="算法" and any(x in remaining for x in ("排序算法","查找算法","算法复杂度")):continue
                if label=="信息系统" and "管理信息系统" in remaining:continue
                if label=="排序" and "排序算法" in remaining:
                    remaining=remaining.replace("排序算法","排序")
                groups.append(list(dict.fromkeys([label,*terms])))
                remaining=re.sub(r"(?<![a-z0-9])"+re.escape(term)+r"(?![a-z0-9])"," ",remaining) if term.isascii() else remaining.replace(term," ")
    candidates = sorted(
        [(a.casefold(), label, aliases) for label, aliases in vocab for a in [label, *aliases] if a],
        key=lambda x: len(x[0]), reverse=True)
    for alias, label, aliases in candidates:
        if contains(remaining, alias):
            groups.append(list(dict.fromkeys([label.casefold(), *[a.casefold() for a in aliases]])))
            if re.fullmatch(r"[a-z0-9 -]+", alias):
                remaining = re.sub(r"(?<![a-z0-9])"+re.escape(alias)+r"(?![a-z0-9])", " ", remaining)
            else:
                remaining = remaining.replace(alias, " ")
    for t in jieba.cut(remaining):
        t = t.strip()
        if t and t not in QUERY_STOP and re.fullmatch(r"\w+", t) and len(t) >= 2:
            if [t] not in groups:
                groups.append([t])
    return list({tuple(g): g for g in groups}.values())


def local_category(text):
    concepts=detected(text)
    if not concepts:return "未分类"
    if re.search(r"\bqsort\b|stdlib\.h",text,re.I):return "计算机/编程语言/C语言"
    top=concepts[0]
    if top[1] in ("信息系统","管理信息系统"):
        management=("组织","业务流程","管理决策","社会技术","人机","organizational","business process","decision making")
        technical=("接口实现","数据库设计","网络架构","系统架构","部署","源代码","api implementation","database schema")
        def score(words):return sum(min(text.casefold().count(w),3)+(2 if w in text[:400].casefold() else 0) for w in words)
        tech,mgmt=score(technical),score(management)
        if top[1]=="管理信息系统":mgmt+=3
        if not tech and not mgmt:return "信息系统"
        return "计算机/信息系统" if tech>mgmt+1 else "管理/信息系统"
    return top[2]
