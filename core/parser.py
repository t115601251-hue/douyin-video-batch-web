"""Strict-ID Douyin parser with real source-resolution choices."""
import hashlib
import html
import json
import re
from dataclasses import dataclass, field, asdict
from urllib.parse import unquote, urlsplit
import httpx
from config import DEFAULT_HEADERS, DEFAULT_TIMEOUT
from .extractor import resolve_aweme_id

FEED_API_HOSTS = ['https://aweme.snssdk.com', 'https://aweme-hl.snssdk.com', 'https://api3-normal-c-hl.amemv.com']

def number(value):
    try:
        return max(0, int(value or 0))
    except (ValueError, TypeError):
        return 0

@dataclass
class VideoVariant:
    id: str
    width: int
    height: int
    bitrate: int
    codec: str
    size: int
    urls: list

    @property
    def label(self):
        size = f' · {self.size / 1048576:.1f} MB' if self.size else ''
        dimensions = f'{self.width} × {self.height}' if self.width and self.height else '分辨率未知'
        rate = f' · {self.bitrate / 1000:.0f} kbps' if self.bitrate else ''
        return f'{dimensions} · {self.codec}{rate}{size}'

    def public(self):
        return {k: v for k, v in asdict(self).items() if k != 'urls'} | {'label': self.label}

@dataclass
class VideoInfo:
    aweme_id: str
    title: str = ''
    author: str = ''
    duration_sec: int = 0
    cover_url: str = ''
    variants: list = field(default_factory=list)
    source_urls: list = field(default_factory=list)
    error_msg: str = ''
    source: str = ''

    @property
    def is_video(self):
        return bool(self.variants) and not self.error_msg

    def public(self):
        return dict(aweme_id=self.aweme_id, title=self.title, author=self.author,
                    duration_sec=self.duration_sec, cover_url=self.cover_url,
                    variants=[v.public() for v in self.variants], source_urls=self.source_urls,
                    canonical_url=f'https://www.douyin.com/video/{self.aweme_id}' if self.aweme_id else '',
                    matched=self.is_video, error=self.error_msg, source=self.source)

    def filename(self, variant):
        text = re.sub(r'[\\/:*?"<>|\x00-\x1f]', '_', f'{self.author[:18]}_{self.title[:36]}').strip(' ._') or '抖音视频'
        return f'{text}_{self.aweme_id}_{variant.width}x{variant.height}_{variant.id}.mp4'

def find_item(data, aweme_id):
    stack = [data]
    while stack:
        obj = stack.pop()
        if isinstance(obj, dict):
            identity = obj.get('aweme_id', obj.get('awemeId'))
            if str(identity) == aweme_id and ('video' in obj or obj.get('images')):
                return obj
            stack.extend(v for v in obj.values() if isinstance(v, (dict, list)))
        elif isinstance(obj, list):
            stack.extend(obj)
    return None

def page_payloads(text):
    for match in re.finditer(r'(?:window\.)?_ROUTER_DATA\s*=\s*', text):
        try:
            yield json.JSONDecoder().raw_decode(text[match.end():])[0]
        except ValueError:
            pass
    for match in re.finditer(r'<script\b[^>]*\bid=[\"\'](?:RENDER_DATA|__NEXT_DATA__)[\"\'][^>]*>(.*?)</script>', text, re.S | re.I):
        try:
            yield json.loads(unquote(html.unescape(match[1])))
        except ValueError:
            pass

def item_to_info(item, aweme_id, raw_url, source):
    if str(item.get('aweme_id', item.get('awemeId'))) != aweme_id:
        raise ValueError('作品 ID 不匹配，拒绝使用其他视频')
    author = item.get('author') or {}
    v = item.get('video') or {}
    info = VideoInfo(aweme_id, str(item.get('desc') or '抖音视频'), str(author.get('nickname') or '未知作者'),
                     number(v.get('duration')) // 1000, source_urls=[raw_url], source=source)
    if item.get('images'):
        info.error_msg = '该链接为图文作品，不是视频'
        return info
    covers = (v.get('cover') or {}).get('url_list') or []
    info.cover_url = covers[0] if covers else ''
    variants = {}

    def add(addr, bitrate=0, codec='H.264'):
        if not isinstance(addr, dict):
            return
        urls = addr.get('url_list') or addr.get('urlList') or []
        if isinstance(urls, str):
            urls = [urls]
        urls = list(dict.fromkeys(u.replace('/playwm/', '/play/') for u in urls if isinstance(u, str) and urlsplit(u).scheme in ('https', 'http')))
        if not urls:
            return
        width = number(addr.get('width')) or number(v.get('width'))
        height = number(addr.get('height')) or number(v.get('height'))
        bitrate = number(bitrate)
        key = hashlib.sha256(f'{width}:{height}:{bitrate}:{codec}:{addr.get("uri", "")}'.encode()).hexdigest()[:10]
        variants.setdefault(key, VideoVariant(key, width, height, bitrate, codec, number(addr.get('data_size')), urls))

    for rate in v.get('bit_rate') or v.get('bitRateList') or []:
        if isinstance(rate, dict) and rate.get('format', 'mp4') == 'mp4':
            add(rate.get('play_addr') or rate.get('playAddr'), rate.get('bit_rate') or rate.get('bitRate'), 'H.265' if rate.get('is_h265') or rate.get('isH265') else 'H.264')
    add(v.get('play_addr_h264'), codec='H.264')
    add(v.get('play_addr') or v.get('playAddr'), codec='H.265' if v.get('is_h265') else 'H.264')
    add(v.get('play_addr_265'), codec='H.265')
    info.variants = sorted(variants.values(), key=lambda x: (x.width * x.height, x.bitrate, x.codec == 'H.264'), reverse=True)
    if not info.variants:
        info.error_msg = '目标作品匹配，但没有可用的 MP4 视频源'
    return info

class DouyinVideoParser:
    def __init__(self, timeout=DEFAULT_TIMEOUT):
        self.timeout = timeout

    async def parse(self, url, client=None):
        if client is None:
            async with httpx.AsyncClient(headers=DEFAULT_HEADERS, timeout=self.timeout) as c:
                return await self.parse(url, c)
        aweme_id = ''
        errors = []
        try:
            aweme_id = await resolve_aweme_id(url, client)
            if not aweme_id:
                raise ValueError('无法从链接确认作品 ID；请复制 /video/ 链接或带 modal_id 的网页地址')
            for host in FEED_API_HOSTS:
                try:
                    result = await self._parse_by_feed_api(host, aweme_id, url, client)
                    if result and (result.is_video or '图文' in result.error_msg):
                        return result
                    errors.append('接口未返回目标作品或可用视频源')
                except (httpx.HTTPError, ValueError, TypeError, KeyError) as e:
                    errors.append(type(e).__name__)
            for page in (f'https://www.iesdouyin.com/share/video/{aweme_id}/', f'https://www.douyin.com/video/{aweme_id}'):
                try:
                    response = await client.get(page, follow_redirects=True)
                    response.raise_for_status()
                    for data in page_payloads(response.text):
                        item = find_item(data, aweme_id)
                        if item:
                            result = item_to_info(item, aweme_id, url, '网页数据')
                            if result.is_video or '图文' in result.error_msg:
                                return result
                    errors.append('网页无目标作品数据，可能需要浏览器验证')
                except (httpx.HTTPError, ValueError, TypeError, KeyError) as e:
                    errors.append(type(e).__name__)
            raise ValueError('未取得此作品的视频源；可能需要登录/验证、链接已失效或暂时限流。' + ' / '.join(dict.fromkeys(errors)))
        except Exception as e:
            return VideoInfo(aweme_id or '', source_urls=[url], error_msg=str(e))

    async def _parse_by_feed_api(self, host, aweme_id, raw_url, client):
        response = await client.get(f'{host}/aweme/v1/feed/', params={'aweme_id': aweme_id})
        response.raise_for_status()
        item = find_item(response.json(), aweme_id)
        return item_to_info(item, aweme_id, raw_url, '视频详情接口') if item else None
