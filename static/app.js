'use strict';
const $ = id => document.getElementById(id);
let parsedId = '', activeId = '', rows = [], lastDownload = null, pollTimer = null, hosted = false;
const preferences = new Map();
function el(tag, text, cls) { const e = document.createElement(tag); if (text !== undefined) e.textContent = text; if (cls) e.className = cls; return e; }
function status(text, error = false) { $('status').textContent = text; $('status').classList.toggle('error', error); }
async function api(path, data) {
  const response = await fetch(path, {method: data === undefined ? 'GET' : 'POST', headers: {'Content-Type': 'application/json'}, ...(data === undefined ? {} : {body: JSON.stringify(data)})});
  const body = await response.json();
  if (!response.ok) throw new Error(body.error || `请求失败 ${response.status}`);
  return body;
}
function applyHostedMode(value) {
  hosted = Boolean(value);
  if (!hosted) return;
  $('localSaveControls').hidden = true;
  $('publicDownloadNotice').hidden = false;
  $('saveHeading').innerHTML = '<b>02</b> 确认下载方式';
  $('downloadBtn').textContent = '批量下载选中视频';
  $('workspaceMode').textContent = 'VIDEO / PUBLIC WORKSPACE';
  $('locationBadge').textContent = '● 公网运行 · 下载到你的设备';
  document.querySelector('header p').textContent = '先核对作品，再选择清晰度，下载为 MP4。';
  $('previewPanel').hidden = true;
  $('playerHint').textContent = '公网版本请直接下载 MP4 到你的设备。服务端文件会在服务重启后清除。';
}
function busy(value) {
  for (const id of ['parseBtn', 'downloadBtn', 'clearBtn', 'globalQuality', 'selectAll']) $(id).disabled = value;
  $('cancelBtn').hidden = !value;
  if (!value) $('downloadBtn').disabled = !parsedId || !rows.some(r => r.matched);
  document.querySelectorAll('#videos input, #videos select').forEach(e => e.disabled = value || e.dataset.invalid === '1');
}
function bytes(n) { return n ? `${(n / 1048576).toFixed(1)} MB` : '大小未知'; }
function chooseVariant(variants, preference) {
  const ranked = [...variants].sort((a,b) => (b.width*b.height-a.width*a.height) || (b.bitrate-a.bitrate));
  if (preference === 'best') return ranked[0];
  if (preference === 'compatible') return ranked.find(v => v.codec === 'H.264') || ranked[0];
  const limit = Number(preference);
  return ranked.find(v => v.width && v.height && Math.min(v.width, v.height) <= limit) || ranked[ranked.length - 1];
}
function renderVideos(items) {
  $('videos').replaceChildren(); $('empty').hidden = items.length > 0;
  items.forEach(row => {
    const box = el('article', undefined, 'video-row');
    const check = el('input'); check.type = 'checkbox'; check.checked = row.matched;
    check.dataset.id = row.aweme_id || ''; check.setAttribute('aria-label', `选择 ${row.title || '此作品'}`);
    if (!row.matched) { check.disabled = true; check.dataset.invalid = '1'; }
    const details = el('div'); details.append(el('p', row.title || row.status || '等待解析', 'video-title'));
    details.append(el('div', `${row.author || '待获取作者'} · ${row.duration_sec || 0} 秒 · 标识 ${row.aweme_id || '待核对'}`, 'metadata'));
    if (row.matched) details.append(el('span', row.source === 'yt-dlp' ? '✓ 已由通用平台适配器核对' : '✓ 抖音作品 ID 已一致核对', 'matched'));
    else if (row.error) details.append(el('p', row.error, 'hint error'));
    for (const source of row.source_urls || []) {
      const a = el('a', source, 'source-link'); a.href = source; a.target = '_blank'; a.rel = 'noopener noreferrer'; details.append(a);
    }
    const quality = el('div');
    if (row.variants && row.variants.length) {
      const label = el('label', '保存分辨率 / 编码 / 码率');
      const select = el('select'); select.dataset.id = row.aweme_id; select.setAttribute('aria-label', `${row.title} 的分辨率`);
      for (const v of row.variants) { const o = el('option', v.label); o.value = v.id; select.append(o); }
      const chosen = preferences.get(row.aweme_id) || chooseVariant(row.variants, $('globalQuality').value).id;
      select.value = row.variants.some(v => v.id === chosen) ? chosen : row.variants[0].id;
      preferences.set(row.aweme_id, select.value);
      select.addEventListener('change', () => preferences.set(row.aweme_id, select.value));
      quality.append(label, select, el('p', `${row.variants.length} 个真实视频源 · 同一作品只保存一次`, 'quality-note'));
    }
    box.append(check, details, quality); $('videos').append(box);
  });
}
function fileUrl(id, download = false) { return `/api/file?id=${encodeURIComponent(id)}${download ? '&download=1' : ''}`; }
function renderDownloads(items) {
  $('downloads').replaceChildren();
  items.forEach(row => {
    const box = el('div', undefined, 'download-row');
    const head = el('div', undefined, 'download-head'); head.append(el('strong', row.title), el('span', row.status, row.status === '下载失败' ? 'error' : ''));
    box.append(head, el('div', `${row.quality || ''} · ${bytes(row.downloaded)} / ${bytes(row.total_bytes)}`, 'metadata'));
    if (row.status === '下载中') { const p = el('progress'); p.max = row.total_bytes || 1; if (row.total_bytes) p.value = row.downloaded || 0; box.append(p); }
    if (row.save_path && !hosted) box.append(el('div', `已保存：${row.save_path}`, 'download-path'));
    if (row.save_path && hosted) box.append(el('div', '文件已临时保存在服务端，请尽快下载到你的设备。', 'download-path'));
    if (row.actual) box.append(el('div', `文件实测分辨率：${row.actual.width} × ${row.actual.height}`, 'metadata'));
    if (row.error) box.append(el('div', row.error, 'hint error'));
    if (row.file_id) {
      const actions = el('div', undefined, 'actions');
      const save = el('a', hosted ? '下载 MP4 到设备' : '另存一份'); save.href = fileUrl(row.file_id, true);
      if (hosted) { save.download = ''; save.addEventListener('click', () => setTimeout(() => $('downloadDialog').showModal(), 120)); actions.append(save); }
      else { const play = el('button', '预览本地视频'); play.addEventListener('click', () => { $('previewPanel').hidden = false; $('previewTitle').textContent = row.title; $('player').src = fileUrl(row.file_id); $('player').load(); $('previewPanel').scrollIntoView({behavior: 'smooth', block: 'center'}); }); actions.append(play, save); }
      box.append(actions);
    }
    $('downloads').append(box);
  });
}
async function poll(id) {
  clearTimeout(pollTimer);
  try {
    const job = await api(`/api/job?id=${encodeURIComponent(id)}`);
    $('progress').value = job.total ? job.completed / job.total * 100 : 0;
    status(job.message || `${job.kind === 'parse' ? '解析核对' : '批量下载'}：${job.completed} / ${job.total} 项完成`);
    if (job.kind === 'parse') {
      renderVideos(job.items);
      if (job.state === 'done') { parsedId = id; rows = job.items; localStorage.setItem('douyinParse', id); }
    } else { lastDownload = job; localStorage.setItem('douyinDownload', id); renderDownloads(job.items); }
    if (job.state === 'running') { busy(true); pollTimer = setTimeout(() => poll(id), 750); return; }
    activeId = ''; localStorage.removeItem('douyinActive'); busy(false);
    if (job.state === 'cancelled') status('任务已取消。已完成的视频保留在所选目录中；未完成文件不会被当成成功。');
    if (job.state === 'failed') status(job.message || '任务失败', true);
    $('retryBtn').hidden = !(job.kind === 'download' && job.items.some(r => ['下载失败', '已取消'].includes(r.status)));
  } catch(e) { activeId = ''; busy(false); status(e.message + '。若服务已重启，请重新解析；磁盘中的文件会保留。', true); }
}
async function startParse() {
  busy(true); parsedId = ''; rows = []; preferences.clear(); $('retryBtn').hidden = true;
  $('downloads').replaceChildren(); lastDownload = null;
  localStorage.removeItem('douyinParse');
  localStorage.removeItem('douyinDownload');
  try { const job = await api('/api/parse', {text: $('links').value}); activeId = job.id; localStorage.setItem('douyinActive', job.id); poll(job.id); }
  catch(e) { busy(false); status(e.message, true); }
}
async function startDownload(retry = false) {
  let choices;
  if (retry && lastDownload) choices = lastDownload.items.filter(r => ['下载失败','已取消'].includes(r.status)).map(r => ({aweme_id: r.aweme_id, variant_id: preferences.get(r.aweme_id) || r.variant_id}));
  else choices = [...document.querySelectorAll('#videos input:checked')].map(e => ({aweme_id: e.dataset.id, variant_id: preferences.get(e.dataset.id)}));
  if (!choices.length) { status('请至少勾选一个视频', true); return; }
  busy(true); $('retryBtn').hidden = true;
  try {
    let saveDir = hosted ? '' : $('saveDir').value;
    if (!hosted) { const checked = await api('/api/path', {path: saveDir}); saveDir = checked.path; $('saveDir').value = saveDir; $('pathStatus').textContent = `实际保存目录：${saveDir}`; }
    const job = await api('/api/download', {parse_id: parsedId, choices, save_dir: saveDir});
    activeId = job.id; localStorage.setItem('douyinActive', job.id); poll(job.id);
  } catch(e) { busy(false); status(e.message, true); }
}
$('parseBtn').addEventListener('click', startParse);
$('downloadBtn').addEventListener('click', () => startDownload());
$('retryBtn').addEventListener('click', () => startDownload(true));
$('cancelBtn').addEventListener('click', async () => { try { await api('/api/cancel', {id: activeId}); status('正在取消并清理未完成文件…'); } catch(e) { status(e.message, true); } });
$('clearBtn').addEventListener('click', () => { $('links').value = ''; });
$('selectAll').addEventListener('click', () => { const checks = [...document.querySelectorAll('#videos input:not(:disabled)')]; const value = !checks.every(e => e.checked); checks.forEach(e => e.checked = value); });
$('globalQuality').addEventListener('change', () => {
  rows.filter(r => r.matched).forEach(r => { const v = chooseVariant(r.variants, $('globalQuality').value); preferences.set(r.aweme_id, v.id); const s = [...document.querySelectorAll('#videos select')].find(e => e.dataset.id === r.aweme_id); if (s) s.value = v.id; });
});
$('pathBtn').addEventListener('click', async () => { try { const data = await api('/api/path', {path: $('saveDir').value}); $('saveDir').value = data.path; $('pathStatus').textContent = `已确认可写：${data.path}`; } catch(e) { $('pathStatus').textContent = e.message; status(e.message, true); } });
$('dismissDownloadDialog').addEventListener('click', () => $('downloadDialog').close());
$('openDownloadsBtn').addEventListener('click', () => { $('downloadDialog').close(); window.open('chrome://downloads/', '_blank', 'noopener'); });
$('closePreview').addEventListener('click', () => { $('player').pause(); $('player').removeAttribute('src'); $('player').load(); $('previewPanel').hidden = true; });
$('player').addEventListener('error', () => { $('playerHint').textContent = '浏览器无法播放此编码。文件已保存，请用本机播放器打开，或重新选择 H.264 视频源。'; });
(async () => {
  try {
    const config = await api('/api/config'); $('saveDir').value = config.save_dir; applyHostedMode(config.hosted);
    const oldParse = localStorage.getItem('douyinParse');
    if (oldParse) { try { const p = await api(`/api/job?id=${oldParse}`); if (p.state === 'done') { parsedId = oldParse; rows = p.items; renderVideos(rows); busy(false); } } catch { localStorage.removeItem('douyinParse'); } }
    const active = localStorage.getItem('douyinActive');
    if (active) { activeId = active; busy(true); poll(active); }
    else {
      const oldDownload = localStorage.getItem('douyinDownload');
      if (oldDownload) {
        try {
          const job = await api(`/api/job?id=${oldDownload}`);
          if (job.parse_id === parsedId) {
            lastDownload = job;
            job.items.forEach(r => { if (r.variant_id) preferences.set(r.aweme_id, r.variant_id); });
            renderVideos(rows); renderDownloads(job.items);
            status(job.message || '已恢复上次任务结果');
            $('progress').value = job.total ? job.completed / job.total * 100 : 0;
            $('retryBtn').hidden = !job.items.some(r => ['下载失败', '已取消'].includes(r.status));
          }
        } catch { localStorage.removeItem('douyinDownload'); }
      }
    }
    if (rows.length && !$('links').value) $('links').value = rows.flatMap(r => r.source_urls || []).join('\n');
  } catch(e) { status(e.message, true); }
})();
