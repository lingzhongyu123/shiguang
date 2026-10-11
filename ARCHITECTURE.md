# 拾光 · 个人成长日志 Agent — 架构与方案

> 版本:v1.2(2026-10-11 已部署上线)
> 网址:https://shiguang-ryos.onrender.com

## 一、系统定位

一个跑在手机浏览器里的私人日志 Agent:用户随时把**口述的、口语化的**日志丢进去,系统自动完成「整理表达 → 提炼内容 → 给出建议 → 管理记忆 → 构建画像」五件事,长期沉淀出一套可回看、可迁移的个人知识系统。

核心设计原则(优先级从高到低):

```
忠于事实 > 保留真实表达 > 提高清晰度 > 提炼价值 > 有依据的建议 > 积累记忆
```

## 二、总体架构

```
┌─────────────────────────┐
│   手机浏览器(鸿蒙 PWA)   │  添加到主屏幕,单手操作
│   static/ 单页应用        │  四标签:记录 / 日志 / 记忆 / 画像
└───────────┬─────────────┘
            │ HTTPS + Cookie 会话
┌───────────▼─────────────┐
│   Render 免费层(无状态)   │  FastAPI + uvicorn,Docker 运行
│   app/main.py  18 个接口  │  不存任何数据,随时可重启/重部署
│   ├─ auth.py  登录鉴权    │  HMAC 签名 Cookie,30 天有效
│   ├─ llm.py   智谱调用    │  System Prompt + 档案 + 近期摘要
│   └─ parser.py 五段解析   │  从原型移植,单测覆盖
└──────┬──────────┬───────┘
       │          │
┌──────▼─────┐ ┌──▼──────────────────┐
│ Supabase   │ │ 智谱 GLM API         │
│ PostgreSQL │ │ glm-4-flash         │
│ (新加坡,免费)│ │ open.bigmodel.cn    │
│ 持久数据    │ │ API Key 只存 Render  │
└────────────┘ └─────────────────────┘
```

**关键取舍:计算与存储分离。** Render 免费层没有持久磁盘,重启即清空——所以它只做无状态的请求处理;所有数据落在 Supabase PostgreSQL(免费 500MB,真正的持久层)。模型 API Key、登录密码、数据库连接串全部通过 Render 环境变量注入,代码仓库(公开 GitHub)里没有任何密钥。

## 三、单条日志的处理流水线

```
用户口述原文
   │
   ▼
① 组装提示词(llm.build_user_prompt)
   System Prompt(七节规范,原文内置)
   + 《个人核心记忆档案》当前六节内容
   + 最近 7 条日志摘要(日期 + 正文前 120 字)
   + 本次快捷指令(9 种模式可选)
   + 口述原文
   │
   ▼
② 调用智谱 glm-4-flash(120s 超时,失败重试 1 次)
   返回五段式结构:
   【1. 整理后的日志】【2. 今日内容提炼】
   【3. 观察与建议】【4. 长期记忆更新】【5. 个人画像变化】
   │
   ▼
③ 解析归档(parser.parse_result)
   第1段 → entries.polished(整理稿)
   第2段 → entries.summary(提炼)
   第3段 → entries.advice(建议列表)
   第4段「记忆更新包」→ pending_memories(待审核队列!)
   第5段 → entries.profile_delta + evidence(证据强度)
   原文 → entries.raw(永久保留,不被润色覆盖)
   │
   ▼
④ 人工审核(唯一需要用户参与的环节)
   记忆页:每条 AI 提议的记忆可改归类 / 确认并入 / 暂不收录
   确认后才写入 archive(画像档案)
```

## 四、记忆与画像机制(本系统的灵魂)

### 4.1 三层数据,各司其职

| 层 | 表 | 性质 |
|---|---|---|
| 原始层 | `entries.raw` | 永久保留原始口述,任何润色不覆盖 |
| 工作层 | `entries`(整理稿/提炼/建议/画像变化) | 每条日志的完整处理结果,可回看可删除 |
| 记忆层 | `pending_memories` + `archive` | 长期记忆:先审核,后入档 |

### 4.2 记忆审核(防止"一次观察变终身标签")

System Prompt 明确要求 AI 把记忆更新包标注「已确认事实」还是「待观察」,且**单次出现的观察不得写成稳定特征**。工程上落地为:

- AI 的记忆提议一律进 `pending_memories`(status=pending),绝不直接写档案;
- 「记忆」页角标提醒待审数量,每条可:改六节归类 → **确认并入**(写 archive)或**暂不收录**(status=discarded,历史保留可追溯);
- 确认时查重,避免重复条目堆积。

### 4.3 画像档案(archive 表,六节结构)

```
长期背景 / 当前目标 / 长期偏好 / 进行中的项目
/ 经多次观察得到的模式 / 近期变化
```

- 每条 = (节名, 一句话记忆),确认记忆时带证据强度(高/中/低);
- 整个档案每次处理日志时注入提示词,让建议越来越贴合;
- 画像页支持手动整段修订(`## 节名` 文本格式,服务端解析);
- 新旧矛盾不悄悄覆盖——删除重写由人执行,AI 只提议。

### 4.4 闭环

```
口述 → 处理 → 审核入档 → 档案注入下次提示词 → 更懂你的建议 → ……
```

用的次数越多,档案越厚,建议越准——这是"长期记忆"不丢的根本:它存在数据库,不在任何模型的上下文里。

## 五、数据模型(PostgreSQL)

```sql
entries           -- 日志:id, date, mode, raw, polished, summary,
                  --       advice(JSON), mempack(JSON), profile_delta,
                  --       evidence(高/中/低), llm_reply(原始回复,调试用), created_at
pending_memories  -- 待审核:id, text, cat(六节之一), src_entry(FK),
                  --          evi, status(pending/archived/discarded), created_at
archive           -- 画像档案:id, section(六节之一), text, updated
todos             -- 行动事项:id, text, due, done, src_entry, created_at
```

导出:`/api/export/md`(人读)、`/api/export/json`(机器/迁移);导入:兼容原型 localStorage JSON。

## 六、接口一览(全部 `/api` 前缀,登录后可用)

```
POST /api/login | /api/logout            会话(HMAC Cookie)
POST /api/process                         核心:调 GLM → 解析 → 归档
GET/DELETE /api/entries[/{id}]            日志列表/删除
GET /api/pending                          待审核队列
POST /api/pending/{id}/confirm|skip       确认并入 / 暂不收录
GET/POST/PATCH/DELETE /api/todos[...]     待办 CRUD
GET/PUT /api/profile                      档案读取/整段修订
GET  /api/export/md | /api/export/json    备份导出
POST /api/import                          备份/原型数据导入
```

## 七、安全模型

- **登录**:单用户密码(环境变量),HMAC-SHA256 签名的 HttpOnly Cookie,30 天;除 login 外全部接口要求会话;
- **密钥隔离**:智谱 Key、数据库密码、App 密码只在 Render 环境变量;GitHub 公开仓库零密钥;浏览器端永远接触不到 Key;
- **传输**:HTTPS(Render 默认提供 Let's Encrypt 证书);
- **数据主权**:随时导出 Markdown/JSON 自留副本;Supabase 数据库整库归你;
- **已知边界**:密码是明文配置于环境变量(单用户个人应用可接受);无速率限制(网址不公开即低风险);数据库连接走 Supabase IPv4 连接池(aws-0-ap-southeast-1)。

## 八、成本与运维

| 项 | 费用 |
|---|---|
| Render 免费层 | ¥0(15 分钟无访问休眠,冷启动 30–60 秒) |
| Supabase 免费层 | ¥0(500MB,当前数据量可用数年) |
| 智谱 glm-4-flash | 极低(新号有免费额度;单条日志约几千 token) |

改代码 → git push → Render 自动重新部署(数据不受影响)。

## 九、演进路线(按需启用)

1. **近期**:把冷启动优化掉(付费 $7/月 或 UptimeRobot 定时 ping);语音直接录音转文字(接智谱 GLM-ASR 或浏览器 Speech API);
2. **中期**:周报/月报定时生成(Render Cron Job);记忆去重与矛盾检测(向量相似度);多设备并发无冲突(已是天然支持);
3. **远期**:换更强模型(只改环境变量 GLM_MODEL);接入日历/待办生态;画像可视化(变化轨迹图)。

## 十、仓库结构

```
shiguang/
  app/
    main.py     FastAPI 路由(18 接口)
    db.py       PostgreSQL 连接(重试)、schema、行→dict
    auth.py     登录、HMAC Cookie
    llm.py      System Prompt、提示词组装、智谱调用
    parser.py   五段解析、记忆行拆节、档案文本解析
    export.py   Markdown/JSON 导出
    config.py   环境变量
  static/
    index.html / style.css / app.js    前端单页(四标签)
    manifest.webmanifest               添加到主屏幕
  Dockerfile      Render 构建用
  deploy.py       (备用)zx0094 服务器部署脚本
  ARCHITECTURE.md 本文档
```

## 十、v1.1 → v1.2 演进记录

### v1.1:长期记忆能力
- pgvector + 智谱 embedding-3(1024 维,HNSW),日志与确认记忆均向量化
- 语义检索 top-5 注入提示词(Context Builder:档案 + 活跃目标 + 最近 3 条 + 相关 5 条)
- events 表 + AI 事件提取;goals 表 + 停滞检测(daysIdle ≥ 7 提醒)
- 模型分层:glm-5.3 日常(thinking.level=low)/ GLM_MODEL_DEEP 深度模式
- 处理异步化:POST /api/process 立即返回 jobId,前端轮询

### v1.2:长期可靠性
- **JSON 结构化输出**:System Prompt 第三节改为 JSON 契约;parse_json_result 提取平衡
  JSON 并归一校验,失败回退旧六段解析。记忆带 category/confidence/note
- **jobs 状态机**:queued → running(embedding/generating/saving)→ done/failed,落库不怕重启
- **事务保护**:entry+待审记忆+事件先 commit,向量写入独立事务,失败不回滚核心数据
- **记忆溯源**:archive.source_entries[]/confidence/last_verified,确认时写入来源日志
- **记忆生命周期**:archive.status(active/deprecated),停用即退出提示词与向量索引,历史保留可恢复
- **Context Builder 预算**:注入仅取 active,每节限量(15/10/10/10/8/8)
- **混合检索**:cosine 粗召回 50 → 0.65*语义 + 0.20*exp(-days/180) + 0.15*来源权重 精排
- 保活:GitHub Actions 每 10 分钟 ping

### 待办(P2/P3)
- 每日自动备份到私有仓库;周报自动生成;prompt/model 版本记录;建议反馈;
  档案溯源 UI 跳转;reranker(数据 >500 条后)
