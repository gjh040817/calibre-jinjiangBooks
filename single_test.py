# -*- coding: utf-8 -*-
import re

SEARCH_TYPE_MAP = {
    "book": 1,
    "author": 2,
    "protagonist": 4,
    "supporting": 5,
    "other": 6,
    "id": 7
}


def parse_search_keyword(query):
    search_type = SEARCH_TYPE_MAP["book"]
    if not query:
        return query, search_type
    q = query.strip()
    m = re.match(r'^(?:t|type)\s*[:=]\s*(\d+)\s*(.*)$', q, re.I)
    if m:
        try:
            tnum = int(m.group(1))
            rest = m.group(2).strip()
            if tnum in SEARCH_TYPE_MAP.values():
                return (rest or query, tnum)
        except Exception:
            pass
    m = re.match(r'^(?:作者|author)\s*[:：]\s*(.+)$', q, re.I)
    if m:
        return m.group(1).strip(), SEARCH_TYPE_MAP['author']
    m = re.match(r'^(?:主角|protagonist)\s*[:：]\s*(.+)$', q, re.I)
    if m:
        return m.group(1).strip(), SEARCH_TYPE_MAP['protagonist']
    m = re.match(r'^(?:配角|supporting)\s*[:：]\s*(.+)$', q, re.I)
    if m:
        return m.group(1).strip(), SEARCH_TYPE_MAP['supporting']
    m = re.match(r'^(?:其它|其他|other)\s*[:：]\s*(.+)$', q, re.I)
    if m:
        return m.group(1).strip(), SEARCH_TYPE_MAP['other']
    m = re.match(r'^(?:ID|文章ID|id)\s*[:：]\s*(.+)$', q, re.I)
    if m:
        return m.group(1).strip(), SEARCH_TYPE_MAP['id']
    if q.startswith("#") and q.endswith("#"):
        inner = q.strip("#").strip()
        return inner, SEARCH_TYPE_MAP['author']
    elif q.startswith("主角#") and q.endswith("#"):
        inner = q[len("主角#"):-1].strip()
        return inner, SEARCH_TYPE_MAP['protagonist']
    elif q.startswith("配角#") and q.endswith("#"):
        inner = q[len("配角#"):-1].strip()
        return inner, SEARCH_TYPE_MAP['supporting']
    elif q.startswith("其他#") and q.endswith("#"):
        inner = q[len("其他#"):-1].strip()
        return inner, SEARCH_TYPE_MAP['other']
    elif q.startswith("ID#") and q.endswith("#"):
        inner = q[len("ID#"):-1].strip()
        return inner, SEARCH_TYPE_MAP['id']
    return query, search_type


if __name__ == '__main__':
    s = '总有老师要请家长'
    q, t = parse_search_keyword(s)
    inv = {v: k for k, v in SEARCH_TYPE_MAP.items()}
    print(f"INPUT: {s!r}")
    print(f"=> query: {q!r}")
    print(f"=> type: {t} ({inv.get(t)})")
