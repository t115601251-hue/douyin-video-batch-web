"""One active batch, responsive status requests and cancellable async work."""
import asyncio
import copy
import threading
import time
import uuid
from pathlib import Path
import httpx
from config import DEFAULT_HEADERS, DEFAULT_TIMEOUT, prepare_directory
from .extractor import extract_public_links, is_douyin_url
from .parser import DouyinVideoParser, VideoInfo
from .universal import UniversalVideoParser
from .downloader import BatchDownloader

class JobManager:
    def __init__(self):
        self.lock = threading.RLock()
        self.jobs = {}
        self.active = {}
        self.files = {}

    def _new(self, owner, kind):
        with self.lock:
            if self.active.get(owner):
                raise ValueError('当前页面已有批次在运行，请等待完成或取消后再试')
            # Completed batches are retained for refresh/retry during this run.
            if len(self.jobs) >= 30:
                oldest = next(iter(self.jobs))
                self.jobs.pop(oldest)
            jid = uuid.uuid4().hex
            job = dict(id=jid, owner=owner, kind=kind, state='running', items=[], total=0, completed=0,
                       message='', save_dir='', cancel=threading.Event(), infos=[], loop=None, task=None)
            self.jobs[jid] = job
            self.active[owner] = jid
            return job

    def snapshot(self, owner, jid):
        with self.lock:
            if jid not in self.jobs or self.jobs[jid].get('owner') != owner:
                raise ValueError('任务已失效，请重新解析链接')
            return copy.deepcopy({k: v for k, v in self.jobs[jid].items() if k not in ('owner', 'cancel', 'infos', 'loop', 'task')})

    def launch(self, job, coro):
        def worker():
            async def run():
                with self.lock:
                    job['loop'] = asyncio.get_running_loop()
                    job['task'] = asyncio.current_task()
                if job['cancel'].is_set():
                    raise asyncio.CancelledError()
                await coro()
            try:
                asyncio.run(run())
                with self.lock:
                    job['state'] = 'cancelled' if job['cancel'].is_set() else 'done'
            except asyncio.CancelledError:
                with self.lock:
                    job['state'] = 'cancelled'
                    for row in job['items']:
                        if row.get('status') in ('下载中', '等待下载', '等待解析', '解析中'):
                            row['status'] = '已取消'
            except Exception as e:
                with self.lock:
                    job['state'] = 'failed'
                    job['message'] = str(e)
            finally:
                with self.lock:
                    job['loop'] = job['task'] = None
                    if self.active.get(job['owner']) == job['id']:
                        self.active.pop(job['owner'], None)
        threading.Thread(target=worker, daemon=True, name='batch-' + job['id'][:8]).start()

    def cancel(self, owner, jid):
        with self.lock:
            job = self.jobs.get(jid)
            if not job or job.get('owner') != owner or job['state'] != 'running':
                return
            job['cancel'].set()
            if job['loop'] and job['task']:
                job['loop'].call_soon_threadsafe(job['task'].cancel)

    def resolve_file(self, owner, fid):
        with self.lock:
            record = self.files.get(fid)
            if not record or record[0] != owner:
                return None
            return record[1]

    def parse(self, owner, text):
        urls = extract_public_links(text)
        if not urls:
            raise ValueError('没有识别到公开网页链接。请粘贴以 http:// 或 https:// 开头的视频页或分享链接')
        job = self._new(owner, 'parse')
        job['total'] = len(urls)
        job['items'] = [{'source_urls': [u], 'status': '等待解析'} for u in urls]
        async def work():
            semaphore = asyncio.Semaphore(3)
            douyin_parser, universal_parser = DouyinVideoParser(), UniversalVideoParser()
            async with httpx.AsyncClient(headers=DEFAULT_HEADERS, timeout=DEFAULT_TIMEOUT, limits=httpx.Limits(max_connections=6)) as client:
                async def one(index, url):
                    async with semaphore:
                        with self.lock:
                            job['items'][index]['status'] = '解析中'
                        try:
                            parser = douyin_parser if is_douyin_url(url) else universal_parser
                            info = await asyncio.wait_for(parser.parse(url, client) if parser is douyin_parser else parser.parse(url), 85)
                        except asyncio.TimeoutError:
                            info = VideoInfo('', source_urls=[url], error_msg='解析超时，请稍后重试')
                        with self.lock:
                            job['items'][index] = info.public() | {'status': '已核对' if info.is_video else '解析失败'}
                            job['completed'] += 1
                        return info
                infos = await asyncio.gather(*(one(i, u) for i, u in enumerate(urls)))
            merged, failures = {}, []
            for info in infos:
                if not info.is_video:
                    failures.append(info)
                elif info.aweme_id in merged:
                    merged[info.aweme_id].source_urls = list(dict.fromkeys(merged[info.aweme_id].source_urls + info.source_urls))
                else:
                    merged[info.aweme_id] = info
            with self.lock:
                job['infos'] = list(merged.values())
                job['items'] = [i.public() | {'status': '已核对' if i.is_video else '解析失败'} for i in list(merged.values()) + failures]
                job['unique_count'] = len(merged)
                job['message'] = f'已核对 {len(merged)} 个不同视频；解析失败 {len(failures)} 条；合并重复作品 {len(infos) - len(merged) - len(failures)} 条'
        self.launch(job, work)
        return job['id']

    def download(self, owner, parse_id, choices, save_dir):
        with self.lock:
            parsed = self.jobs.get(parse_id)
            if not parsed or parsed.get('owner') != owner or parsed['kind'] != 'parse' or parsed['state'] != 'done':
                raise ValueError('请先完成链接解析，再选择清晰度下载')
            if not isinstance(choices, list) or not choices or len(choices) > 100:
                raise ValueError('请勾选 1～100 个视频')
            selections, seen = [], set()
            for choice in choices:
                if not isinstance(choice, dict):
                    raise ValueError('无效的视频选择')
                info = next((i for i in parsed['infos'] if i.aweme_id == choice.get('aweme_id')), None)
                variant = next((v for v in info.variants if v.id == choice.get('variant_id')), None) if info else None
                if not variant:
                    raise ValueError('所选视频或清晰度不属于此解析任务，请重新解析')
                if info.aweme_id not in seen:
                    selections.append((info, variant))
                    seen.add(info.aweme_id)
            path = prepare_directory(save_dir)
            job = self._new(owner, 'download')
            job['save_dir'] = str(path)
            job['parse_id'] = parse_id
            job['total'] = len(selections)
            job['items'] = [{'aweme_id': i.aweme_id, 'title': i.title, 'status': '等待下载', 'quality': v.label} for i, v in selections]
        async def work():
            downloader = BatchDownloader(str(path))
            def update(row):
                with self.lock:
                    if row.get('save_path'):
                        fid = uuid.uuid5(uuid.NAMESPACE_URL, row['save_path']).hex
                        self.files[fid] = (job['owner'], Path(row['save_path']))
                        row['file_id'] = fid
                    index = next(k for k, r in enumerate(job['items']) if r['aweme_id'] == row['aweme_id'])
                    job['items'][index] = row
                    job['completed'] = sum(r['status'] in ('下载成功', '下载失败', '已存在', '已取消') for r in job['items'])
            await downloader.download_all(selections, update, job['cancel'])
            with self.lock:
                good = sum(r['status'] in ('下载成功', '已存在') for r in job['items'])
                bad = sum(r['status'] == '下载失败' for r in job['items'])
                job['message'] = f'已保存/已存在 {good} 个，失败 {bad} 个。实际保存目录：{path}'
        self.launch(job, work)
        return job['id']
