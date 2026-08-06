# Kanav (kanav.ad)

Chinese AV site (MacCMS v10). Crawler: `kanav.py` (protocol: crawler.v2).

## Site categories

| id | Category |
|----|----------|
| 1  | 中文字幕 (Chinese subtitles) |
| 2  | 日韩有码 (JP/KR censored) |
| 3  | 日韩无码 (JP/KR uncensored) |
| 4  | 国产AV (Chinese AV) |
| 22 | 流出自拍 (Leaked amateur) |
| 30 | 自拍泄密 (Private self-recorded leaks) |
| 31 | 探花约炮 (Hidden-camera hookups) |
| 32 | 主播录制 (Streamer recordings) |
| 20 | 动漫番剧 (Anime) |
| 25 | 里番 (R18 anime) |
| 26 | 泡面番 (Short anime) |
| 27 | Motion Anime |
| 28 | 3D动画 (3D animation) |
| 29 | 同人作品 (Doujin works) |

Plus a "hot" label page (`/index.php/label/hot.html`, single page, 48 videos, no pagination).

## Supported crawl targets

- `config.source = "category"` (default): any category above via `config.category_id`
- `config.source = "hot"`: the hot label page (48 videos, single page)

## Default behavior

With no config, the crawler targets **category id=22 (流出自拍 / Leaked amateur)**,
paged newest-first (~621 pages).

Example job config:

```json
"config": {
  "source": "category",
  "category_id": 22,
  "workers": 4
}
```
