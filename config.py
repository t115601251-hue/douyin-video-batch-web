"""Portable configuration; imports never write to other drives."""
import re
import shutil
import tempfile
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
DEFAULT_DOWNLOAD_DIR = BASE_DIR / 'downloads'
DEFAULT_TIMEOUT = 18
DEFAULT_MAX_CONCURRENCY = 3
MAX_LINKS = 100
MAX_BODY = 256 * 1024
DEFAULT_HEADERS = {
    'User-Agent': 'Mozilla/5.0 (iPhone; CPU iPhone OS 16_6 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/16.6 Mobile/15E148 Safari/604.1',
    'Accept-Language': 'zh-CN,zh;q=0.9',
    'Referer': 'https://www.douyin.com/',
}

def prepare_directory(raw: str) -> Path:
    raw = raw.strip().strip('\"\'') if raw else str(DEFAULT_DOWNLOAD_DIR)
    raw = re.sub(r'^([A-Za-z])盘[\\/]*', lambda m: m[1].upper() + ':\\', raw)
    p = Path(raw).expanduser()
    if not p.is_absolute() or str(p).startswith(('\\\\', '//')):
        raise ValueError('请输入完整的本机目录，例如 D:\\DouyinVideos；不支持网络共享路径')
    p = p.resolve()
    p.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryFile(dir=p):
        pass
    if shutil.disk_usage(p).free < 32 * 1024 * 1024:
        raise ValueError('目标磁盘剩余空间不足 32 MB，请选择其他目录')
    return p
