"""Bounded downloads, verified MP4 containers, atomic files and provenance."""
import asyncio
import os
import shutil
import struct
import time
import uuid
from pathlib import Path
from urllib.parse import urlsplit, urljoin
import httpx
from config import DEFAULT_HEADERS, DEFAULT_TIMEOUT, prepare_directory

MEDIA_DOMAINS = ('douyin.com', 'iesdouyin.com', 'douyinvod.com', 'bytecdn.cn', 'bytecdn.com',
                 'bytedance.com', 'pstatp.com', 'snssdk.com', 'amemv.com', 'ixigua.com',
                 'toutiaovod.com', 'ibytedtos.com', '365yg.com', 'byteicdn.com')

def allowed_media_url(url):
    p = urlsplit(url)
    return p.scheme in ('http', 'https') and not p.username and not p.password and p.port in (None, 80, 443) and any(
        (p.hostname or '') == d or (p.hostname or '').endswith('.' + d) for d in MEDIA_DOMAINS)

def inspect_mp4(path):
    """Validate box boundaries and extract the actual video track dimensions."""
    total = Path(path).stat().st_size
    found, dimensions = set(), []
    with open(path, 'rb') as f:
        def walk(start, end, depth=0):
            if depth > 4:
                return
            pos = start
            while pos < end:
                if end - pos < 8:
                    raise ValueError('MP4 尾部不完整')
                f.seek(pos)
                size, kind = struct.unpack('>I4s', f.read(8))
                header = 8
                if size == 1:
                    if end - pos < 16:
                        raise ValueError('MP4 扩展头不完整')
                    size = struct.unpack('>Q', f.read(8))[0]
                    header = 16
                elif size == 0:
                    size = end - pos
                if size < header or pos + size > end:
                    raise ValueError('MP4 数据不完整或不是有效视频')
                if depth == 0:
                    found.add(kind)
                if kind in (b'moov', b'trak'):
                    walk(pos + header, pos + size, depth + 1)
                if kind == b'tkhd' and size >= header + 84:
                    f.seek(pos + size - 8)
                    w, h = struct.unpack('>II', f.read(8))
                    if w and h:
                        dimensions.append((w >> 16, h >> 16))
                pos += size
        walk(0, total)
    if not {b'ftyp', b'moov', b'mdat'}.issubset(found) or not dimensions:
        raise ValueError('响应不是带视频轨道的完整 MP4，未保存为成功视频')
    w, h = max(dimensions, key=lambda pair: pair[0] * pair[1])
    return {'width': w, 'height': h, 'bytes': total}

class BatchDownloader:
    def __init__(self, save_dir, max_concurrency=3, timeout=DEFAULT_TIMEOUT):
        self.save_dir = prepare_directory(save_dir)
        self.semaphore = asyncio.Semaphore(max_concurrency)
        self.timeout = timeout
        self.locks = {}

    async def download_single(self, info, variant, client, callback=None, cancel=None):
        result = {'aweme_id': info.aweme_id, 'title': info.title, 'variant_id': variant.id,
                  'status': '等待下载', 'downloaded': 0, 'total_bytes': variant.size,
                  'save_path': '', 'error': '', 'source_urls': info.source_urls, 'quality': variant.label}
        def update(**values):
            result.update(values)
            if callback:
                callback(dict(result))
        def cancelled():
            return cancel is not None and cancel.is_set()
        if info.source == 'yt-dlp':
            return await asyncio.to_thread(self._download_with_ytdlp, info, variant, callback, cancelled)
        target = self.save_dir / info.filename(variant)
        key = str(target)
        async with self.semaphore, self.locks.setdefault(key, asyncio.Lock()):
            if cancelled():
                update(status='已取消')
                return result
            if target.exists():
                try:
                    actual = await asyncio.to_thread(inspect_mp4, target)
                    update(status='已存在', save_path=str(target), downloaded=actual['bytes'], total_bytes=actual['bytes'], actual=actual)
                    return result
                except (OSError, ValueError):
                    pass
            last_error = ''
            for source in variant.urls:
                if cancelled():
                    break
                # One retry for a transient transport failure. CDN failover never
                # silently changes the chosen resolution/codec.
                for attempt in range(2):
                    temp = target.with_name(target.name + '.' + uuid.uuid4().hex + '.part')
                    try:
                        url = source
                        response = None
                        for _ in range(7):
                            if not allowed_media_url(url):
                                raise ValueError('视频源域名不在允许的抖音 CDN 范围内')
                            request = client.build_request('GET', url, headers={'Accept-Encoding': 'identity', **DEFAULT_HEADERS})
                            response = await client.send(request, stream=True, follow_redirects=False)
                            if response.is_redirect:
                                url = urljoin(url, response.headers.get('location', ''))
                                await response.aclose()
                                response = None
                                continue
                            break
                        if response is None:
                            raise ValueError('视频源重定向过多')
                        try:
                            if response.status_code != 200:
                                raise ValueError(f'视频源 HTTP {response.status_code}')
                            content_type = response.headers.get('content-type', '').lower()
                            if any(t in content_type for t in ('text/', 'json', 'image/')):
                                raise ValueError('视频源返回网页或错误信息，不是视频')
                            expected = int(response.headers.get('content-length', '0'))
                            if expected and shutil.disk_usage(self.save_dir).free < expected + 16 * 1048576:
                                raise ValueError('目标磁盘空间不足')
                            downloaded, last_update = 0, 0
                            update(status='下载中', downloaded=0, total_bytes=expected or variant.size)
                            with open(temp, 'xb') as f:
                                async for chunk in response.aiter_bytes(256 * 1024):
                                    if cancelled():
                                        raise asyncio.CancelledError()
                                    f.write(chunk)
                                    downloaded += len(chunk)
                                    if time.monotonic() - last_update > .25:
                                        update(downloaded=downloaded)
                                        last_update = time.monotonic()
                                f.flush()
                                os.fsync(f.fileno())
                            if expected and downloaded != expected:
                                raise ValueError('下载字节数与服务器声明不符，文件不完整')
                            actual = await asyncio.to_thread(inspect_mp4, temp)
                            if variant.width and variant.height and sorted((actual['width'], actual['height'])) != sorted((variant.width, variant.height)):
                                raise ValueError(f'实际分辨率 {actual["width"]}×{actual["height"]} 与所选 {variant.width}×{variant.height} 不符')
                            if cancelled():
                                raise asyncio.CancelledError()
                            os.replace(temp, target)
                            update(status='下载成功', save_path=str(target), downloaded=downloaded, total_bytes=downloaded, actual=actual)
                            return result
                        finally:
                            await response.aclose()
                    except asyncio.CancelledError:
                        update(status='已取消')
                        return result
                    except Exception as e:
                        last_error = f'{type(e).__name__}: {e}'
                        if isinstance(e, httpx.TransportError) and attempt == 0 and not cancelled():
                            await asyncio.sleep(.8)
                            continue
                        break
                    finally:
                        temp.unlink(missing_ok=True)
            update(status='已取消' if cancelled() else '下载失败', error='' if cancelled() else last_error)
            return result

    def _download_with_ytdlp(self, info, variant, callback, cancelled):
        """Download and merge one selected public-platform variant into one MP4."""
        result = {'aweme_id': info.aweme_id, 'title': info.title, 'variant_id': variant.id,
                  'status': '等待下载', 'downloaded': 0, 'total_bytes': variant.size,
                  'save_path': '', 'error': '', 'source_urls': info.source_urls, 'quality': variant.label}
        def update(**values):
            result.update(values)
            if callback:
                callback(dict(result))
        target = self.save_dir / info.filename(variant)
        try:
            import yt_dlp
            if cancelled():
                update(status='已取消')
                return result
            if target.exists():
                actual = inspect_mp4(target)
                update(status='已存在', save_path=str(target), downloaded=actual['bytes'], total_bytes=actual['bytes'], actual=actual)
                return result
            selector = variant.urls[0] if variant.urls else 'bestvideo+bestaudio/best'
            def progress(data):
                if cancelled():
                    raise KeyboardInterrupt('已取消')
                if data.get('status') == 'downloading':
                    update(status='下载中', downloaded=int(data.get('downloaded_bytes') or 0), total_bytes=int(data.get('total_bytes') or data.get('total_bytes_estimate') or variant.size or 0))
            options = {
                'format': selector,
                'merge_output_format': 'mp4',
                'outtmpl': str(target.with_suffix('')) + '.%(ext)s',
                'noplaylist': True,
                'quiet': True,
                'no_warnings': True,
                'overwrites': False,
                'progress_hooks': [progress],
            }
            update(status='下载中')
            with yt_dlp.YoutubeDL(options) as ydl:
                ydl.download(info.source_urls[:1])
            if cancelled():
                update(status='已取消')
                return result
            if not target.is_file():
                candidates = sorted(self.save_dir.glob(target.stem + '.*'), key=lambda p: p.stat().st_mtime, reverse=True)
                target = next((p for p in candidates if p.suffix.lower() == '.mp4'), target)
            actual = inspect_mp4(target)
            update(status='下载成功', save_path=str(target), downloaded=actual['bytes'], total_bytes=actual['bytes'], actual=actual)
        except KeyboardInterrupt:
            update(status='已取消')
        except Exception as e:
            update(status='下载失败', error=f'{type(e).__name__}: {e}')
        return result

    async def download_all(self, selections, callback=None, cancel=None, client=None):
        if client is None:
            async with httpx.AsyncClient(timeout=httpx.Timeout(self.timeout, read=30), limits=httpx.Limits(max_connections=6)) as c:
                return await self.download_all(selections, callback, cancel, c)
        # A duplicate input may have different URLs but must only write once.
        unique = {(info.aweme_id, variant.id): (info, variant) for info, variant in selections}
        return await asyncio.gather(*(self.download_single(info, variant, client, callback, cancel) for info, variant in unique.values()))
