# -*- coding: utf-8 -*-
"""
使用晋江 APP API 根据书名获取元数据（跳过 calibre）。
用法：
  python fetch_metadata_app.py "书名" "cookie-string"
如果未提供 cookie，会尝试从环境变量 JINJIANG_COOKIE 读取。

流程：
 1) 调用 app 搜索 API 搜索书名，解析返回 JSON 获取 novelId
 2) 调用 novelbasicinfo 接口获取结构化元数据并打印

注意：需要安装 requests（pip install requests）。
"""
import os
import re
import sys
import json

try:
    import requests
except Exception:
    requests = None
else:
    try:
        # disable insecure warnings when verify=False is used
        import urllib3
        urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
    except Exception:
        pass


APP_SEARCH_URL = 'https://app.jjwxc.org/androidapi/search'
APP_SEARCH_V3 = 'https://app.jjwxc.org/search/searchV3'
NOVEL_BASIC_URLS = [
    'https://app-cdn.jjwxc.net/androidapi/novelbasicinfo',  # CDN 端点（推荐）
    'https://app.jjwxc.org/androidapi/getBookDetail',  # 标准 APP 详情接口
    'https://app.jjwxc.org/androidapi/novelbasicinfo'  # 备选端点
]

HEADERS = {
    'User-Agent': 'JJWXC-Android/9.9.9 (Android; 10; SM-G973F)',
    'Referer': 'http://android.jjwxc.net?v=206',
    'Accept': 'application/json, text/plain, */*'
}


def extract_token_from_cookie(cookie):
    if not cookie:
        return None
    m = re.search(r'token=([^;\s]+)', cookie)
    if m:
        return m.group(1)
    # sometimes bbstoken contains useful value
    m2 = re.search(r'bbstoken=([^;\s]+)', cookie)
    if m2:
        return m2.group(1)
    return None


def search_app(title, cookie=None, token=None):
    """调用 APP 搜索接口，返回可能的 novelId 列表（按优先顺序）"""
    hdrs = dict(HEADERS)
    if cookie:
        hdrs['Cookie'] = cookie

    # 优先尝试 searchV3 接口（与主插件逻辑一致）
    params_v3 = {
        'keyword': title,
        'type': 1,  # 1 表示书名
        'page': 1
    }
    if token:
        params_v3['token'] = token

    # prefer requests if available
    if requests:
        # 策略1：优先尝试 searchV3
        try:
            r = requests.get(APP_SEARCH_V3, params=params_v3, headers=hdrs, timeout=10, verify=False)
            j = r.json()
            ids = extract_novel_ids(j)
            if ids:
                return ids
        except Exception as e:
            print(f'SearchV3 request failed: {e}')
        
        # 策略2：回退到 androidapi/search
        try:
            params = {
                'keyword': title,
                'type': 1,
                'page': 1,
                'versionCode': 282
            }
            if token:
                params['token'] = token
            r = requests.get(APP_SEARCH_URL, params=params, headers=hdrs, timeout=10, verify=False)
            j = r.json()
            ids = extract_novel_ids(j)
            if ids:
                return ids
        except Exception as e:
            print(f'Search request failed: {e}')
            return []
    else:
        # minimal urllib fallback
        from urllib.parse import urlencode
        from urllib.request import Request, urlopen
        import ssl as _ssl
        ctx = _ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = _ssl.CERT_NONE
        
        # 策略1：优先尝试 searchV3
        try:
            url = APP_SEARCH_V3 + '?' + urlencode(params_v3)
            req = Request(url, headers=hdrs)
            with urlopen(req, timeout=10, context=ctx) as res:
                content = res.read().decode(res.headers.get_content_charset() or 'utf-8', errors='ignore')
                j = json.loads(content)
                ids = extract_novel_ids(j)
                if ids:
                    return ids
        except Exception as e:
            print(f'SearchV3 request failed (urllib): {e}')
        
        # 策略2：回退到 androidapi/search
        try:
            params = {
                'keyword': title,
                'type': 1,
                'page': 1,
                'versionCode': 282
            }
            if token:
                params['token'] = token
            url = APP_SEARCH_URL + '?' + urlencode(params)
            req = Request(url, headers=hdrs)
            with urlopen(req, timeout=10, context=ctx) as res:
                content = res.read().decode(res.headers.get_content_charset() or 'utf-8', errors='ignore')
                j = json.loads(content)
                ids = extract_novel_ids(j)
                if ids:
                    return ids
        except Exception as e:
            print('Search request failed (urllib):', e)
            return []
    
    return []


def extract_novel_ids(j):
    """从JSON响应中提取novelId列表"""
    ids = []
    # common shape: { code:0, data:{books:[{novelid:..., bookname:...}, ...]}}
    if isinstance(j, dict):
        if 'data' in j and isinstance(j['data'], dict):
            data = j['data']
            # books list
            if 'books' in data and isinstance(data['books'], list):
                for b in data['books']:
                    nid = str(b.get('novelid') or b.get('bookId') or b.get('id') or b.get('novelId'))
                    if nid and nid not in ids:
                        ids.append(nid)
            # sometimes data is a list directly
            elif isinstance(data, list):
                for b in data:
                    nid = str(b.get('novelid') or b.get('bookId') or b.get('id') or b.get('novelId'))
                    if nid and nid not in ids:
                        ids.append(nid)
        # fallback: top-level list
        if not ids:
            for key in ('books', 'list', 'data', 'results', 'items'):
                if key in j and isinstance(j[key], list):
                    for b in j[key]:
                        if isinstance(b, dict):
                            nid = str(b.get('novelid') or b.get('bookId') or b.get('id') or b.get('novelId'))
                            if nid and nid not in ids:
                                ids.append(nid)
    # also try regex on raw JSON dump
    if not ids:
        s = json.dumps(j)
        for m in re.findall(r'novelid\W*[:=]\W*"?(\d+)"?', s):
            if m not in ids:
                ids.append(m)
    return ids


def get_novel_basic(novelid, cookie=None, token=None):
    """获取书籍基本信息，尝试多个端点和参数变体（与主插件逻辑一致）"""
    hdrs = dict(HEADERS)
    if cookie:
        hdrs['Cookie'] = cookie

    # 参数名变体列表（不同接口可能使用不同的参数名）
    param_variants = [
        {'novelid': novelid},   # 小写
        {'novelId': novelid},  # 驼峰
        {'bookId': novelid},   # bookId 格式
        {'bookid': novelid}    # 全小写 bookid
    ]

    # 遍历所有端点和参数变体组合
    for endpoint in NOVEL_BASIC_URLS:
        for base_params in param_variants:
            params = dict(base_params)
            if token:
                params['token'] = token
            params.setdefault('version', '9.9.9')
            params.setdefault('platform', 'android')

            if requests:
                try:
                    r = requests.get(endpoint, params=params, headers=hdrs, timeout=15, verify=False)
                    if r.status_code in (200, 201):
                        data = r.json()
                        # 验证返回数据是否有效
                        if data and _is_valid_book_data(data, novelid):
                            return data
                except Exception as e:
                    continue
            else:
                from urllib.parse import urlencode
                from urllib.request import Request, urlopen
                import ssl as _ssl
                ctx = _ssl.create_default_context()
                ctx.check_hostname = False
                ctx.verify_mode = _ssl.CERT_NONE
                try:
                    url = endpoint + '?' + urlencode(params)
                    req = Request(url, headers=hdrs)
                    with urlopen(req, timeout=15, context=ctx) as res:
                        if res.status in (200, 201):
                            content = res.read().decode(res.headers.get_content_charset() or 'utf-8', errors='ignore')
                            data = json.loads(content)
                            if data and _is_valid_book_data(data, novelid):
                                return data
                except Exception:
                    continue
    
    print('Novel basic request failed: all endpoints tried')
    return None


def _is_valid_book_data(data, novelid):
    """验证返回的数据是否包含有效的书籍信息"""
    if not data:
        return False
    # 尝试提取数据对象
    app_data = None
    if isinstance(data, dict):
        if data.get('code') == 0 and data.get('data'):
            d = data.get('data')
            if isinstance(d, dict) and (d.get('book') or d.get('novel')):
                app_data = d.get('book') or d.get('novel') or d
            else:
                app_data = d
        elif data.get('data') and isinstance(data.get('data'), dict):
            app_data = data.get('data')
        else:
            app_data = data
    elif isinstance(data, list) and data:
        app_data = data[0]
    
    if not app_data:
        return False
    
    # 检查是否包含关键字段
    if isinstance(app_data, dict):
        title = app_data.get('novelName') or app_data.get('bookname') or app_data.get('bookName') or app_data.get('title')
        author = app_data.get('authorName') or app_data.get('author') or app_data.get('authorname')
        nid = str(app_data.get('novelId') or app_data.get('novelid') or app_data.get('id') or '')
        # 至少要有标题或作者，或者ID匹配
        return bool(title or author or (nid == str(novelid)))
    return False


def normalize_cover_url(cover, novelid=None):
    """规范化封面URL，与主插件逻辑一致"""
    if not cover:
        return ''
    cover = str(cover).strip()
    if cover:
        if cover.startswith('//'):
            cover = 'https:' + cover
        elif cover.startswith('/'):
            cover = 'https://www.jjwxc.net' + cover
        elif not cover.startswith('http://') and not cover.startswith('https://') and not cover.startswith('data:'):
            if cover:
                cover = 'https://www.jjwxc.net/' + cover.lstrip('/')
    # 验证URL有效性
    if cover and (cover.startswith('http://') or cover.startswith('https://') or cover.startswith('data:')):
        return cover
    return ''


def print_basic_info(basic):
    if not basic:
        print('No basic info')
        return
    # the API may return { code:0, data:{...} } or {...}
    data = basic.get('data') if isinstance(basic, dict) and 'data' in basic else basic
    if isinstance(data, dict):
        title = data.get('novelName') or data.get('bookname') or data.get('bookName') or data.get('title')
        author = data.get('authorName') or data.get('author') or data.get('authorname')
        novelid = data.get('novelId') or data.get('novelid') or data.get('id')
        
        # 封面：优先使用 novelCover 和 originalCover（真实封面），避免使用 localImg（默认封面）
        cover = None
        # 优先级：novelCover > originalCover > 其他字段 > localImg（最后备选）
        if 'novelCover' in data and data['novelCover']:
            cover = data['novelCover']
        elif 'originalCover' in data and data['originalCover']:
            cover = data['originalCover']
        else:
            # 其他字段作为备选
            cover_candidates = ('coverimg', 'cover', 'cover_img', 'bookimg', 'coverUrl', 'cover_url')
            for key in cover_candidates:
                if key in data and data[key]:
                    cover = data[key]
                    break
            # 最后才尝试 localImg（通常是默认封面）
            if not cover and 'localImg' in data and data['localImg']:
                cover = data['localImg']
        
        cover = normalize_cover_url(cover, novelid)
        
        print('Title:', title)
        print('Author:', author)
        print('NovelId:', novelid)
        print('Chapters:', data.get('maxChapterId') or data.get('chapterCount'))
        print('VIP start:', data.get('vipChapterid') or data.get('vip_start'))
        print('Intro short:', data.get('novelIntroShort') or '')
        print('Intro html:', data.get('novelIntro') or '')
        print('Tags:', data.get('novelTags') or '')
        print('Cover URL:', cover if cover else '(未找到)')
        print('\n完整数据 (JSON):')
        print(json.dumps(data, ensure_ascii=False, indent=2))
    else:
        print('Unexpected basic info structure:', basic)
47

if __name__ == '__main__':
    title = ' '.join(sys.argv[1:2]) if len(sys.argv) > 1 else ''
    cookie = sys.argv[2] if len(sys.argv) > 2 else None
    if not cookie:
        cookie = os.environ.get('JINJIANG_COOKIE')
    
    # 测试用默认 Cookie（如果未提供）
    if not cookie:
        # 可以从这里设置测试 Cookie
        TEST_COOKIE = 'timeOffset_o=-25113.39990234375; smidV2=20251110093349f8ece07e595e431586d9d52a5d1a58e20065d1b8e4581b0f0; token=NDMwOTY3NDF8OTAzMGRmMjI5NGJhMTU3MDBkZmNiZTc3ZDIyZDliN2V8fDIwNioqKioqKipAcXEuY29tfDI4ODE3NTZ8MTI5NjAwfDF856eL5a%2BC5oCdfHxRUeeUqOaIt3wxfHRlbmNlbnR8MXwwfHw%3D; bbsnicknameAndsign=2%257E%2529%2524%25E6%2597%25A0%25E5%25BD%2592; bbstoken=NDMwOTY3NDFfMF83NTNlNTAzMzdjYzgzNGY4OTlmNjU5MzE5Nzk3NjU4Yl8xX194Kys4eGN1OF8x; testcookie=yes; Hm_lvt_bc3b748c21fe5cf393d26c12b2c38d99=1762738429,1762776282,1762825346; HMACCOUNT=6BD7CCFF2EB9EFB7; JJSESS=%7B%22clicktype%22%3A%22%22%7D; reader_nickname=%u65E0%u5F52; Hm_lpvt_bc3b748c21fe5cf393d26c12b2c38d99=1762825380; JJEVER=%7B%22fenzhan%22%3A%22yq%22%2C%22background%22%3A%22%22%2C%22font_size%22%3A%22%22%2C%22isKindle%22%3A%22%22%2C%22shumeideviceId%22%3A%22WHJMrwNw1k/FVLIc7nt0GyI9OPRcuz8nHAtF/IJah5bKL1TzQulqhp9etjSLPJOLAr+RGNz8/3800oSFp/e0R8rLgOV6kGkjtdCW1tldyDzmQI99+chXEiomCeRcf7tnwYp5HxsF710xU/V4b7llpcwCHPPxycwCneu8bpbMPuOTJc3aMEBGDbKyOlsVOXoQiMFBiwKR9bnRiNVCcw5ywKJtXjMqg03OXq6GVHK0ZfYqJj7Aw1ranSyNWLJMpjdRze5685hmEETs%3D1487582755342%22%2C%22nicknameAndsign%22%3A%222%257E%2529%2524%25E6%2597%25A0%25E5%25BD%2592%22%2C%22foreverreader%22%3A%2243096741%22%2C%22desid%22%3A%22DxAedktf2wGl4Ta1o+cCmoPgRwRz0BKM%22%2C%22sms_total%22%3A3%2C%22lastCheckLoginTimePc%22%3A1762825439%7D'
        cookie = TEST_COOKIE
        print('使用测试 Cookie')

    token = extract_token_from_cookie(cookie)
    if not title:
        title = input('请输入书名或 novelId: ').strip()

    # 如果输入 looks like a pure number -> treat as novelId
    if re.fullmatch(r'\d+', title):
        nid = title
        basic = get_novel_basic(nid, cookie=cookie, token=token)
        print_basic_info(basic)
        sys.exit(0)

    print('Searching app API for:', title)
    ids = search_app(title, cookie=cookie, token=token)
    if not ids:
        print('No ids found from app search')
        sys.exit(1)

    print('Found candidate novelIds:', ids)
    # try first few
    for nid in ids[:5]:
        basic = get_novel_basic(nid, cookie=cookie, token=token)
        if basic:
            # basic may contain code/data structure; check presence of novelName
            d = basic.get('data') if isinstance(basic, dict) and 'data' in basic else basic
            if isinstance(d, dict) and (d.get('novelName') or d.get('bookname') or d.get('novelid')):
                print('\n=== Selected novel info ===')
                print_basic_info(basic)
                break
    else:
        print('No usable basic info returned for candidate ids')
