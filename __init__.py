
import re
import time
import random
import gzip
import json
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from queue import Queue, Empty
from urllib.parse import urlparse, unquote, urlencode, parse_qs
from urllib.request import Request, urlopen
import ssl

# Calibre 相关导入
from calibre import random_user_agent
from calibre.ebooks.metadata import check_isbn
from calibre.ebooks.metadata.book.base import Metadata
from calibre.ebooks.metadata.sources.base import Source, Option
from lxml import etree



# HTML 转文本工具（可选依赖）
try:
    from html2text import html2text as _html2text
except Exception:
    _html2text = None


# 国际化函数回退机制
# 确保 `_()` 函数在非 Calibre 环境下也可用，避免语法/编译期错误
# 在 Calibre 环境中，`_()` 用于翻译字符串；在非 Calibre 环境中，直接返回原字符串
try:
    _
except NameError:
    def _(s):
        return s


def html_to_text(html: str) -> str:
    """
    将 HTML 内容转换为纯文本
    
    转换策略：
    1. 优先使用 html2text 库（如果已安装），能更好地处理复杂 HTML 结构
    2. 如果 html2text 不可用或转换失败，使用简单的正则表达式方法：
       - 移除所有 HTML 标签
       - 压缩多个连续空白字符为单个空格
       - 去除首尾空白
    
    Args:
        html: 待转换的 HTML 字符串
        
    Returns:
        转换后的纯文本字符串，如果输入为空则返回空字符串
    """
    if not html:
        return ''
    # 优先使用 html2text 库进行转换（处理更完善）
    if _html2text:
        try:
            return _html2text(html).strip()
        except Exception:
            pass
    # 简单回退方案：使用正则表达式移除 HTML 标签并压缩空白字符
    txt = re.sub(r'<[^>]+>', '', html)  # 移除所有 HTML 标签
    txt = re.sub(r'\s+', ' ', txt).strip()  # 将多个连续空白字符压缩为单个空格
    return txt


def normalize_query(s: str) -> str:
    """
    清洗和规范化搜索关键词，提高搜索匹配率
    
    处理步骤：
    1. 全角字符转半角字符（统一字符宽度）
    2. 去除括号内注记
    3. 中文标点转换为英文标点
    4. 删除不可见字符和特殊符号（保留字母、数字、中日韩文字、常见标点）
    5. 合并多个连续空格为单个空格，去除首尾空白
    
    Args:
        s: 原始搜索关键词

    Returns:
        清洗后的规范化关键词，如果输入为空则直接返回
    """
    if not s:
        return s
    
    # 步骤1：全角字符转半角字符
    # 将全角空格（0x3000）和全角标点符号（0xFF01-0xFF5E）转换为对应的半角字符
    def full2half(u):
        res = []
        for ch in u:
            code = ord(ch)
            if code == 0x3000:  # 全角空格
                res.append(' ')
            elif 0xFF01 <= code <= 0xFF5E:  # 全角标点符号范围
                res.append(chr(code - 0xFEE0))  # 转换为对应的半角字符
            else:
                res.append(ch)
        return ''.join(res)

    s = full2half(s)

    # 步骤2：去除括号内的注记内容（支持中英文括号）
    # 移除圆括号、方括号、中文括号、中文方括号内的所有内容
    s = re.sub(r"\([^\)]*\)", '', s)  # 英文圆括号
    s = re.sub(r"\[[^\]]*\]", '', s)  # 英文方括号
    s = re.sub(r"（[^）]*）", '', s)  # 中文圆括号
    s = re.sub(r"【[^】]*】", '', s)  # 中文方括号

    # 步骤3：替换特殊分隔符为空格，中文标点转换为英文标点
    # 将各种中点、分隔符统一替换为空格
    s = s.replace('·', ' ').replace('•', ' ').replace('・', ' ').replace('\u2026', ' ')
    # 中文标点转换为对应的英文标点
    s = s.replace('：', ':').replace('。', '.').replace('，', ',')

    # 步骤4：删除特殊字符，只保留字母、数字、中日韩文字和常见标点
    # \w: 字母、数字、下划线
    # \u4e00-\u9fff: 中日韩统一表意文字
    # \u3000-\u303F: CJK 符号和标点
    # 保留的标点：连字符(-)、点(.)、冒号(:)、单引号(')、逗号(,)、空格
    s = re.sub(r"[^\w\u4e00-\u9fff\u3000-\u303F\-\.:,' ]+", ' ', s)

    # 步骤5：合并多个连续空格为单个空格，去除首尾空白
    s = re.sub(r"\s+", ' ', s).strip()
    return s


def generate_title_variations(cleaned_title: str):
    """
    基于清洗后的书名生成多个搜索变体，用于提高搜索匹配率
    
    当原始搜索无结果时，通过生成变体可以：
    1. 去除常见标注词（如"完结"、"番外"等），这些词可能影响搜索结果
    2. 提取书名中的关键词，按长度降序排列，优先使用较长的词
    
    变体生成策略：
    - 第一优先级：去除标注词后的书名
    - 第二优先级：按词长度降序排列的关键词（最多5个）
    
    Args:
        cleaned_title: 已清洗的书名（通常来自 normalize_query）
        
    Returns:
        书名变体列表，按优先级排序。如果输入为空则返回空列表
    """
    if not cleaned_title:
        return []

    variations = []
    
    # 策略1：去除常见标注词后生成变体
    # 这些标注词通常不影响书籍的核心信息，但可能干扰搜索匹配
    stopwords = ['完结', '完本', '连载', '番外', '全本', 'txt', '全文', '番外篇']
    short = cleaned_title
    for w in stopwords:
        short = short.replace(w, ' ')  # 将标注词替换为空格
    short = re.sub(r"\s+", ' ', short).strip()  # 压缩空格
    # 如果去除标注词后仍有内容且与原书名不同，则加入变体列表
    if short and short != cleaned_title:
        variations.append(short)

    # 策略2：按词长度降序提取关键词
    # 将书名按空格分词，按长度从长到短排序，优先使用较长的词（通常更具体）
    tokens = [t for t in re.split(r"\s+", cleaned_title) if t]
    tokens = sorted(tokens, key=lambda x: len(x), reverse=True)
    # 最多取前5个关键词，且长度至少为2个字符（避免单字符干扰）
    for t in tokens[:5]:
        if len(t) >= 2 and t not in variations:
            variations.append(t)

    return variations

# ============================================================================
# 晋江文学城核心配置
# ============================================================================

# 基础 URL 配置
JINJIANG_BASE_URL = "https://www.jjwxc.net/"  # 晋江文学城主站
JINJIANG_M_BASE_URL = "https://m.jjwxc.net/"  # 移动端网站
JINJIANG_APP_BASE_URL = "https://app.jjwxc.org/"  # APP 接口域名

# 搜索接口 URL
JINJIANG_SEARCH_WEB_URL = "https://www.jjwxc.net/search.php"  # 网页搜索接口（已弃用，仅作备选）
JINJIANG_SEARCH_APP_URL = "https://app.jjwxc.org/search/searchV3"  # APP 搜索接口 V3（推荐，更稳定）
JINJIANG_SEARCH_APP_ANDROID_API = "https://app.jjwxc.org/androidapi/search"  # Android API 搜索接口（备选）

# 书籍详情接口 URL
JINJIANG_BOOK_DETAIL_WEB_URL = "https://www.jjwxc.net/onebook.php?novelid=%s"  # 网页详情页（备选方案）
JINJIANG_BOOK_DETAIL_APP_URL = "https://app.jjwxc.org/androidapi/getBookDetail"  # APP 详情接口（推荐）

# 正则表达式：从 URL 中提取书籍 ID
JINJIANG_BOOK_ID_PATTERN = re.compile(r"novelid=(\d+)")

# 插件元信息
PROVIDER_NAME = "Jinjiang Books"  # 插件显示名称
PROVIDER_ID = "jinjiang_enhanced"  # 插件唯一标识符（用于 Calibre 的 identifier）
PROVIDER_VERSION = (0, 3, 0) # 插件版本号
PROVIDER_AUTHOR = 'Qishan '  # 插件作者

# 并发配置
JINJIANG_CONCURRENCY_SIZE = 5  # 默认并发请求数（建议不超过5，避免触发反爬虫机制）

# 搜索类型映射（对应晋江 APP API 中的搜索类型参数）
# 这些类型值用于指定搜索范围：书名、作者、角色、ID 等
SEARCH_TYPE_MAP = {
    "book": 1,        # 按书名搜索（默认类型）
    "author": 2,      # 按作者搜索（JSON 格式：#关键词#）
    "protagonist": 4, # 按主角搜索（JSON 格式：主角#关键词#）
    "supporting": 5,  # 按配角搜索（JSON 格式：配角#关键词#）
    "other": 6,       # 按其他关键字搜索（JSON 格式：其他#关键词#）
    "id": 7           # 按作品 ID 搜索（JSON 格式：ID#关键词#）
}


class JinjiangBookSearcher:
    """
    晋江书籍搜索器
    
    负责从晋江文学城搜索和获取书籍信息，支持：
    - 多种搜索类型（书名、作者、角色、ID等）
    - APP API 和网页接口的自动切换
    - 并发请求处理
    - 登录 Cookie 支持
    """
    
    def __init__(self, *args, **kwargs):
        """
        初始化搜索器
        
        兼容性构造函数：接受多种参数形式，避免因参数名不同导致的错误。
        支持位置参数和关键字参数，优先使用关键字参数。
        
        参数说明：
        - concurrency_size / max_workers: 并发请求数（默认5）
        - jinjiang_delay_enable: 是否启用随机延迟（默认True，用于避免反爬虫）
        - jinjiang_login_cookie: 登录后的 Cookie 字符串（可选，用于访问 VIP 内容）
        - jinjiang_search_with_author: 是否在搜索时包含作者名（默认False）
        - jinjiang_prefer_app_api: 是否优先使用 APP API（默认True）
        
        Args:
            *args: 位置参数，按顺序为：max_workers, delay_enable, login_cookie
            **kwargs: 关键字参数，支持上述所有参数名
        """
        # 解析位置参数（兼容旧版本调用方式）
        # 位置参数顺序：max_workers, jinjiang_delay_enable, jinjiang_login_cookie
        pos_max_workers = args[0] if len(args) > 0 else None
        pos_delay = args[1] if len(args) > 1 else None
        pos_cookie = args[2] if len(args) > 2 else None

        # 并发参数处理：优先级为 kwargs > 位置参数 > 默认值
        # 支持 concurrency_size 和 max_workers 两种参数名
        concurrency = kwargs.pop('concurrency_size', None)
        if concurrency is None:
            concurrency = kwargs.pop('max_workers', None)
        if concurrency is None:
            concurrency = pos_max_workers
        try:
            self.max_workers = int(concurrency) if concurrency is not None else JINJIANG_CONCURRENCY_SIZE
        except Exception:
            self.max_workers = JINJIANG_CONCURRENCY_SIZE

        # 其他配置选项（带默认值）
        # 如果提供了位置参数则优先使用位置参数，否则使用 kwargs 或默认值
        self.jinjiang_delay_enable = kwargs.pop('jinjiang_delay_enable', pos_delay if pos_delay is not None else True)
        self.jinjiang_login_cookie = kwargs.pop('jinjiang_login_cookie', pos_cookie if pos_cookie is not None else None)
        self.jinjiang_search_with_author = kwargs.pop('jinjiang_search_with_author', False)
        self.jinjiang_prefer_app_api = kwargs.pop('jinjiang_prefer_app_api', True)

        # 初始化 HTML 解析器（用于网页接口的回退方案）
        self.book_parser = JinjiangBookHtmlParser()
        
        # 初始化线程池，限制最大线程数在 1-20 之间，避免误设置过大导致资源浪费
        max_workers_safe = max(1, min(self.max_workers, 20))
        self.thread_pool = ThreadPoolExecutor(max_workers=max_workers_safe, thread_name_prefix='jinjiang_async')

        # 从 Cookie 中提取 sid（会话ID，用于 APP 接口的身份验证）
        self.sid = self.extract_sid_from_cookie()

    def extract_sid_from_cookie(self):
        """
        从登录 Cookie 中提取 sid（会话ID）
        
        sid 是晋江 APP 接口进行身份验证所需的参数。本方法尝试从 Cookie 字符串中
        提取 sid，支持多种 Cookie 格式和字段名。
        
        提取策略（按优先级）：
        1. 直接查找 sid= 字段
        2. 查找 token= 或 bbstoken= 字段（URL 解码后使用）
        3. 从 JJSESS Cookie 的 JSON 数据中提取（支持 sid/sidkey/token 字段）
        4. 其他常见字段（如 JJEVER，但通常不包含 sid）
        
        Returns:
            提取到的 sid 字符串，如果未找到则返回 None
        """
        if not self.jinjiang_login_cookie:
            return None

        c = self.jinjiang_login_cookie
        
        # 策略1：直接查找 sid= 字段（最常见的情况）
        m = re.search(r"sid=([^;\s]+)", c)
        if m:
            return m.group(1)

        # 策略2：查找 token= 或 bbstoken= 字段
        # 这些字段的值可能经过 URL 编码，需要解码
        m = re.search(r"token=([^;\s]+)", c)
        if m:
            return unquote(m.group(1))
        m = re.search(r"bbstoken=([^;\s]+)", c)
        if m:
            return unquote(m.group(1))

        # 策略3：从 JJSESS Cookie 中提取
        # JJSESS 可能包含 JSON 格式的数据，其中包含 sid/sidkey/token 等字段
        m = re.search(r"JJSESS=([^;]+)", c)
        if m:
            raw = unquote(m.group(1))
            try:
                # 尝试将 JJSESS 的值解析为 JSON 对象
                j = json.loads(raw)
                if isinstance(j, dict):
                    # 在 JSON 对象中查找 sid、sidkey 或 token 字段
                    for key in ('sid', 'sidkey', 'token'):
                        if key in j and j[key]:
                            return j[key]
            except Exception:
                # 如果解析 JSON 失败，尝试用正则表达式从字符串中提取 sidkey
                m2 = re.search(r"sidkey\W*[:=]\W*'?\"?([\w-]+)'?\"?", raw)
                if m2:
                    return m2.group(1)

        # 策略4：尝试其他常见字段（如 JJEVER）
        # 注意：JJEVER 通常包含用户信息，但不直接包含 sid，因此不做进一步解析
        m = re.search(r"JJEVER=([^;\s]+)", c)
        if m:
            # JJEVER 有时包含用户信息，但不是直接 sid；不做进一步解析
            return None

        return None

    def parse_search_keyword(self, query):
        """
        解析搜索关键词，识别搜索类型
        
        支持多种搜索关键词格式，用于指定不同的搜索类型（书名、作者、角色、ID等）。
        解析后的搜索类型将传递给晋江 API 进行相应类型的搜索。
        
        支持的格式：
        1. URL 参数格式：t=2 关键词 或 type=2 关键词（数字对应 SEARCH_TYPE_MAP）
        2. 中文前缀格式：作者:xxx、主角:xxx、配角:xxx、其它:xxx、ID:xxx
        3. 英文前缀格式：author:xxx、protagonist:xxx、supporting:xxx、other:xxx、id:xxx
        4. JSON 规则格式：#关键词#（作者）、主角#关键词#、配角#关键词#、其他#关键词#、ID#关键词#
        5. 默认：无前缀时按书名搜索
        
        Args:
            query: 原始搜索关键词字符串
            
        Returns:
            tuple: (清洗后的关键词, 搜索类型代码)
                  搜索类型代码对应 SEARCH_TYPE_MAP 中的值，默认为 1（书名搜索）
        """
        search_type = SEARCH_TYPE_MAP["book"]  # 默认搜索书名

        if not query:
            return query, search_type

        q = query.strip()

        # 格式1：URL 参数格式（t=2 或 type=2）
        # 示例: "t=2 我喜欢你的信息素" 或 "type=2 作者名"
        m = re.match(r'^(?:t|type)\s*[:=]\s*(\d+)\s*(.*)$', q, re.I)
        if m:
            try:
                tnum = int(m.group(1))
                rest = m.group(2).strip()
                # 验证类型代码是否有效
                if tnum in SEARCH_TYPE_MAP.values():
                    return (rest or query, tnum)
            except Exception:
                pass

        # 格式2：中文/英文前缀格式（支持中英文冒号）
        # 作者搜索：作者:xxx 或 author:xxx
        m = re.match(r'^(?:作者|author)\s*[:：]\s*(.+)$', q, re.I)
        if m:
            return m.group(1).strip(), SEARCH_TYPE_MAP['author']

        # 主角搜索：主角:xxx 或 protagonist:xxx
        m = re.match(r'^(?:主角|protagonist)\s*[:：]\s*(.+)$', q, re.I)
        if m:
            return m.group(1).strip(), SEARCH_TYPE_MAP['protagonist']

        # 配角搜索：配角:xxx 或 supporting:xxx
        m = re.match(r'^(?:配角|supporting)\s*[:：]\s*(.+)$', q, re.I)
        if m:
            return m.group(1).strip(), SEARCH_TYPE_MAP['supporting']

        # 其他关键字搜索：其它:xxx、其他:xxx 或 other:xxx
        m = re.match(r'^(?:其它|其他|other)\s*[:：]\s*(.+)$', q, re.I)
        if m:
            return m.group(1).strip(), SEARCH_TYPE_MAP['other']

        # ID 搜索：ID:xxx、文章ID:xxx 或 id:xxx
        m = re.match(r'^(?:ID|文章ID|id)\s*[:：]\s*(.+)$', q, re.I)
        if m:
            return m.group(1).strip(), SEARCH_TYPE_MAP['id']

        # 格式3：JSON 规则格式（兼容旧版本）
        # #关键词# 表示按作者搜索
        if q.startswith("#") and q.endswith("#"):
            inner = q.strip("#").strip()
            return inner, SEARCH_TYPE_MAP['author']
        # 主角#关键词# 表示按主角搜索
        elif q.startswith("主角#") and q.endswith("#"):
            inner = q[len("主角#"):-1].strip()
            return inner, SEARCH_TYPE_MAP['protagonist']
        # 配角#关键词# 表示按配角搜索
        elif q.startswith("配角#") and q.endswith("#"):
            inner = q[len("配角#"):-1].strip()
            return inner, SEARCH_TYPE_MAP['supporting']
        # 其他#关键词# 表示按其他关键字搜索
        elif q.startswith("其他#") and q.endswith("#"):
            inner = q[len("其他#"):-1].strip()
            return inner, SEARCH_TYPE_MAP['other']
        # ID#关键词# 表示按作品 ID 搜索
        elif q.startswith("ID#") and q.endswith("#"):
            inner = q[len("ID#"):-1].strip()
            return inner, SEARCH_TYPE_MAP['id']

        # 默认：无前缀时按书名搜索
        return query, search_type

    def search_via_app_api(self, query, search_type, log):
        """
        通过 APP 搜索接口获取书籍列表
        
        APP 接口相比网页接口更稳定，反爬虫机制较弱，返回的数据格式也更规范。
        本方法会尝试多个 APP 搜索接口，按优先级依次尝试，直到成功获取结果。
        
        搜索流程：
        1. 检查是否有 sid（会话ID），无 sid 则无法使用 APP 接口
        2. 优先尝试 searchV3 接口（更稳定）
        3. 如果 searchV3 失败，回退到 androidapi/search 接口
        4. 解析返回的 JSON 数据，提取书籍 ID 并构建详情页 URL
        
        Args:
            query: 搜索关键词（已清洗）
            search_type: 搜索类型代码（对应 SEARCH_TYPE_MAP 中的值）
            log: 日志记录器对象
            
        Returns:
            书籍详情页 URL 列表，如果搜索失败或未找到结果则返回空列表
        """
        book_urls = []
        
        # 检查是否有 sid（APP 接口必需的身份验证参数）
        if not self.sid:
            log.warning("APP接口需要登录Cookie（含sid），切换到网页搜索")
            return book_urls
        
        # 配置 SSL 上下文：禁用主机名验证和证书验证
        # 这可以避免本地证书问题，但会降低安全性（仅用于开发环境）
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE

        tried = []  # 记录尝试过的 URL，用于调试

        # 辅助函数：从 JSON 响应中提取书籍列表
        # 兼容多种可能的 JSON 结构（不同接口可能返回不同格式）
        def _extract_books_from_json(data):
            books_list = []
            if not data:
                return books_list
            
            # 兼容多种 JSON 响应结构
            if isinstance(data, dict):
                # 结构1：{ code:0, data: { books: [...] } }（标准响应格式）
                if data.get('code') == 0 and data.get('data'):
                    d = data.get('data')
                    # data 对象中可能使用不同的键名存储书籍列表
                    books_list = d.get('books') or d.get('results') or d.get('items') or d.get('list') or []
                else:
                    # 结构2：直接在顶层包含书籍列表的键
                    books_list = data.get('books') or data.get('results') or data.get('items') or data.get('list') or []
            elif isinstance(data, list):
                # 结构3：直接返回数组
                books_list = data
            return books_list

        # 策略1：优先尝试 searchV3 接口（更稳定可靠）
        try:
            params = {'keyword': query, 'type': search_type, 'page': 1}
            if self.sid:
                params['token'] = self.sid
            url = JINJIANG_SEARCH_APP_URL + '?' + urlencode(params)
            tried.append(url)
            log.info(f"Trying APP searchV3 URL: {url}")
            res = urlopen(Request(url, headers=self.get_headers(), method='GET'), timeout=15, context=ctx)
            if res.status in (200, 201):
                content = self.get_res_content(res)
                try:
                    data = json.loads(content)
                except Exception:
                    data = None
                books = _extract_books_from_json(data)
                if books:
                    for book in books:
                        novelid = book.get('novelid') or book.get('bookId') or book.get('id')
                        if novelid and len(book_urls) < self.max_workers:
                            detail_url = JINJIANG_BOOK_DETAIL_WEB_URL % novelid
                            book_urls.append(detail_url)
                            log.info(f"searchV3 found book: {book.get('bookname') or book.get('title')} (ID: {novelid})")
                    return book_urls
                else:
                    log.debug('searchV3 returned no books')
        except Exception as e:
            log.debug(f"searchV3 request failed: {e}")

        # 策略2：如果 searchV3 失败，回退到 androidapi/search 接口
        try:
            params = {'versionCode': 282, 'keyword': query, 'type': search_type, 'page': 1}
            if self.sid:
                params['token'] = self.sid
            url2 = JINJIANG_SEARCH_APP_ANDROID_API + '?' + urlencode(params)
            tried.append(url2)
            log.info(f"APP androidapi search URL: {url2}")
            res2 = urlopen(Request(url2, headers=self.get_headers(), method='GET'), timeout=15, context=ctx)
            if res2.status in (200, 201):
                content2 = self.get_res_content(res2)
                try:
                    data2 = json.loads(content2)
                except Exception:
                    data2 = None
                books2 = _extract_books_from_json(data2)
                if books2:
                    for book in books2:
                        novelid = book.get('novelid') or book.get('bookId') or book.get('id')
                        if novelid and len(book_urls) < self.max_workers:
                            detail_url = JINJIANG_BOOK_DETAIL_WEB_URL % novelid
                            book_urls.append(detail_url)
                            log.info(f"androidapi found book: {book.get('bookname') or book.get('title')} (ID: {novelid})")
                    return book_urls
                else:
                    log.debug('androidapi returned no books')
                    # 把响应内容写入日志（前2000字符），便于诊断
                    try:
                        snippet = (content2 or '')[:2000]
                        log.debug(f"androidapi response snippet: {snippet}")
                    except Exception:
                        pass
        except Exception as e:
            log.debug(f"androidapi request failed: {e}")

        # 若都未命中，记录尝试过的 URL 以便排查
        if tried:
            log.debug(f"Tried APP search URLs: {', '.join(tried)}")

        return book_urls

    def search_via_web(self, query, search_type, log):
        """
        网页搜索接口（已弃用）
        
        注意：网页搜索功能已移除，插件现在仅使用 APP 接口进行搜索。
        保留此方法是为了保持接口兼容性，实际调用时直接返回空列表。
        
        Args:
            query: 搜索关键词
            search_type: 搜索类型代码
            log: 日志记录器对象
            
        Returns:
            空列表（网页搜索已禁用）
        """
        log.debug('search_via_web called but web search has been removed; returning empty list')
        return []

    def load_book_urls_new(self, query, log):
        """
        统一的搜索入口方法
        
        解析搜索关键词，识别搜索类型，然后调用相应的搜索接口获取书籍列表。
        目前仅使用 APP 接口进行搜索（网页搜索已弃用）。
        
        Args:
            query: 原始搜索关键词（可能包含类型前缀）
            log: 日志记录器对象
            
        Returns:
            书籍详情页 URL 列表，如果搜索失败则返回空列表
        """
        # 步骤1：解析搜索关键词，识别搜索类型
        query, search_type = self.parse_search_keyword(query)
        # 获取搜索类型的名称（用于日志显示）
        type_name = [k for k, v in SEARCH_TYPE_MAP.items() if v == search_type][0] if search_type in SEARCH_TYPE_MAP.values() else 'unknown'
        log.info(f"Search query: {query}, type: {search_type} ({type_name})")
        
        # 步骤2：调用 APP 接口进行搜索（网页搜索已弃用）
        book_urls = self.search_via_app_api(query, search_type, log)
        if not book_urls:
            log.info('APP接口未返回结果')
        return book_urls

    def search_books(self, query, authors, log):
        """
        搜索书籍并获取详细信息
        
        执行搜索操作，获取匹配的书籍列表，然后并发加载每本书的详细信息。
        支持在搜索时自动添加作者名以提高搜索准确性。
        
        Args:
            query: 搜索关键词
            authors: 作者列表（可选，如果启用 jinjiang_search_with_author 则会添加到搜索关键词中）
            log: 日志记录器对象
            
        Returns:
            书籍信息字典列表，每个字典包含一本书的完整元数据
        """
        # 如果启用了"搜索时包含作者"选项，将作者名添加到搜索关键词中
        if self.jinjiang_search_with_author and authors:
            author_str = ' '.join(authors)
            query = f'{query} {author_str}'
            log.info(f"Enhanced search query: {query}")
        
        # 步骤1：获取匹配的书籍 URL 列表
        book_urls = self.load_book_urls_new(query, log)
        books = []
        
        # 步骤2：并发加载每本书的详细信息
        # 使用线程池并发请求，提高加载效率
        futures = [self.thread_pool.submit(self.load_book, url, log) for url in book_urls]
        
        # 步骤3：收集所有并发任务的结果
        for future in as_completed(futures):
            try:
                book = future.result()
                if book:
                    books.append(book)
            except Exception as e:
                log.error(f"Future error: {e}")
        
        return books

    # 榜单/发现类功能已完全移除以简化插件行为（仅保留按书名/作者的识别与封面下载）

    def extract_novelid(self, href):
        """
        从 URL 中提取书籍 ID（novelid）
        
        支持多种 URL 格式：
        - 网页格式：https://www.jjwxc.net/onebook.php?novelid=123456
        - APP 格式：可能使用 bookId 参数
        
        Args:
            href: 书籍详情页 URL
            
        Returns:
            书籍 ID 字符串，如果无法提取则返回 None
        """
        if not href:
            return None
        
        # 解析 URL 查询参数
        params = parse_qs(urlparse(href).query)
        
        # 优先查找 novelid 参数（网页格式）
        novelids = params.get('novelid', [])
        if novelids:
            return novelids[0]
        
        # 备选：查找 bookId 参数（APP 接口格式）
        book_ids = params.get('bookId', [])
        return book_ids[0] if book_ids else None

    def build_book_detail_url(self, novelid):
        """
        构建书籍详情页 URL
        
        Args:
            novelid: 书籍 ID
            
        Returns:
            完整的书籍详情页 URL 字符串
        """
        return JINJIANG_BOOK_DETAIL_WEB_URL % novelid

    def load_book(self, url, log):
        """
        加载并解析书籍详情信息
        
        加载策略：
        1. 如果启用了随机延迟，先执行随机延迟（避免反爬虫）
        2. 优先使用 APP 详情接口（如果已配置 sid 且启用 prefer_app_api）
        3. 如果 APP 接口失败，回退到网页详情页解析
        
        Args:
            url: 书籍详情页 URL
            log: 日志记录器对象
            
        Returns:
            书籍信息字典，包含标题、作者、简介、封面等字段。如果加载失败则返回 None
        """
        book = None
        start_time = time.time()
        
        # 步骤1：如果启用了延迟，先执行随机延迟（避免触发反爬虫机制）
        if self.jinjiang_delay_enable:
            self.random_sleep(log)
        
        # 步骤2：从 URL 中提取书籍 ID
        novelid = self.extract_novelid(url)
        if not novelid:
            log.error(f"Cannot extract novelid from URL: {url}")
            return None
        
        # 步骤3：优先使用 APP 详情接口（数据更结构化，解析更可靠）
        if self.jinjiang_prefer_app_api and self.sid:
            book = self.load_book_via_app_api(novelid, log)
            if book:
                elapsed = (time.time() - start_time) * 1000
                log.info(f"APP API loaded book: {book['title']} (time: {elapsed:.0f}ms)")
                return book
            log.info(f"APP detail API failed, fallback to web page: {url}")
        
        # 步骤4：如果 APP 接口失败，回退到网页详情页解析（兜底方案）
        try:
            res = urlopen(Request(url, headers=self.get_headers(), method='GET'), timeout=10)
            if res.status in [200, 201]:
                elapsed = (time.time() - start_time) * 1000
                log.info(f"Web loaded book: {url} (time: {elapsed:.0f}ms)")
                book_detail_content = self.get_res_content(res)
                book = self.book_parser.parse_book(url, book_detail_content, log)
        except Exception as e:
            log.error(f"Web load book failed: {e}")
        
        return book

    def fetch_and_merge_other_info(self, novelid, book, log=None, base_data=None):
        """
        从 APP 的 getnovelOtherInfo 接口获取扩展信息并合并到书籍描述中
        
        本方法从晋江 APP 的扩展信息接口获取书籍的详细元数据，包括：
        - 文章类型、全文字数、非V点击、文章积分
        - 签约状态、收藏数、排名、营养值/评分
        - 扩展简介、标签、主角/配角/其他角色
        - 风格、视角、系列信息、作者留言等
        
        这些信息会被格式化后合并到 book 字典的 description 和 description_html 字段中，
        同时部分信息也会更新到 book 的其他字段（如 tags、comments 等）。
        
        注意：
        - 所有错误都会被捕获，不会抛出异常，以免影响主流程
        - 异常信息会记录到 log.debug 中，便于调试
        - 如果接口请求失败或解析失败，方法会静默返回，不影响书籍基本信息
        
        Args:
            novelid: 书籍 ID
            book: 书籍信息字典（会被修改，添加扩展信息）
            log: 日志记录器对象（可选）
            base_data: 基础数据字典（可选，用于补充信息源）
        """
        if not novelid:
            return

        try:
            # 步骤1：构建请求 URL 并发送请求
            params = {'versionCode': 279, 'novelId': novelid, 'type': 'novelbasicinfo'}
            url = 'https://app.jjwxc.org/androidapi/getnovelOtherInfo' + '?' + urlencode(params)
            
            # 配置 SSL 上下文（禁用验证以避免证书问题）
            ctx = ssl.create_default_context()
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE
            
            if log:
                log.debug(f'Trying other-info URL: {url}')
            res = urlopen(Request(url, headers=self.get_headers(), method='GET'), timeout=10, context=ctx)
            if res.status not in (200, 201):
                return
            
            # 步骤2：解析响应内容
            content = self.get_res_content(res)
            try:
                data = json.loads(content)
            except Exception:
                data = None

            # 步骤3：从响应中提取有效数据对象（兼容多种 JSON 封装格式）
            other = None
            if isinstance(data, dict):
                # 尝试从常见嵌套键中提取数据
                for k in ('data', 'a', 'novelLeave', 'novel', 'result'):
                    if k in data and data[k]:
                        other = data[k]
                        break
                # 如果未找到嵌套数据，使用整个字典
                if other is None:
                    other = data
            elif isinstance(data, list) and data:
                # 如果是数组，取第一个元素
                other = data[0]
            else:
                other = data

            if not other:
                return

            # 辅助函数：从对象中提取字段值（支持多种键名变体和嵌套查找）
            # 特点：
            # - 大小写不敏感
            # - 下划线不敏感（支持 camelCase 和 snake_case）
            # - 支持嵌套查找（在 data/novel/result 等嵌套对象中查找）
            def pick(obj, *keys):
                if not obj:
                    return ''
                try:
                    if isinstance(obj, dict):
                        # 创建小写键名映射（用于大小写不敏感查找）
                        lowmap = {str(k).lower(): k for k in obj.keys()}
                        for k in keys:
                            # 策略1：精确匹配
                            if k in obj and obj[k]:
                                return obj[k]
                            # 策略2：大小写不敏感匹配
                            lk = k.lower()
                            if lk in lowmap:
                                v = obj.get(lowmap[lk])
                                if v:
                                    return v
                    # 策略3：在嵌套对象中查找（递归）
                    for nest in ('data', 'a', 'novel', 'result'):
                        nested = obj.get(nest) if isinstance(obj, dict) else None
                        if nested:
                            v = pick(nested, *keys)
                            if v:
                                return v
                    # 策略4：如果是数组，在第一个元素中查找
                    if isinstance(obj, list) and obj:
                        return pick(obj[0], *keys)
                except Exception:
                    return ''
                return ''

            # 步骤4：构建数据源列表（按优先级排序）
            # 优先级：other（扩展信息） > base_data（基础数据） > book（已有书籍信息）
            sources = []
            if isinstance(other, dict):
                sources.append(other)
            if isinstance(base_data, dict):
                sources.append(base_data)
            if isinstance(book, dict):
                sources.append(book)

            # 辅助函数：从多个数据源中查找字段值（按优先级）
            def pick_any(*keys):
                for src in sources:
                    val = pick(src, *keys)
                    if isinstance(val, str):
                        if val.strip():
                            return val.strip()
                    elif val is not None:
                        return val
                return ''

            # 步骤5：开始提取和格式化各种扩展信息
            lines = []  # 用于存储格式化的描述行

            # 提取留言/作者留言（novelLeave）
            leave_obj = None
            for src in sources:
                if isinstance(src, dict):
                    for k in ('novelLeave', 'leave', 'novelleave'):
                        if k in src and src[k]:
                            leave_obj = src[k]
                            break
                if leave_obj:
                    break
            leave_display = ''
            if leave_obj:
                try:
                    ld_back = leave_obj.get('leaveDateBack') or leave_obj.get('leave_date_back') or ''
                    ld = leave_obj.get('leaveDate') or leave_obj.get('leaveDateStr') or leave_obj.get('leave_date') or ''
                    lcont = leave_obj.get('leaveContent') or leave_obj.get('leave_content') or ''
                    leave_lines = [str(x) for x in (ld_back, lcont, ld) if x]
                    if leave_lines:
                        leave_display = '\n'.join(leave_lines) + '\n&lrm;\n'
                except Exception:
                    leave_display = ''

            # 文章类型：加入 lines 并合并到 tags
            novel_class = pick_any('novelClass', 'novel_class', 'category')
            if novel_class:
                try:
                    existing_tags = book.get('tags') or []
                    if not isinstance(existing_tags, list):
                        existing_tags = [t.strip() for t in str(existing_tags).split(',') if t.strip()]
                    merged = [novel_class] + [t for t in existing_tags if str(t).strip() and str(t).strip() != str(novel_class).strip()]
                    seen = set()
                    uniq = []
                    for t in merged:
                        if t and t not in seen:
                            seen.add(t)
                            uniq.append(t)
                    book['tags'] = uniq
                except Exception:
                    book['tags'] = [novel_class]

            # 全文字数
            novel_size = pick_any('novelSize', 'novel_size', 'novelSizeShow', 'novelsizeformat', 'word_count', 'words')
            if isinstance(novel_size, (list, dict)):
                try:
                    novel_size = html_to_text(json.dumps(novel_size, ensure_ascii=False))
                except Exception:
                    novel_size = ''

            # 非V点击、积分
            novip_clicks = pick_any('novip_clicks', 'novipClicks', 'novipClick', 'novipclicks')
            novel_score = pick_any('novelScore', 'score', 'novelscore')

            # 签约状态：仅当字段存在时才写入，支持多种表示形式（字符串数字/布尔）
            is_sign = pick_any('isSign', 'is_sign', 'issign')
            signed_display = ''
            try:
                if is_sign is not None and str(is_sign).strip() != '':
                    raw_sign = str(is_sign).strip()
                    signed_flag = False
                    if isinstance(is_sign, bool):
                        signed_flag = bool(is_sign)
                    else:
                        lr = raw_sign.lower()
                        if lr in ('1', 'true', 'yes'):
                            signed_flag = True
                        else:
                            try:
                                if re.match(r'^\d+$', raw_sign) and int(raw_sign) > 0:
                                    signed_flag = True
                            except Exception:
                                signed_flag = False
                    signed_display = '已签约' if signed_flag else '未签约'
                    if log:
                        try:
                            log.debug(f'fetch_and_merge_other_info isSign raw="{raw_sign}" -> {signed_display}')
                        except Exception:
                            pass
            except Exception:
                if log:
                    try:
                        log.debug('Error parsing isSign value')
                    except Exception:
                        pass

            if not signed_display:
                signed_display = '未签约'

            # 收藏/排名/营养
            befav = pick_any('novelbefavoritedcount', 'befavoritedcount', 'favoriteCount')
            nutrition = pick_any('nutrition_novel', 'nutrition', 'nutritionNovel')
            ranking_raw = pick_any('ranking', 'rank', 'ranking_str')
            ranking_number = ''
            if ranking_raw:
                try:
                    m = re.search(r"(\d+)", str(ranking_raw))
                    if m:
                        ranking_number = m.group(1)
                except Exception:
                    ranking_number = ''
            if not befav:
                befav = '0'
            if not ranking_number:
                ranking_number = '暂无排名'
            nutrition_display = str(nutrition) if nutrition else '0'

            # 拓展简介（novelIntro）——映射到 book['comments']，同时也保留到 lines 以供 description 合并
            intro = pick_any('novelIntro', 'novelintro', 'novelIntroShort', 'novelIntroShortHtml', 'description', 'desc')
            if intro:
                intro_txt = html_to_text(str(intro))
                # map to comments (append if exists)
                try:
                    exist_comments = book.get('comments') or ''
                    if exist_comments:
                        book['comments'] = str(exist_comments).strip() + '\n\n' + intro_txt
                    else:
                        book['comments'] = intro_txt
                except Exception:
                    book['comments'] = intro_txt
                intro_txt = intro_txt.replace('立意:', '立意：').replace('立意 :', '立意：')
            # 标签（novelTags）——合并到 book['tags'] 并加入 lines 供描述使用
            tags = pick_any('novelTags', 'novel_tags', 'tags')
            parsed_tags = []
            try:
                if isinstance(tags, str):
                    parsed_tags = [t.strip() for t in re.split(r'[,&/;，、\s]+', tags) if t.strip()]
                elif isinstance(tags, list):
                    parsed_tags = [str(t).strip() for t in tags if str(t).strip()]
            except Exception:
                parsed_tags = []
            tags_line = ''
            if parsed_tags:
                # merge into book['tags'] preserving order and uniqueness
                try:
                    existing_tags = book.get('tags') or []
                    if not isinstance(existing_tags, list):
                        existing_tags = [t.strip() for t in str(existing_tags).split(',') if t.strip()]
                    merged = existing_tags + [t for t in parsed_tags if t not in existing_tags]
                    # dedupe while preserving order
                    seen = set()
                    uniq = []
                    for t in merged:
                        if t and t not in seen:
                            seen.add(t)
                            uniq.append(t)
                    book['tags'] = uniq
                except Exception:
                    book['tags'] = parsed_tags
                tags_line = '标签：' + '&nbsp;'.join(parsed_tags)

            # 主角/配角/其它
            prot = pick_any('protagonist', 'protagonists', '主角')
            costar = pick_any('costar', 'coStar', '配角')
            other_roles = pick_any('other', 'others')
            role_parts = []
            def clean_role(val):
                if not val:
                    return ''
                txt = html_to_text(str(val))
                return re.sub(r'^(主角|配角|其它|其他)[:：]\s*', '', txt)
            prot_clean = clean_role(prot)
            costar_clean = clean_role(costar)
            other_clean = clean_role(other_roles)
            if prot_clean:
                role_parts.append(f'主角：{prot_clean}')
            if costar_clean:
                role_parts.append(f'配角：{costar_clean}')
            if other_clean:
                role_parts.append(f'其它：{other_clean}')
            roles_comb = ''.join(role_parts)

            # 风格/视角/系列（保持与参考模板一致的布局）
            style = pick_any('novelStyle', 'style')
            mainview = pick_any('mainview', 'view')
            series = pick_any('series')
            style_line = f"风格：{style or ''}&nbsp;&nbsp;&nbsp;&nbsp;视角：{mainview or ''}" if (style or mainview) else ''
            series_line = f'所属：{series}' if series else ''

            # 构建最终描述块，参考提供的模板
            first_line = (leave_display or '') + (f"文章类型：{novel_class}" if novel_class else '')
            if first_line:
                lines.append(first_line)
            elif leave_display:
                lines.append(leave_display.rstrip('\n'))

            if novel_size:
                lines.append(f'全文字数：{novel_size}')
            if novip_clicks:
                lines.append(f'非V点击：{novip_clicks}')
            if novel_score:
                lines.append(f'文章积分：{novel_score}')
            if signed_display:
                lines.append(f'签约状态：{signed_display}')

            lines.append('&lrm;')
            lines.append(f'⭐&nbsp;{befav}丨👍&nbsp;No.{ranking_number}丨🍼&nbsp;{nutrition_display}')
            lines.append('&lrm;')

            if intro and intro_txt:
                lines.append(intro_txt)
                lines.append('&lrm;')

            if tags_line:
                lines.append(tags_line)
            if roles_comb:
                lines.append(roles_comb)
            if style_line:
                lines.append(style_line)
            if series_line:
                lines.append(series_line)

            # 移除原先注入到描述中的前端 JS 处理提示，避免在 Calibre 简介中出现杂项文本

            # 把 lines 合并到 description 字段（HTML + 纯文本）
            if lines:
                extra_html = '<br>'.join([str(x).replace('\n', '<br>') for x in lines])
                extra_text = '\n'.join([str(x) for x in lines])
                # 合并到已有简介
                try:
                    exist_html = book.get('description_html') or book.get('description') or ''
                    if exist_html:
                        book['description_html'] = str(exist_html) + '<br><br>' + extra_html
                    else:
                        book['description_html'] = extra_html
                except Exception:
                    book['description_html'] = extra_html
                try:
                    exist_txt = book.get('description') or ''
                    if exist_txt:
                        book['description'] = str(exist_txt) + '\n\n' + extra_text
                    else:
                        book['description'] = extra_text
                except Exception:
                    book['description'] = extra_text

        except Exception as e:
            if log:
                try:
                    log.debug(f'fetch_and_merge_other_info error: {e}')
                except Exception:
                    pass
            return


    def load_book_via_app_api(self, novelid, log):
        """
        通过 APP 详情接口获取书籍数据（多端兼容）
        
        本方法尝试多个已知的 APP 详情接口，使用不同的参数名变体，以兼容不同的接口版本。
        如果某个接口返回可用的 JSON 数据，则解析并返回书籍信息字典，否则返回 None。
        
        尝试策略：
        1. 尝试多个接口端点（CDN、主站等）
        2. 对每个端点，尝试不同的参数名（novelid、novelId、bookId、bookid）
        3. 兼容不同的 JSON 返回结构（code/data 封装、直接对象、数组等）
        4. 验证解析结果的关键字段（title、authors），缺失则视为失败
        
        Args:
            novelid: 书籍 ID
            log: 日志记录器对象
            
        Returns:
            书籍信息字典，如果所有接口都失败则返回 None
        """
        # 候选接口端点列表（按优先级排序）
        # 优先尝试 CDN 端点（通常更快），然后回退到主站端点
        detail_endpoints = [
            'https://app-cdn.jjwxc.net/androidapi/novelbasicinfo',  # CDN 端点（推荐）
            JINJIANG_BOOK_DETAIL_APP_URL,  # 标准 APP 详情接口
            'https://app.jjwxc.org/androidapi/novelbasicinfo'  # 备选端点
        ]

        # 配置 SSL 上下文：禁用验证以避免本地证书问题
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE

        tried = []  # 记录尝试过的 URL，用于调试

        # 参数名变体列表（不同接口可能使用不同的参数名）
        param_variants = [
            {'novelid': novelid},   # 小写
            {'novelId': novelid},  # 驼峰
            {'bookId': novelid},   # bookId 格式
            {'bookid': novelid}    # 全小写 bookid
        ]

        # 遍历所有端点和参数变体组合
        for endpoint in detail_endpoints:
            for base_params in param_variants:
                params = dict(base_params)
                # 如果存在 sid，添加 token 参数（用于身份验证）
                if self.sid:
                    params['token'] = self.sid
                # 添加客户端信息（模拟 APP 请求）
                params.setdefault('version', '9.9.9')
                params.setdefault('platform', 'android')

                url = endpoint + '?' + urlencode(params)
                tried.append(url)
                try:
                    log.debug(f'Trying APP detail URL: {url}')
                    res = urlopen(Request(url, headers=self.get_headers(), method='GET'), timeout=15, context=ctx)
                    if res.status not in (200, 201):
                        continue
                    content = self.get_res_content(res)
                    # try parse JSON
                    try:
                        data = json.loads(content)
                    except Exception:
                        data = None

                    if not data:
                        # some endpoints may return directly the object or wrap under 'data' or 'items'
                        try:
                            # attempt to find JSON-like substring
                            j = json.loads(content.strip())
                            data = j
                        except Exception:
                            data = None

                    if data:
                        # common patterns: { code:0, data: { ... } } or { data: { book: ... } } or { items: [...] }
                        app_data = None
                        if isinstance(data, dict):
                            if data.get('code') == 0 and data.get('data'):
                                d = data.get('data')
                                # if 'book' key present inside data, use it
                                if isinstance(d, dict) and (d.get('book') or d.get('novel')):
                                    app_data = d.get('book') or d.get('novel') or d
                                else:
                                    app_data = d
                            elif data.get('data') and isinstance(data.get('data'), dict):
                                app_data = data.get('data')
                            elif data.get('items'):
                                # items list - pick first item that matches novelid
                                items = data.get('items')
                                if isinstance(items, list) and items:
                                    # find matching item by novelid/bookId if present
                                    found = None
                                    for it in items:
                                        try:
                                            if str(it.get('novelid') or it.get('bookId') or it.get('id')) == str(novelid):
                                                found = it
                                                break
                                        except Exception:
                                            continue
                                    app_data = found or items[0]
                            else:
                                # sometimes the top-level dict is already the book data
                                app_data = data
                        elif isinstance(data, list) and data:
                            # list of books
                            app_data = data[0]

                        if app_data:
                            try:
                                parsed = self.parse_app_book_data(app_data, novelid, log)
                                # 验证解析结果：若关键字段缺失（书名/作者），视为解析失败以触发回退
                                title_ok = bool(parsed.get('title'))
                                authors_ok = bool(parsed.get('authors'))
                                if not title_ok or not authors_ok:
                                    log.debug(f'APP detail parsed but missing title/authors for {novelid}, will fallback to web')
                                else:
                                    return parsed
                            except Exception as e:
                                log.debug(f'parse_app_book_data failed: {e}')
                except Exception as e:
                    log.debug(f'APP detail request to {url} failed: {e}')

        log.debug(f'Tried APP detail URLs: {tried}')
        return None

    def parse_app_book_data(self, app_data, novelid, log=None):
        """
        解析 APP 接口返回的 JSON 数据
        
        本方法具有强大的兼容性，能够处理不同版本的 API 返回格式：
        - 支持多种键名变体（驼峰、下划线、大小写等）
        - 支持在嵌套字段中查找（book/novel/data/items[0] 等）
        - 自动清洗 HTML 标签，提取纯文本
        - 当关键字段缺失时，尝试将原始 JSON 保存到桌面 debug 文件（便于排查问题）
        
        解析的字段包括：
        - 基本信息：title、authors、cover、description
        - 元数据：tags、publishedDate、status、word_count、chapters
        - 扩展信息：通过 fetch_and_merge_other_info 方法补充
        
        Args:
            app_data: APP 接口返回的 JSON 数据（字典或列表）
            novelid: 书籍 ID
            log: 日志记录器对象（可选）
            
        Returns:
            书籍信息字典，包含所有解析出的字段
        """
        book = {}
        book['id'] = novelid

        # 生成候选 key 变体
        def key_variants(k):
            vs = set()
            if not k:
                return vs
            vs.add(k)
            vs.add(k.lower())
            # 驼峰/下划线互转
            vs.add(''.join([p.capitalize() if i>0 else p for i,p in enumerate(k.split('_'))]))
            vs.add(k.replace('_', ''))
            vs.add(k.replace('_', '').lower())
            vs.add(k.replace(' ', ''))
            # 常见驼峰小写首字母
            if '_' in k:
                parts = k.split('_')
                camel = parts[0] + ''.join([p.capitalize() for p in parts[1:]])
                vs.add(camel)
            return vs

        # 从一个 dict 中递归查找首个非空值（只向下一层嵌套寻找）
        def _pick_from(obj, *keys):
            if not obj or not keys:
                return ''
            # 先尝试直接或变体键
            try:
                for k in keys:
                    for cand in key_variants(k):
                        if isinstance(obj, dict) and cand in obj and obj[cand]:
                            return obj[cand]
                # 再尝试不精确匹配（忽略大小写）
                if isinstance(obj, dict):
                    lowmap = {str(kk).lower(): kk for kk in obj.keys()}
                    for k in keys:
                        lk = k.lower()
                        if lk in lowmap:
                            v = obj.get(lowmap[lk])
                            if v:
                                return v
            except Exception:
                pass
            # 如果未找到，尝试在常见嵌套字段中寻找（book/novel/data/items first element）
            for nest_key in ('book', 'novel', 'data', 'result'):
                try:
                    nested = obj.get(nest_key)
                except Exception:
                    nested = None
                if isinstance(nested, dict):
                    v = _pick_from(nested, *keys)
                    if v:
                        return v
                elif isinstance(nested, list) and nested:
                    v = _pick_from(nested[0], *keys)
                    if v:
                        return v
            # 最后，如果 obj 本身是列表，尝试第一项
            if isinstance(obj, list) and obj:
                try:
                    return _pick_from(obj[0], *keys)
                except Exception:
                    pass
            return ''

        # title: 尝试大量候选键
        title_candidates = ('bookname', 'bookName', 'book_name', 'novelname', 'novelName', 'name', 'title', 'novelname_format', 'novelname_format_html')
        title = _pick_from(app_data, *title_candidates) or ''
        if isinstance(title, (list, dict)):
            title = str(title)
        title = html_to_text(str(title))
        book['title'] = title.strip()

        # authors: 尝试更多键名和嵌套
        author_candidates = ('authorname', 'author', 'authorName', 'authors', 'writer', 'writerName', 'author_name', 'authorNames')
        author_field = _pick_from(app_data, *author_candidates) or ''
        if isinstance(author_field, (list, dict)):
            # 如果是 list，尝试 join 或取第一个
            if isinstance(author_field, list) and author_field:
                author_str = ','.join([html_to_text(str(x)) for x in author_field])
            else:
                author_str = html_to_text(json.dumps(author_field, ensure_ascii=False))
        else:
            author_str = html_to_text(str(author_field))

        # 分割作者字符串（兼容中文分割符）
        authors = [a.strip() for a in re.split(r'[,&/;，、\s]+', author_str) if a.strip()]
        book['authors'] = authors
        book['url'] = JINJIANG_BOOK_DETAIL_WEB_URL % novelid

        # 封面：优先使用 novelCover 和 originalCover（真实封面），避免使用 localImg（默认封面）
        # 优先级：novelCover > originalCover > 其他字段 > localImg（最后备选）
        cover = _pick_from(app_data, 'novelCover') or ''
        if not cover:
            cover = _pick_from(app_data, 'originalCover') or ''
        if not cover:
            cover_candidates = ('coverimg', 'cover', 'cover_img', 'bookimg', 'coverUrl', 'cover_url')
            cover = _pick_from(app_data, *cover_candidates) or ''
        # 最后才尝试 localImg（通常是默认封面）
        if not cover:
            cover = _pick_from(app_data, 'localImg') or ''
        cover = str(cover).strip()
        if cover:
            if cover.startswith('//'):
                cover = 'https:' + cover
            elif cover.startswith('/'):
                cover = JINJIANG_BASE_URL.rstrip('/') + cover
            elif not cover.startswith('http://') and not cover.startswith('https://') and not cover.startswith('data:'):
                # 如果是相对路径但没有前导斜杠，尝试构建完整URL
                if cover:
                    cover = JINJIANG_BASE_URL.rstrip('/') + '/' + cover.lstrip('/')
        # 验证URL有效性：只保留有效的HTTP/HTTPS URL或data URI
        book['cover'] = cover if cover and (cover.startswith('http://') or cover.startswith('https://') or cover.startswith('data:')) else ''

        # 简介
        intro_candidates = ('intro', 'novelintroshort', 'novelintro', 'description', 'desc')
        intro_html = _pick_from(app_data, *intro_candidates) or ''
        book['description_html'] = str(intro_html).strip()
        book['description'] = html_to_text(str(intro_html))

        # 标签
        category = _pick_from(app_data, 'category') or ''
        tags_raw = _pick_from(app_data, 'tags') or ''
        tags = []
        try:
            if isinstance(tags_raw, str):
                tags = [t.strip() for t in tags_raw.split(',') if t.strip()]
            elif isinstance(tags_raw, list):
                tags = [str(t).strip() for t in tags_raw if str(t).strip()]
        except Exception:
            tags = []
        if category:
            tags.insert(0, category)
        book['tags'] = tags

        # createtime -> publishedDate
        createtime = _pick_from(app_data, 'createtime', 'createTime', 'publish_time') or ''
        createtime = str(createtime).strip()
        published = ''
        try:
            if createtime.isdigit():
                ts = int(createtime)
                if ts > 1e12:
                    ts = ts / 1000
                published = datetime.fromtimestamp(ts).strftime('%Y-%m-%d')
            else:
                m = re.match(r"^(\d{4})[-/年]?(\d{1,2})?[-/月]?(\d{1,2})?", createtime)
                if m:
                    y = m.group(1)
                    mo = m.group(2) or '01'
                    d = m.group(3) or '01'
                    published = f"{y}-{int(mo):02d}-{int(d):02d}"
                else:
                    published = createtime
        except Exception:
            published = createtime
        book['publishedDate'] = published

        book['status'] = _pick_from(app_data, 'status') or ''
        book['word_count'] = _pick_from(app_data, 'wordcount', 'wordCount') or 0

        try:
            book['chapters'] = int(_pick_from(app_data, 'chapterCount', 'chaptercount', 'chapters') or 0)
        except Exception:
            book['chapters'] = None
        try:
            book['vip_start'] = int(_pick_from(app_data, 'vip_start', 'vipStart', 'vipstart') or 0)
        except Exception:
            book['vip_start'] = None

        book['source'] = {
            "id": PROVIDER_ID,
            "description": PROVIDER_NAME,
            "link": JINJIANG_BASE_URL
        }

        # 尝试拉取并合并来自 getnovelOtherInfo 的扩展信息（留言、类型、标签等）
        try:
            try:
                # 通过单独方法请求并合并额外信息
                self.fetch_and_merge_other_info(novelid, book, log, base_data=app_data)
            except Exception:
                # 不应阻塞主流程，日志调试即可
                if log:
                    log.debug('fetch_and_merge_other_info failed')
        except Exception:
            pass

        # 若关键字段缺失，仅记录到日志（不再写入桌面文件）
        if (not book['title'] or not book['authors']) and log:
            try:
                # 仅记录JSON片段到日志，不写入文件
                snippet = json.dumps(app_data, ensure_ascii=False)[:2000]
                log.warning(f'APP解析未提取到 title/author，原始 JSON 片段: {snippet}')
            except Exception:
                pass

        return book

    def get_res_content(self, res):
        """
        处理 HTTP 响应内容
        
        处理步骤：
        1. 检查响应是否使用 gzip 压缩，如果是则解压
        2. 检测字符编码（从响应头获取，默认使用 UTF-8）
        3. 解码响应内容为字符串
        
        Args:
            res: urllib 的 HTTPResponse 对象
            
        Returns:
            解码后的响应内容字符串
        """
        # 检查响应是否使用 gzip 压缩
        encoding = res.info().get('Content-Encoding')
        if encoding == 'gzip':
            res_content = gzip.decompress(res.read())
        else:
            res_content = res.read()
        
        # 检测字符编码（从响应头获取，默认使用 UTF-8）
        charset = res.headers.get_content_charset() or 'utf-8'
        
        # 解码响应内容，忽略无法解码的字符（避免解码错误导致程序崩溃）
        return res_content.decode(charset, errors='ignore')

    def get_headers(self):
        """
        生成增强的 HTTP 请求头（模拟 APP/移动端）
        
        请求头特点：
        - 随机 User-Agent（50% 概率使用 Calibre 的随机 UA，50% 使用晋江 APP UA）
        - 支持 gzip 压缩
        - 设置合适的 Accept 头
        - 如果配置了登录 Cookie，自动添加到请求头中
        
        Returns:
            包含所有必要请求头的字典
        """
        headers = {
            # User-Agent：50% 概率使用 Calibre 随机 UA，50% 使用晋江 APP UA（模拟移动端）
            'User-Agent': random_user_agent() if random.random() > 0.5 else 'JJWXC-Android/9.9.9 (Android; 10; SM-G973F)',
            'Accept-Encoding': 'gzip, deflate, br',  # 支持压缩传输
            'Accept': 'application/json, text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
            # Referer：根据是否优先使用 APP API 设置不同的来源页
            'Referer': JINJIANG_M_BASE_URL if self.jinjiang_prefer_app_api else JINJIANG_BASE_URL,
            'Connection': 'keep-alive',  # 保持连接
            'X-Requested-With': 'XMLHttpRequest'  # 标识为 AJAX 请求
        }
        # 如果配置了登录 Cookie，添加到请求头中
        if self.jinjiang_login_cookie:
            headers['Cookie'] = self.jinjiang_login_cookie
        return headers

    def random_sleep(self, log):
        """
        执行随机延迟（用于避免触发反爬虫机制）
        
        延迟时间根据使用的接口类型调整：
        - APP 接口：0.2-0.8 秒（反爬虫机制较弱，可以较短延迟）
        - 网页接口：0.5-1.8 秒（反爬虫机制较强，需要较长延迟）
        
        Args:
            log: 日志记录器对象（用于记录延迟时间）
        """
        if self.jinjiang_prefer_app_api:
            random_sec = random.uniform(0.2, 0.8)  # APP 接口延迟较短（反爬虫机制较弱）
        else:
            random_sec = random.uniform(0.5, 1.8)  # 网页接口延迟较长（反爬虫机制较强）
        log.info(f'Random sleep: {random_sec:.2f}s')
        time.sleep(random_sec)


class JinjiangBookHtmlParser:
    """
    网页详情页 HTML 解析器（兜底方案）
    
    当 APP 接口失败时，使用此解析器从网页 HTML 中提取书籍信息。
    使用 lxml 库解析 HTML，通过 XPath 选择器提取各种字段。
    """
    def __init__(self):
        self.novelid_pattern = re.compile(r"novelid=(\d+)")

    def parse_book(self, url, book_content, log):
        book = {}
        html = etree.HTML(book_content)
        if not html:
            return None

        # 书籍ID
        id_match = self.novelid_pattern.search(url)
        book['id'] = id_match.group(1) if id_match else None
        if not book['id']:
            return None

        # 书名
        title_elements = html.xpath("//h1[contains(@class, 'bookname')] | //div[contains(@class, 'novelname')]/h1")
        book['title'] = self.get_text(title_elements).strip()
        if not book['title']:
            return None

        # 作者
        author_elements = html.xpath("//a[contains(@class, 'author')] | //div[contains(@class, 'authorinfo')]//a[contains(@href, 'authorid')]")
        book['authors'] = [self.get_text(author_elements).strip()] if author_elements else []

        # 封面
        img_elements = html.xpath("//div[contains(@class, 'bookimg')]//img | //div[contains(@class, 'novelimg')]//img")
        book['cover'] = ''
        if img_elements:
            cover_src = img_elements[0].attrib.get('src', '').strip()
            if cover_src:
                if cover_src.startswith('//'):
                    cover_src = 'https:' + cover_src
                elif cover_src.startswith('/'):
                    cover_src = JINJIANG_BASE_URL.rstrip('/') + cover_src
                elif not cover_src.startswith('http://') and not cover_src.startswith('https://'):
                    # 如果是相对路径，尝试构建完整URL
                    if not cover_src.startswith('data:'):
                        cover_src = JINJIANG_BASE_URL.rstrip('/') + '/' + cover_src.lstrip('/')
                # 验证URL有效性
                if cover_src and (cover_src.startswith('http://') or cover_src.startswith('https://') or cover_src.startswith('data:')):
                    book['cover'] = cover_src

        # 简介
        summary_elements = html.xpath("//div[contains(@class, 'intro')] | //div[@id='novelintro']")
        book['description'] = self.get_text(summary_elements, join_lines=True)

        # 标签
        tag_elements = html.xpath("//div[contains(@class, 'tag')]//a | //div[contains(@class, 'classify')]//a")
        book['tags'] = [self.get_text([elem]).strip() for elem in tag_elements if self.get_text([elem]).strip()]

        # 出版时间
        pubdate_elements = html.xpath("//div[contains(@class, 'infobox')]//span[contains(text(), '连载时间') or contains(text(), '发表时间')]")
        book['publishedDate'] = self.get_tail(pubdate_elements)

        # 来源信息
        book['url'] = url
        book['source'] = {
            "id": PROVIDER_ID,
            "description": PROVIDER_NAME,
            "link": JINJIANG_BASE_URL
        }

        return book

    def get_text(self, elements, default_str='', join_lines=False):
        texts = []
        for elem in elements:
            if isinstance(elem, etree._Element):
                text = ' '.join(elem.xpath('.//text()')).strip()
                if text:
                    texts.append(text)
        if join_lines:
            return '\n'.join(texts) if texts else default_str
        return texts[0] if texts else default_str

    def get_tail(self, elements, default_str=''):
        for elem in elements:
            if isinstance(elem, etree._Element) and elem.tail:
                tail_text = elem.tail.strip()
                if tail_text:
                    return tail_text
            next_elem = elem.getnext()
            if next_elem:
                next_text = self.get_text([next_elem]).strip()
                if next_text:
                    return next_text
        return default_str


class NewJinjiangBooks(Source):
    """
    晋江文学城元数据插件主类
    
    这是 Calibre 元数据源插件，继承自 Source 基类。
    提供从晋江文学城获取书籍元数据的功能，包括识别书籍和下载封面。
    
    主要功能：
    - identify: 根据书名/作者搜索并识别书籍
    - cover: 下载书籍封面图片
    
    支持的平台：Windows、macOS、Linux
    最低 Calibre 版本要求：5.0.0
    """
    name = PROVIDER_NAME
    description = 'Enhanced Jinjiang Books Plugin (supports APP API, multi-type search) - 支持多类型搜索、APP接口'
    supported_platforms = ['windows', 'osx', 'linux']
    author = PROVIDER_AUTHOR
    version = PROVIDER_VERSION
    minimum_calibre_version = (5, 0, 0)
    capabilities = frozenset(['identify', 'cover'])  # 插件能力：识别和封面下载
    
    # touched_fields: 声明插件会修改的 Calibre 元数据字段
    # 注意：只声明 Calibre 标准支持的字段，非标准字段（如 status/word_count）不应放入此列表
    touched_fields = frozenset([
        'title', 'authors', 'tags', 'pubdate', 'comments', 'identifier:isbn',
        'rating', 'identifier:' + PROVIDER_ID, 'publisher'
    ])
    book_searcher = None  # 书籍搜索器实例（延迟初始化）

    options = (
        Option(
            'jinjiang_concurrency_size', 'number', JINJIANG_CONCURRENCY_SIZE,
            _('Concurrency size:'),
            _('Maximum number of concurrent requests (≤5 recommended)')
        ),
        Option(
            'jinjiang_delay_enable', 'bool', True,
            _('Enable random delay:'),
            _('Avoid anti-crawling (required for web search)')
        ),
        Option(
            'jinjiang_login_cookie', 'string', None,
            _('Login cookie:'),
            _('Cookie after logging into Jinjiang (required for APP API and VIP content)')
        ),
        Option(
            'jinjiang_search_with_author', 'bool', False,
            _('Search with author:'),
            _('Add author name to search keywords (improve accuracy)')
        ),
        Option(
            'jinjiang_prefer_app_api', 'bool', True,
            _('Prefer APP API:'),
            _('Use APP API first (more stable, less anti-crawling)')
        ),
    )

    def __init__(self, *args, **kwargs):
        """
        初始化插件
        
        从 Calibre 配置中读取用户设置的选项，并初始化书籍搜索器。
        """
        Source.__init__(self, *args, **kwargs)
        
        # 从 Calibre 配置中读取用户选项
        concurrency_size = int(self.prefs.get('jinjiang_concurrency_size', JINJIANG_CONCURRENCY_SIZE))
        jinjiang_delay_enable = bool(self.prefs.get('jinjiang_delay_enable', True))
        jinjiang_login_cookie = self.prefs.get('jinjiang_login_cookie', None)
        jinjiang_search_with_author = bool(self.prefs.get('jinjiang_search_with_author', False))
        jinjiang_prefer_app_api = bool(self.prefs.get('jinjiang_prefer_app_api', True))
        
        # 初始化书籍搜索器，传入所有配置选项
        self.book_searcher = JinjiangBookSearcher(
            concurrency_size=concurrency_size,
            jinjiang_delay_enable=jinjiang_delay_enable,
            jinjiang_login_cookie=jinjiang_login_cookie,
            jinjiang_search_with_author=jinjiang_search_with_author,
            jinjiang_prefer_app_api=jinjiang_prefer_app_api
        )

    def get_book_url(self, identifiers):
        """
        从标识符中获取书籍 URL
        
        Args:
            identifiers: Calibre 标识符字典
            
        Returns:
            如果找到晋江 ID，返回 (provider_id, book_id, url) 元组，否则返回 None
        """
        jinjiang_id = identifiers.get(PROVIDER_ID, None)
        if jinjiang_id:
            return PROVIDER_ID, jinjiang_id, JINJIANG_BOOK_DETAIL_WEB_URL % jinjiang_id
        return None

    def identify(self, log, result_queue, abort, title=None, authors=None, identifiers={}, timeout=30):
        """
        Calibre 识别接口：根据书名/作者搜索并识别书籍
        
        识别策略（按优先级）：
        1. 如果提供了书籍 ID，直接通过 ID 加载
        2. 如果提供了书名，使用书名搜索（可能包含作者名）
        3. 如果书名搜索无结果，尝试仅按作者搜索
        4. 如果仍无结果，生成书名变体并重试
        
        所有找到的书籍都会被转换为 Calibre Metadata 对象并放入结果队列。
        
        Args:
            log: 日志记录器对象
            result_queue: 结果队列（用于返回识别到的书籍元数据）
            abort: 中止信号（用于支持用户取消操作）
            title: 书籍标题（可选）
            authors: 作者列表（可选）
            identifiers: 书籍标识符字典（可选，如果包含晋江 ID 则直接使用）
            timeout: 超时时间（秒）
        """
        log.info(f'Jinjiang identify: title={title}, authors={authors}, identifiers={identifiers}')

        # 策略1：优先通过 ID 查询（最快速、最准确）
        book_url_info = self.get_book_url(identifiers)
        if book_url_info:
            provider_id, book_id, url = book_url_info
            log.info(f'Query by ID: {book_id}')
            book = self.book_searcher.load_book(url, log)
            books = [book] if book else []
        else:
            # 策略2：通过书名/作者搜索
            if not title and not authors:
                log.warning('No title or authors provided')
                return

            # 步骤1：清洗并规范化查询词（提高搜索匹配率）
            cleaned_title = normalize_query(title) if title else ''
            cleaned_authors = [normalize_query(a) for a in authors] if authors else []

            # 步骤2：使用书名（可能包含作者）进行搜索
            t0 = time.time()
            books = self.book_searcher.search_books(query=cleaned_title or ' '.join(cleaned_authors), authors=authors, log=log)
            elapsed = time.time() - t0
            log.info(f'Found {len(books)} results from Jinjiang (time: {elapsed:.3f}s)')

            # 步骤3：如果书名搜索无结果，尝试仅按作者搜索
            # 使用晋江的 JSON 规则：#作者# 表示按作者搜索
            if not books and cleaned_authors:
                t1 = time.time()
                author_query = ' '.join(cleaned_authors)
                wrapped = f"#{author_query}#"
                log.info(f'No results for title-search, retrying with author-only (wrapped): {wrapped}')
                books = self.book_searcher.search_books(query=wrapped, authors=authors, log=log)
                elapsed2 = time.time() - t1
                log.info(f'Author-only search found {len(books)} results (time: {elapsed2:.3f}s)')

            # 步骤4：如果仍无结果，生成书名变体并重试
            # 变体包括：去除标注词（如"完结"、"番外"）、提取关键词等
            if not books and cleaned_title:
                variations = generate_title_variations(cleaned_title)
                for var in variations:
                    t2 = time.time()
                    log.info(f'Trying title variation: {var}')
                    books = self.book_searcher.search_books(query=var, authors=authors, log=log)
                    elapsed3 = time.time() - t2
                    log.info(f'Variation {var} found {len(books)} results (time: {elapsed3:.3f}s)')
                    if books:
                        break

        for book in books:
            if abort.is_set():
                break
            if book:
                metadata = self.to_metadata(book, log)
                if isinstance(metadata, Metadata):
                    # cache cover url if present
                    try:
                        dbid = metadata.identifiers.get(PROVIDER_ID)
                        if metadata.cover and dbid:
                            try:
                                # store cover URL mapping so Calibre can download later
                                self.cache_identifier_to_cover_url(dbid, metadata.cover)
                            except Exception:
                                log.debug('cache_identifier_to_cover_url failed')
                    except Exception:
                        pass

                    # allow Calibre to clean/normalize the metadata before returning
                    try:
                        self.clean_downloaded_metadata(metadata)
                    except Exception:
                        log.debug('clean_downloaded_metadata failed')

                    result_queue.put(metadata)

    # browse 能力已移除以简化插件为仅按书名/作者提取元数据

    def to_metadata(self, book, log):
        mi = Metadata(book['title'], book['authors'])
        mi.identifiers = {PROVIDER_ID: book['id']}
        mi.url = book['url']
        # 封面：保留封面 URL 到 mi.cover（Calibre 接受 URL 字符串）并缓存 URL 供 download_cover 使用
        mi.cover = book.get('cover', None)
        if mi.cover:
            try:
                self.cache_identifier_to_cover_url(book['id'], mi.cover)
            except Exception:
                log.debug('Cache cover URL failed')
        
        # 简介（已转换为纯文本）
        mi.comments = book.get('description', '') or book.get('description_html', '')
        
        # 标签
        if book.get('tags'):
            mi.tags = book['tags']
        
        # 出版日期
        pubdate_str = book.get('publishedDate')
        if pubdate_str:
            try:
                pubdate_str = pubdate_str.replace('年', '-').replace('月', '-').replace('日', '')
                if re.match(r'^\d{4}-\d{2}-\d{2}$', pubdate_str):
                    mi.pubdate = datetime.strptime(pubdate_str, '%Y-%m-%d')
                elif re.match(r'^\d{4}-\d{2}$', pubdate_str):
                    mi.pubdate = datetime.strptime(pubdate_str, '%Y-%m')
                elif re.match(r'^\d{4}$', pubdate_str):
                    mi.pubdate = datetime.strptime(pubdate_str, '%Y')
            except Exception as e:
                log.warning(f'Parse pubdate failed: {e}')
        
        # 新增字段（来自APP接口）
        mi.set('status', book.get('status', ''))  # 连载状态
        mi.set('word_count', book.get('word_count', 0))  # 字数
        # 保留原始 HTML 简介以便需要时使用
        if book.get('description_html'):
            mi.set('description_html', book.get('description_html'))
        # 章节与 VIP 信息
        if book.get('chapters') is not None:
            mi.set('chapters', book.get('chapters'))
        if book.get('vip_start') is not None:
            mi.set('vip_start', book.get('vip_start'))
        # 语言
        try:
            mi.language = 'zh_CN'
        except Exception:
            try:
                mi.set('language', 'zh_CN')
            except Exception:
                log.debug('Failed to set language')
        # 评分（若存在）
        if book.get('rating') is not None:
            try:
                mi.rating = float(book.get('rating'))
            except Exception:
                pass
        # ISBN/Series（占位，如果返回再写入）
        if book.get('isbn'):
            try:
                mi.isbn = book.get('isbn')
            except Exception:
                pass
        if book.get('series'):
            try:
                mi.series = book.get('series')
            except Exception:
                pass
        
        mi.source = book['source']['description']
        # 如果元数据来自晋江 APP/网页，则统一设置出版社为“晋江文学城”
        try:
            mi.publisher = '晋江文学城'
        except Exception:
            try:
                mi.set('publisher', '晋江文学城')
            except Exception:
                log.debug('Failed to set publisher field')
        return mi

    def download_cover(self, log, result_queue, abort, title=None, authors=None, identifiers={}, timeout=30, get_best_cover=False):
        cached_url = self.get_cached_cover_url(identifiers)
        if not cached_url:
            log.info('No cached cover, run identify first')
            rq = Queue()
            self.identify(log, rq, abort, title=title, authors=authors, identifiers=identifiers)
            if abort.is_set():
                return
            
            results = []
            while True:
                try:
                    results.append(rq.get_nowait())
                except Empty:
                    break
            
            for mi in results:
                cached_url = self.get_cached_cover_url(mi.identifiers)
                if cached_url:
                    break
        
        if not cached_url:
            log.info('No cover found')
            return
        
        log.info(f'Download cover: {cached_url}')
        try:
            br = self.browser
            if self.book_searcher.jinjiang_login_cookie:
                br = br.clone_browser()
                br.set_current_header('Cookie', self.book_searcher.jinjiang_login_cookie)
            br.set_current_header('Referer', JINJIANG_BASE_URL)
            br.set_current_header('User-Agent', random_user_agent())
            
            cdata = br.open_novisit(cached_url, timeout=timeout).read()
            if cdata:
                result_queue.put((self, cdata))
        except Exception as e:
            log.error(f'Download cover failed: {e}')

    def get_cached_cover_url(self, identifiers):
        jinjiang_id = identifiers.get(PROVIDER_ID)
        if not jinjiang_id:
            return None
        return self.cached_identifier_to_cover_url(jinjiang_id)


# 测试代码
if __name__ == "__main__":
    try:
        from calibre.ebooks.metadata.sources.test import test_identify_plugin, title_test, authors_test
    except Exception:
        # Calibre test harness not available in this environment; skip local tests
        print('Calibre test harness not available, skipping tests')
    else:
        test_identify_plugin(
            NewJinjiangBooks.name,
            [
                (
                    {
                        'title': '#酱子贝#',  # 作者搜索（JSON规则）
                        'authors': [],
                        'identifiers': {}
                    },
                    [
                        title_test('我行让我上[电竞]', exact=False),
                        authors_test(['酱子贝'])
                    ]
                ),
                (
                    {
                        'title': '我喜欢你的信息素',
                        'authors': ['引路星'],
                        'identifiers': {}
                    },
                    [
                        title_test('我喜欢你的信息素', exact=True),
                        authors_test(['引路星'])
                    ]
                )
            ]
        )