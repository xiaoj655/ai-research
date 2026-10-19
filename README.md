# AI Research

AI Agent 研究报告仓库，提供适合 KOReader 的全文 RSS / Atom 订阅。

研究流程见 [深入研究 skill](skills/deep-research/SKILL.md)，仓库约定见 [AGENTS.md](AGENTS.md)。

## 在掌阅 / KOReader 中订阅

RSS（推荐）：

```text
https://raw.githubusercontent.com/xiaoj655/ai-research/main/feed.xml
```

Atom（任选一个订阅即可）：

```text
https://raw.githubusercontent.com/xiaoj655/ai-research/main/atom.xml
```

在「新闻下载器（RSS/Atom）」中添加地址并同步。菜单名称随 KOReader 语言和版本可能不同。

- **Download full article / 下载全文：关闭**，直接使用订阅中的完整正文。
- **Include images / 包含图片：开启**，需要图片时同步下载。
- **Limit / 下载条数：0** 表示下载订阅中的全部文章；日常可设为 10。

若手动编辑 `feed_config.lua`，完整示例为：

```lua
return {
    { "https://raw.githubusercontent.com/xiaoj655/ai-research/main/feed.xml", limit = 0, download_full_article = false, include_images = true },
}
```

每篇文章会生成独立 EPUB，可离线阅读。仓库附带一篇订阅测试文章。图片效果和复杂表格、数学公式的排版取决于阅读器；公式建议写成普通文本或图片。

## 发布研究报告

将 UTF-8 Markdown 文件存入 `reports/`，支持子目录。每篇文件开头需要 YAML 元数据：

```markdown
---
title: "研究主题：关键结论"
date: "2026-10-18T10:00:00+08:00"
author: "AI Research"
---

## 结论

这里是报告全文……

## 证据与来源

- [来源标题](https://example.com/article)
```

`title`、`date` 必填；日期必须带时区。可选 `updated` 表示最后修订时间，不能早于 `date`。`draft: true` 会排除在订阅源之外，但文件仍是公开的。date 只用于排序，不用于定时发布；未准备发布的报告应设为草稿。

报告合并或推送至 `main` 后，Actions 自动生成并提交根目录的 `feed.xml`、`atom.xml`。无需 GitHub Pages、服务器或额外令牌。Raw 地址存在缓存，更新可能需要等几分钟。不要手动编辑生成的 XML 文件。

图片建议保存到 `reports/assets/`，正文使用相对路径，如 `![图示](assets/diagram.png)`；生成器会转换成绝对地址。普通相对链接会指向 GitHub 对应文件。外部图片必须能被设备直接访问。避免依赖 GitHub 登录、网页脚本或本地文件。

订阅源包含所有非草稿报告，并按发布日期倒序排列。报告较多或图片较大时，首次同步会更慢，可限制下载条数。

KOReader 通常会跳过已经下载的同名文章；更新报告正文后，可能需要删除设备上对应 EPUB 再同步，才能看到修订内容。尽量保持文件路径、标题、发布日期稳定，避免重复下载。

## 本地检查

```sh
python -m pip install -r requirements.txt
python -m unittest discover -s scripts -p 'test_*.py'
python scripts/build_feed.py
```

Actions 失败时查看仓库的 Actions 页面。若仓库规则禁止机器人推送 `main`，需要调整相应规则或改成通过 PR 更新生成文件。
