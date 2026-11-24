# 运行与缓存

## 首次初始化
1. 在 GitHub Actions 手动运行 **Vegas History Warmup**。
2. 工作流验证 22 个合约映射。
3. 对每个合约分别预热 1H、4H 历史 K 线，直到 Bitget 历史接口返回边界。
4. SQLite 缓存压缩后保存为 GitHub Release 资产 `market-cache.sqlite.gz`。

## 日常扫描
- **Vegas Scheduled Scan** 每日按北京时间 08:05、10:05、16:05、20:05、22:05 执行。
- 下载缓存 Release，增量补充 1H、4H K 线，运行扫描。
- 更新 `data/signals.json`，并将 SQLite 快照更新到 Release。
- Bark 应读取 `data/signals.json`；通知模板与推送逻辑不属于本仓库。

## 权限
Repository Settings → Actions → General → Workflow permissions 需要允许 Read and write permissions。
工作流使用内置 GITHUB_TOKEN，不需要 Bitget API Key（行情接口为公开接口）。

## 数据完整性
- 预热遇到任一配置合约在 Bitget 不存在时，工作流失败并列出缺失合约，不会将部分缓存标记为完整。
- 每个合约/周期的实际缓存条数和最早、最新时间由 SQLite 保存。
- Release 资产是缓存数据库的持久化副本；Git 仓库不存放不断增长的数据库文件。
