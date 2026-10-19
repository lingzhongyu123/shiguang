# 拾光：脱敏交接（2026-10-19）

源码：https://github.com/lingzhongyu123/shiguang 。公开仓库不得包含任何真实凭据、私人日志或解密备份。

## 运行与接口

- FastAPI + PostgreSQL/pgvector；静态前端，无构建链。
- Render 环境变量：DATABASE_URL、SHIGUANG_PASSWORD、ZHIPU_API_KEY、GLM_MODEL、GLM_MODEL_DEEP。值只在管理平台配置。
- GitHub Actions Secrets：RENDER_API_KEY、SHIGUANG_PASSWORD、BACKUP_KEY。不可通过日志输出或导出密钥。
- POST /api/process 返回 jobId；GET /api/jobs/{id} 轮询状态及 preview。只有 done 表示已归档。
- GET /api/jobs 列出未完成任务；POST /api/jobs/{id}/retry 保留原文并重试。
- preview 是临时文本，不得据此写入画像。最终完整 JSON 校验后再事务归档。
- GET /api/health 返回 ok 与 Render Git commit；用精确 commit 验收上线。
- 普通模型调用总预算 480 秒，深度 900 秒；连接错误允许有限重试，读超时不重试。服务端 20 分钟回收中断任务，前端 21 分钟轮询兜底。
- GLM 推理档：日常 low，深度 high。输出预算：只润色/快速 16384，其余 32768。预算是上限，不是要求写满。
- 提示词 v2.2-stream-grounded；静态资源 v14；登录 token 有效期一年。

## 部署与备份

- 代码推送触发 deploy；只改 backup.enc 或文档不会触发此工作流。
- 部署密钥缺失必须失败；Render live 且 /api/health 的 commit 匹配才视为成功。
- restore-check 手动工作流将 backup.enc 解密后导入 runner 内的临时 PostgreSQL，重复导入验证幂等；不接触生产库、不上传明文 artifact。
- 加密备份的旧版本保留在 Git 历史中，不会自动裁剪到 30 份。
- 改网站口令须同步 Render 与 GitHub SHIGUANG_PASSWORD；改 Render API Key 须同步 GitHub RENDER_API_KEY；改数据库密码须同步 DATABASE_URL。先验证新凭据，再撤销旧凭据。

## 验证与限制

本地 47 项 Python 回归及 7 项前端测试通过（虚构数据、临时本机数据库）。真实模型测速使用 tools/benchmark_llm.py，密钥从环境变量传入，不保存。

2026-10-19 本机到智谱的单次结果（非 SLA，不含 Render/数据库/embedding 开销）：

| 场景 | 模型 | 首正文 token | 完成 |
|---|---|---:|---:|
| 虚构短日志，无历史 | Flash low | 1.9 秒 | 12.2 秒 |
| 虚构短日志，无历史 | GLM high | 8.6 秒 | 28.0 秒 |
| 虚构 932 字 + 8 条历史 | Flash low | 2.4 秒 | 17.9 秒 |
| 虚构 932 字 + 8 条历史 | GLM high | 44.5 秒 | 70.7 秒 |

长样本首个可显示正文分别为 3.2 秒和 48.2 秒。每格仅一次样本，提示词在短样本之后加强了候选记忆事实边界，不作为严格 A/B 对比。首轮发现模型把单次阅读推成习惯，已加强提示词；候选记忆仍必须人工确认。

实际生产部署、加密恢复演练和凭据轮换状态以当次工作记录为准，不能从本文件或工作流触发成功推断已完成。
