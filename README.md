# 视频批量下载工作台

网页工具：粘贴公开视频链接，核对作品后选择真实可用分辨率，批量下载为 MP4。抖音按作品 ID 严格核对；其他公开平台使用 `yt-dlp` 的站点适配器。

[![Deploy to Render](https://render.com/images/deploy-to-render-button.svg)](https://render.com/deploy?repo=https://github.com/t115601251-hue/douyin-video-batch-web)

## 使用

1. 粘贴公开视频页、短链接或包含链接的分享文案。链接可以分行，也可连续粘贴，例如 `链接1https://链接2`。
2. 点击“解析并核对链接”，查看标题、作者、来源和真实可选分辨率。
3. 选择每个作品的清晰度，批量下载。每个作品只保留一个 MP4 文件。

### 本机版

双击 `双击启动.bat`。文件保存到你确认的本机目录，默认是项目内的 `downloads`。

### 公网版

项目包含 [Render](https://render.com) 的免费部署配置 `render.yaml`。公网访问时，服务按浏览器会话隔离任务；下载完成后点击“下载 MP4 到设备”。免费实例空闲后会休眠，服务重启时临时文件会清除，所以请及时下载。

## 开发运行

```powershell
pip install -r requirements.txt
python web_server.py
```

程序只处理公开可访问的视频链接。私密、失效、需要登录、付费或 DRM 保护的内容无法解析或下载。

## 仓库内容

源码仓库不包含 `python_env`、`downloads`、`settings.json`、日志和已下载视频；`.gitignore` 已排除这些本地文件。
