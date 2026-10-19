# Agent 约定

研究任务遵循 [深入研究 skill](skills/deep-research/SKILL.md)。尊重事实，其余研究判断尽量交给 Agent 自主发挥。

## 研究报告发布约定

- 用户要求保存到本仓库的研究成果，使用 UTF-8 Markdown 放入 `reports/`；一篇报告一个文件，使用稳定且有意义的文件名。
- 每篇必须包含 YAML 元数据 `title` 和带时区的 ISO 8601 `date`，例如 `2026-10-18T10:00:00+08:00`。可选 `author`、`updated`、布尔值 `draft`。
- 正文写完整报告，注明来源链接和不确定性，不要只写摘要或外部文档链接。默认语言跟随用户要求。
- 草稿设置 `draft: true`；草稿文件依然公开，不得提交秘密或私人信息。
- 图片放在 `reports/assets/` 或报告旁的资源目录，使用相对路径；不要提交本地绝对路径。
- `feed.xml` 和 `atom.xml` 是自动生成文件，禁止手动维护。推送 `main` 后由 GitHub Actions 更新。
- 发布前运行 `python -m unittest discover -s scripts -p 'test_*.py'` 和 `python scripts/build_feed.py`。若环境缺少依赖，先安装 `requirements.txt`。
- 保持报告的文件路径、标题和发布日期稳定；修订使用 `updated`。KOReader 可能需要删除旧 EPUB 再同步才能获取修订。
