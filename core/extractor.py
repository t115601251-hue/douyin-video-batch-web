"""Extract links without guessing an ID from unrelated recommendations."""
import re
from urllib.parse import urlsplit, parse_qs, urljoin, urlunsplit
import httpx
from config import DEFAULT_HEADERS, DEFAULT_TIMEOUT, MAX_LINKS

# Find the start of every link first.  This deliberately does not require a
# newline or a space between links: two copied links can be pasted together.
LINK_START_RE = re.compile(r'(?=(?:https?://)?(?:[\w-]+\.)*(?:douyin\.com|iesdouyin\.com)/)', re.I)
LINK_RE = re.compile(r'(?:https?://)?(?:[\w-]+\.)*(?:douyin\.com|iesdouyin\.com)/[^\s<>\"\'\]\)（），。！？；]+', re.I)
ID_RE = re.compile(r'/(?:share/)?(?:video|note)/(\d{10,25})(?:/|$)')

def is_douyin_url(url):
    p = urlsplit(url)
    return (p.scheme in ('https', 'http') and not p.username and not p.password
            and p.port in (None, 80, 443) and any((p.hostname or '') == h or (p.hostname or '').endswith('.' + h)
            for h in ('douyin.com', 'iesdouyin.com')))

def direct_id(url):
    if not is_douyin_url(url):
        return None
    p = urlsplit(url)
    m = ID_RE.search(p.path)
    if m:
        return m[1]
    query = parse_qs(p.query)
    for key in ('modal_id', 'aweme_id', 'item_id', 'item_ids'):
        values = query.get(key, [])
        if values and re.fullmatch(r'\d{10,25}', values[0]):
            return values[0]

def extract_douyin_links(raw_text):
    if not isinstance(raw_text, str):
        raise ValueError('链接内容必须是文本')
    result, seen = [], set()
    starts = [match.start() for match in LINK_START_RE.finditer(raw_text)]
    for index, start in enumerate(starts):
        # Stop at the next link start so a pasted ".../abchttps://..." is
        # recognised as two links rather than one invalid URL.
        candidate = raw_text[start:starts[index + 1] if index + 1 < len(starts) else len(raw_text)]
        match = LINK_RE.match(candidate)
        if not match:
            continue
        url = match[0].rstrip('.,;!?}')
        if not re.match(r'https?://', url, re.I):
            url = 'https://' + url
        if not is_douyin_url(url):
            continue
        p = urlsplit(url)
        url = urlunsplit(('https', p.netloc.lower(), p.path, p.query, ''))
        if url not in seen:
            seen.add(url)
            result.append(url)
    if len(result) > MAX_LINKS:
        raise ValueError(f'单批最多 {MAX_LINKS} 条不同链接，请分批提交')
    return result

async def resolve_aweme_id(url, client=None):
    if not is_douyin_url(url):
        raise ValueError('仅支持抖音网页或分享链接')
    found = direct_id(url)
    if found:
        return found
    if client is None:
        async with httpx.AsyncClient(headers=DEFAULT_HEADERS, timeout=DEFAULT_TIMEOUT) as c:
            return await resolve_aweme_id(url, c)
    for _ in range(6):
        if not is_douyin_url(url):
            raise ValueError('分享链接跳转到了非抖音域名，已停止')
        found = direct_id(url)
        if found:
            return found
        response = await client.get(url, follow_redirects=False)
        if response.is_redirect:
            url = urljoin(url, response.headers.get('location', ''))
            continue
        response.raise_for_status()
        for canonical in re.findall(r'<link\b[^>]*rel=[\"\']canonical[\"\'][^>]*>', response.text, re.I):
            match = re.search(r'href=[\"\']([^\"\']+)', canonical, re.I)
            if match:
                found = direct_id(urljoin(url, match[1]))
                if found:
                    return found
        return None
    raise ValueError('分享链接跳转次数过多')
