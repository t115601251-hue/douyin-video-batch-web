"""Public-video adapter backed by yt-dlp's maintained extractor collection."""
import asyncio
import hashlib
import re

from .parser import VideoInfo, VideoVariant, number


class UniversalVideoParser:
    """Resolve a public single-video URL without treating a playlist as one video."""

    async def parse(self, url):
        return await asyncio.to_thread(self._parse, url)

    def _parse(self, url):
        try:
            import yt_dlp
        except ImportError:
            return VideoInfo('', source_urls=[url], error_msg='缺少 yt-dlp。请按 requirements.txt 安装依赖后重启。')
        try:
            options = {
                'quiet': True,
                'no_warnings': True,
                'noplaylist': True,
                'skip_download': True,
                'socket_timeout': 25,
            }
            with yt_dlp.YoutubeDL(options) as ydl:
                data = ydl.extract_info(url, download=False)
            if not isinstance(data, dict) or data.get('_type') in ('playlist', 'multi_video'):
                raise ValueError('请粘贴单条视频链接，不支持把播放列表当作一个视频下载')
            source_id = str(data.get('id') or hashlib.sha256(url.encode()).hexdigest()[:16])
            extractor = str(data.get('extractor_key') or data.get('extractor') or '通用平台')
            info = VideoInfo(
                aweme_id=f'{extractor}:{source_id}',
                title=str(data.get('title') or '未命名视频'),
                author=str(data.get('uploader') or data.get('channel') or data.get('creator') or '未知作者'),
                duration_sec=number(data.get('duration')),
                cover_url=str(data.get('thumbnail') or ''),
                source_urls=[url],
                source='yt-dlp',
            )
            variants = {}
            for fmt in data.get('formats') or []:
                if not isinstance(fmt, dict) or fmt.get('vcodec') in (None, 'none'):
                    continue
                format_id = str(fmt.get('format_id') or '')
                if not format_id:
                    continue
                # A video-only stream is merged with the best available audio.
                selector = format_id if fmt.get('acodec') not in (None, 'none') else f'{format_id}+bestaudio/best'
                width, height = number(fmt.get('width')), number(fmt.get('height'))
                codec = str(fmt.get('vcodec') or '视频')
                bitrate = number(fmt.get('tbr')) * 1000
                key = 'yt-' + hashlib.sha256(selector.encode()).hexdigest()[:12]
                variants.setdefault(key, VideoVariant(key, width, height, bitrate, codec, number(fmt.get('filesize') or fmt.get('filesize_approx')), [selector]))
            info.variants = sorted(variants.values(), key=lambda v: (v.width * v.height, v.bitrate), reverse=True)
            if not info.variants:
                info.error_msg = '该网站未提供可下载的视频流，可能需要登录、地区权限或受 DRM 保护'
            return info
        except Exception as e:
            return VideoInfo('', source_urls=[url], error_msg=f'通用解析失败：{e}')
