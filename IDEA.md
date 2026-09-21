# photo-agent IDEA

> Version: M3+ commercial roadmap + Photo Intelligence / Creative Search
> Date: 2026-09-21
> Positioning: China-market, local-first photo intelligence, AI creative selection, and potential future social/game layer

## 0. Long-term thesis

photo-agent should not ultimately become another generic photo manager.

The long-term opportunity is to build a **Photo Intelligence Engine** that understands photos, user intent, aesthetic preference and eventually generated creative content.

Core evolution:

```text
Photo Agent
  ↓
Photo Intelligence
  ↓
Personal Taste / Preference Engine
  ↓
Professional Aesthetic Model
  ↓
Creative Selection Engine
  ↓
AI Creative Search / "SSR 抽卡"
  ↓
AI Identity / Social / Game (optional long-term direction)
```

The core product principle remains:

> **用户告诉我这次想让别人看到怎样的自己，我负责从相册里找到、组织最合适的照片；如果真实素材不够，再帮助补齐。**

A second long-term principle is added:

> **生成模型负责创造海量候选，专业模型负责从海量候选中找到真正值得留下、继续生成或交给人类审核的作品。**

---

# 1. Strategic questions to validate

项目未来是否值得继续投入，核心不是模型参数，而是三个战略问题：

### 1.1 用户不足时如何继续

不能把商业模式建立在“未来一定有大量用户”上。

产品必须具备多条独立生存路径：

```text
Photo Agent
├─ 单人照片管理 / 筛选
├─ AI Photo Intelligence
├─ 专业审美 / 选片模型
├─ API / SDK
├─ AI 摄影助手
└─ 用户规模足够后再考虑 Social / Game
```

社交网络是上层期权，而不是项目早期的生命线。

### 1.2 数据如何形成模型资产

用户数量不是唯一指标。

真正重要的是：

- Preference Samples / Active User
- High-confidence Preference Samples
- Pairwise choices
- Model-user disagreement cases
- Hard cases
- 用户最终保留 / 删除 / 替换行为

### 1.3 专业模型的价值空间

单独卖“AI 审美评分”容易商品化；更大的价值在于：

> **在海量照片或 AI 生成候选中，自动找到最值得人看的、用的、保留的作品。**

它直接节省计算、时间和人工筛选成本。

---

# 2. M0-M2 基础能力

## M0 - 文件事实层

目标：知道用户有哪些照片。

- 扫描照片目录
- SQLite 索引
- 元数据
- 文件事实
- 缩略图
- 增量扫描
- exact duplicate

## M1 - 视觉关系层

目标：知道哪些照片可能属于同一组 / 相似照片。

- pHash
- dHash
- 近重复检测
- 相似照片候选
- 相似组
- threshold sweep
- contact sheet

原则：宁可候选池稍大，也不要过早漏掉真正相似照片。

## M2 - 照片理解与质量层

目标：知道照片是什么，以及照片本身质量。

- sharpness
- exposure
- noise
- quality_score
- 后续视觉语义接口

M2 的质量分目前以本地、可解释、低成本为原则。

---

# 3. M3 - Agent 层

目标：让 Qwen3.8-27B 通过安全 Tool 完成多步骤照片任务。

当前开发环境：

- llama.cpp
- Qwen3.8-27B-GSQ-RCO-IQ3_S-mtp
- RTX 4090 D
- 约 24.5 GB VRAM
- OpenAI-compatible HTTP API

不为了 Agent 额外安装 Ollama。

## M3.0

第一阶段优先 CLI / HTTP，不急着做复杂 Web UI。

示例：

```bash
photo-agent ask "帮我找出去年旅行中最好的照片"
photo-agent agent
```

第一批 Tools：

- search_photos
- get_photo
- get_metadata
- get_quality
- get_duplicate_groups
- get_similar_photos
- generate_report

M3.0 只读：

- search
- inspect
- analyze
- group
- score
- report

暂不允许 Agent 直接 delete / move / rename / overwrite。

未来删除优先：

```text
真实文件 → quarantine → 可恢复 → 用户确认 → 真正删除
```

---

# 4. M3.1-M3.5：从照片 Agent 到用户意图 Agent

## M3.1 - Taste Profile

长期保存的不是“用户人格”，而是：

> **用户喜欢别人看到怎样的自己。**

可学习：

- 自拍 / 人像比例
- 风景 / 朋友 / 美食 / 旅行比例
- 自然感 vs 精致感
- 随意感 vs 仪式感
- 明亮 vs 暗调
- 近景 vs 远景
- 是否喜欢露脸
- 是否接受 AI 生成
- 用户实际选择 / 删除 / 替换行为

初始画像可以来自少量主动输入；长期画像主要来自行为。

## M3.2 - Intent

核心入口：

> **“这次想发什么？”**

支持自然语言：

> “刚从杭州回来，想发个朋友圈，看起来这趟旅行挺丰富的，但不要太刻意。”

也提供快捷入口：

- 刚旅行回来
- 和朋友出去玩
- 约会
- 今天心情不错
- 生日
- 最近生活不错
- 想显得最近生活很丰富
- 随便帮我选

## M3.3 - Intent → Selection → Composition

```text
User Intent
 ↓
Photo Retrieval
 ↓
Photo Understanding
 ↓
Quality Filtering
 ↓
Similarity Reduction
 ↓
Taste Profile
 ↓
Candidate Ranking
 ↓
Composition
 ↓
Preview
```

## M3.4 - 多方案

默认可以给：

- A：自然记录
- B：氛围感
- C：精致感

用户选择直接成为偏好数据。

## M3.5 - AI 补齐

先证明 AI 懂真实照片，再补素材。

必须区分：

### A. 真实照片增强

- 调色
- 裁剪
- 扩图
- 小物体处理
- 背景处理

### B. AI 新生成

- 虚构场景
- 补充氛围图
- 新人物 / 新地点

B 必须明确标识为 AI 生成，不制造“用户真实到过某地”的假证据。

---

# 5. M4-M7：商业产品演化

## M4 - Social Content

输出不再只是照片：

- 九宫格
- 图片顺序
- 配文
- emoji
- 话题
- 小红书标题 / 正文
- 抖音图文
- 封面
- 长图 / 排版

目标：

> “帮我把这次经历发出去。”

## M5 - Mobile / WeChat / Sharing

优先：

```text
生成 → 保存 → 系统分享
```

自动发布不是 MVP 前置条件。

## M6 - Commercialization

- 用户账户
- 支付
- 点数 / 配额
- 任务队列
- 成本控制
- 数据分析
- 内容安全
- 运营后台

## M7 - Personal Taste Engine

形成长期用户偏好模型：

```text
用户历史照片
+
用户主动选择
+
用户修改行为
+
发布 / 互动反馈
 ↓
Personal Preference Model
```

---

# 6. 新增长期方向：Professional Aesthetic Model

## 6.1 不从零训练视觉大模型

个人开发者不应该与视觉基础模型预训练规模正面竞争。

推荐路线：

```text
成熟视觉 backbone / embedding
        ↓
少量高质量专业数据
        ↓
小型 scoring / ranking head
        ↓
专业审美模型
```

参考思路：

- NIMA：迁移学习 + 人类评分分布
- LAION-Aesthetics：成熟视觉 embedding + 轻量审美预测器

核心原则：

> **价值来自专业数据与任务定义，而不是模型参数规模。**

## 6.2 不把目标定义成“给照片打一个分”

更重要的是：

> **从一组照片中找到最值得留下的照片。**

因此优先研究 pairwise preference / ranking：

```text
A vs B
 ↓
用户选择 A
 ↓
A > B
```

这比要求用户回答“87 分还是 89 分”更自然。

## 6.3 审美向量

未来不要只有一个 aesthetic_score，可以拆成：

```text
Technical Quality
Composition
Subject
Lighting
Color
Emotion
Moment
Storytelling
Uniqueness
Context Relevance
```

最终再结合个人偏好得到 ranking。

## 6.4 数据分层

不要把所有用户偏好混成一个“真值”。

建议区分：

- Professional Preference
- General User Preference
- Context Preference
- Personal Preference

同一张照片可以对不同用户得到不同结果。

---

# 7. 数据飞轮：Qwen 27B 作为教师

4090D + Qwen3.8-27B 不只用于开发，也可以成为未来专业模型的数据教师。

```text
Qwen 27B / 强 VLM
        ↓
自动分析大量照片
        ↓
产生初始评分 + 理由 + pairwise judgment
        ↓
人工抽样纠错
        ↓
Preference Dataset
        ↓
小型专业模型
        ↓
Photo Agent 实际使用
        ↓
真实用户选择
        ↓
继续反哺模型
```

重点不是追求海量数据，而是追求高信息量数据。

## Active Learning

模型已经会区分“明显好 / 明显坏”后，继续训练这些样本收益降低。

应该主动收集：

```text
模型最不确定的样本
模型与用户意见冲突的样本
两张都很好的照片
不同维度互相冲突的照片
```

这些 Hard Cases 是个人开发者可以建立壁垒的地方。

---

# 8. 新增方向：AI Creative Selection / “SSR 抽卡”

这是 Professional Aesthetic Model 的重要扩展方向。

## 8.1 核心概念

生成模型负责创造，审美模型负责搜索：

```text
Prompt
 ↓
海量候选生成
 ↓
Cheap Filter
 ↓
Aesthetic Model
 ↓
VLM Critic
 ↓
高质量生成 / 渲染
 ↓
人工最终选择
```

例如：

```text
100,000 candidates
 ↓
10,000
 ↓
1,000
 ↓
100
 ↓
20
 ↓
5
 ↓
1 SSR
```

## 8.2 SSR 不等于“绝对艺术价值”

SSR 应定义为：

> **在特定任务、风格、Prompt、上下文和用途下，极少数最符合目标的作品。**

因此评价不能只有 aesthetic：

```text
Aesthetic
+
Prompt Alignment
+
Composition
+
Motion Quality
+
Temporal Consistency
+
Artifact Detection
+
Novelty
+
Context Fit
+
Usability
```

## 8.3 多级筛选

推荐：

```text
Level 1：廉价视觉模型
筛明显垃圾

Level 2：专业审美模型
筛审美与构图

Level 3：Qwen / VLM Critic
理解语义、故事、Prompt adherence

Level 4：高质量视频 / 图片生成
把计算资源集中在高潜力候选

Level 5：人工
最终选择
```

## 8.4 关键指标：SSR Hit Rate

定义：

```text
SSR Hit Rate = SSR 数量 / 总生成候选数量
```

例如：

```text
V0：0.12%
V1：0.31%
V2：0.87%
V3：1.73%
```

这个指标比“模型评分准确率”更接近真实商业价值，因为它直接衡量模型是否提高了优秀作品的命中率。

## 8.5 从随机抽卡到主动搜索

未来模型不仅筛选，还可以优化：

- Prompt
- seed
- sampler
- CFG
- style
- camera
- lighting
- motion
- duration
- model
- LoRA

目标：

> **主动寻找更容易产生 SSR 的生成空间。**

最终形成：

```text
Generate → Evaluate → Optimize → Generate → ...
```

这将 Professional Aesthetic Model 升级为 **Creative Search Engine**。

---

# 9. 新增长期方向：AI Identity + Social + Game

这是远期期权，不是当前主线。

## 9.1 AI Profile

用户授权照片库后，AI 自动选择少量“最能代表这个人”的照片。

目标不是简单 Top-N aesthetic，而是：

> **Maximum Identity Coverage：用最少照片最大程度表达一个人的特点。**

例如：

```text
最佳人像
旅行
兴趣爱好
生活状态
有故事的一张
```

## 9.2 AI Identity Card

可以形成：

- AI 角色卡
- 兴趣标签
- 生活方式标签
- 摄影风格
- 旅行 / 运动 / 艺术等维度

原则：描述用户，而不是给人的价值做绝对判断。

## 9.3 社交 / 游戏

可能的长期玩法：

- 今日照片擂台
- 摄影挑战
- AI 组队
- 旅行搭子
- 摄影搭档
- 兴趣匹配
- 双向 Match

避免把产品核心做成简单的“AI 给真人颜值 / 性感 / 价值打分排行榜”。

更有价值的方向是：

> **AI 帮用户发现自己的特点，并帮助用户找到可能感兴趣的人。**

## 9.4 社交冷启动原则

不要一开始直接做社交平台。

正确顺序：

```text
单人价值
 ↓
Photo Intelligence
 ↓
AI Profile
 ↓
少量用户互动
 ↓
验证 Social Discovery
 ↓
再扩大 Social / Game
```

如果社交起不来，产品仍然可以独立发展为 Photo Intelligence / AI Creative Selection。

---

# 10. 三条商业化出口

项目必须从一开始就保留转型路径。

## 路线 A：Photo Intelligence

消费者产品：

- 智能选片
- 相册整理
- AI 摄影助手
- AI 内容生成

## 路线 B：Professional / Creative Selection API

面向：

- 摄影软件
- 相册
- NAS
- 内容平台
- AI 创作工具
- 图片 / 视频生成平台

提供：

- Quality
- Aesthetic
- Ranking
- Preference
- Prompt Alignment
- Creative Selection

## 路线 C：Social / Game

只有当用户规模和互动行为达到临界点后才进入。

---

# 11. 用户规模与数据策略

## 0 用户

使用：

- 公开数据
- 自建小规模数据
- Qwen 自动标注
- 自己生成 AI 候选

目标：建立 V0。

## 100-1,000 用户

重点：

- pairwise choice
- 用户接受 / 拒绝
- 模型与用户冲突
- Hard Cases

## 1万-10万用户

增加：

- 场景分类
- 用户类型
- Context Preference
- 不同摄影目的

可能出现：

- Portrait Model
- Travel Model
- Family Model
- Landscape Model
- Social Model

## 100万+用户

如果真的形成规模，可以观察：

```text
AI 推荐
 ↓
用户选择
 ↓
展示
 ↓
别人互动
 ↓
收藏 / 喜欢 / 匹配
 ↓
长期行为
```

此时数据才真正形成强大的真实世界 preference signal。

但用户数量不是唯一目标，数据价值取决于高质量行为信号。

---

# 12. 市场空间判断框架

不要把三个市场混成一个。

## 12.1 Photo Management

市场大，但竞争激烈。

竞争对象包括：

- Apple Photos
- Google Photos
- Synology Photos
- Adobe Lightroom
- Immich
- 各类 NAS 相册

差异化必须来自 AI 理解和筛选，而不是基本相册功能。

## 12.2 AI Photo Curation / Photography Assistant

潜在价值更高：

- 3000 张照片选 100 张
- 旅行相册
- 最佳头像
- 社交平台照片
- 摄影师挑片
- AI 内容组合

## 12.3 AI Creative Selection

潜在价值来自：

> **从海量生成结果中找到少量高价值结果。**

如果能够提高优秀作品命中率，就可以直接节省 GPU、时间和人工成本。

## 12.4 AI Social

潜在市场最大，但冷启动风险最高。

因此 Social 是上限，不应成为早期项目唯一生命线。

---

# 13. 产品核心护城河

容易复制：

- pHash
- 九宫格
- 基础 VLM
- 通用大模型
- 普通图片生成
- 简单评分

真正应该积累：

```text
真实照片
+
用户选择
+
用户长期偏好
+
Pairwise Preference
+
Hard Cases
+
发布 / 互动结果
+
AI 生成候选
+
最终人类选择
```

最终形成：

> **Preference Dataset + Photo Intelligence + Creative Search Engine**

核心竞争力不是“我有一个 3B 模型”，而是：

> **我有别人没有的高质量偏好数据，以及把这些数据转化为更高 SSR 命中率的能力。**

---

# 14. 技术架构演化

## 当前

```text
Windows
 ├─ photo-agent
 ├─ SQLite
 ├─ local photos
 └─ llama.cpp
      └─ Qwen3.8-27B
```

## M3

```text
photo-agent
 ├─ Scanner
 ├─ DB
 ├─ M1
 ├─ M2
 ├─ Tools
 └─ Agent
       ↓
   llama.cpp
```

## Professional Aesthetic / Creative Selection

```text
Image / Video
      ↓
Frozen Vision Encoder
      ↓
Feature / Embedding
      ├─ Quality Head
      ├─ Aesthetic Head
      ├─ Ranking Head
      ├─ Context Head
      └─ Preference Head
             ↓
       Creative Ranking
```

## 商业 MVP

```text
Mobile / Web
      ↓
API Gateway
      ↓
Task Service
      ↓
Agent Orchestrator
      ├── Photo Search
      ├── User Profile
      ├── Quality
      ├── Similarity
      ├── Vision Model
      ├── Aesthetic Model
      ├── Ranking
      └── Generation / Creative Search
             ↓
       Object Storage
```

---

# 15. 云端与本地职责

## 本地

- 原始照片
- 本地索引
- EXIF
- pHash / dHash
- 大规模扫描
- 缩略图
- 私密数据
- 高隐私模式

## 云端

- 用户账户
- Agent session
- 支付
- 任务队列
- AI API
- 生成任务
- 内容发布
- 统计
- 风控

原则：

> **不要把所有原图默认永久上传云端。**

优先：

```text
本地索引
 ↓
候选集
 ↓
只上传任务需要的低 / 中分辨率图片
```

---

# 16. 成本与硬件原则

当前 RTX 4090D 足够：

- M0-M3
- VLM 调试
- Agent
- Tool Calling
- 本地批量实验
- 小规模审美模型实验

不提前购买 GPU 集群。

商业早期优先：

```text
API
vs
云 GPU
vs
本地混合
```

先根据真实负载决定。

图像 / 视频生成尤其应该先通过 API 做产品验证，不要提前自建昂贵生成基础设施。

---

# 17. 商业模式

早期可以测试：

## Free

- 基础照片分析
- 少量 AI 选图
- 少量模板

## Credits / Points

- AI 精修
- AI 补图
- AI 视频 / 高级生成
- Creative Search

原则：生成是高成本能力，应与付费额度绑定。

不要在没有真实支付数据之前拍脑袋确定最终价格。

---

# 18. 核心验证指标

不要早期追求：

- 模型参数
- GPU 利用率
- 总照片数量
- AI 调用量

重点看：

### Photo Agent

- Intent → Result 成功率
- 首次结果接受率
- 修改率
- 重做率
- 7 日 / 30 日复用率

### Preference Model

- Pairwise accuracy
- User acceptance rate
- Model-user disagreement rate
- Hard-case resolution rate

### Creative Search

- SSR Hit Rate
- Top-K human acceptance
- GPU / API cost per accepted result
- Search compute saved
- Prompt optimization gain

### 商业化

- AI 补图付费率
- Credit conversion
- 用户留存
- 每活跃用户收入
- 单任务毛利

---

# 19. 当前最大风险

## 风险 1：用户规模不足

解决：

> 产品必须在没有大规模社交网络时也有独立价值。

## 风险 2：审美数据不足

解决：

> 公开数据 + Qwen 教师 + 少量人工 + Active Learning + 用户行为。

## 风险 3：专业模型被通用 VLM 商品化

解决：

> 不卖简单评分，重点做 Ranking / Preference / Context / Creative Selection。

## 风险 4：AI 生成结果质量不稳定

解决：

> 多级筛选 + 自动抽卡 + Hard Case 训练 + SSR Hit Rate。

## 风险 5：社交冷启动

解决：

> Social 后置；先建立单人 Photo Intelligence 价值。

## 风险 6：隐私 / 数据授权 / 合规

解决：

> 本地优先、明确授权、原图最小上传、AI 生成明确标识、商业化前单独做合规项目。

---

# 20. 最终路线图

```text
M0
文件事实层
 ↓
M1
视觉关系层
 ↓
M2
质量 / 基础视觉理解
 ↓
M3.0
Agent + Tools
 ↓
M3.1
Taste Profile
 ↓
M3.2
Intent Understanding
“这次想发什么？”
 ↓
M3.3
Intent → Selection
 ↓
M3.4
Composition / 多方案
 ↓
M3.5
AI Edit / AI Fill
 ↓
M4
Social Content
 ↓
M5
Mobile / WeChat / Sharing
 ↓
M6
商业化
 ↓
M7
Personal Taste Engine
 ↓
长期并行方向 A
Professional Aesthetic Model
 ↓
Preference / Ranking
 ↓
Creative Selection Engine
 ↓
AI Creative Search
 ↓
SSR 抽卡
 ↓
长期并行方向 B
AI Identity
 ↓
Social / Game
```

---

# 21. 当前明确决策

截至 2026-09-21：

### 当前主线

- M0-M2 继续完善
- M3 Agent + Tools
- 本地 llama.cpp + Qwen3.8-27B
- 只读优先
- 自然语言照片任务
- “这次想发什么？”作为未来核心交互

### 中期重点

- Preference 数据结构预留
- Pairwise ranking 数据
- Taste Profile
- Professional Aesthetic Model 的实验性研究
- Qwen 27B 作为教师模型
- Active Learning / Hard Cases

### 长期方向

- Creative Selection
- AI 生成海量候选自动筛选
- SSR Hit Rate
- Prompt / 参数主动搜索
- AI Identity
- Social / Game

### 暂缓

- 自建 GPU 集群
- 复杂社交网络
- 大规模云端原图存储
- 全平台自动发布
- 广告系统
- 一开始就做完整 App
- 从零预训练视觉基础模型

---

# 22. 下一步

当前仍然只做 M3.0：

```text
Agent
+
Tools
+
多轮 Tool Calling
+
安全限制
```

用真实照片验证自然语言任务。

同时，在数据库 / 数据结构层为未来留下：

```text
preference_samples
- candidate_a
- candidate_b
- winner
- context
- source
- confidence
- reason
- user_id
- timestamp
```

以及未来 Creative Search 所需：

```text
creative_candidates
- prompt
- seed
- model
- parameters
- aesthetic_score
- alignment_score
- artifact_score
- human_selection
- rarity / tier
```

**现在不训练大模型，不做社交 App，不做大规模生成平台。先把 Photo Agent 做成真正好用的单人产品，并让数据结构为未来的 Preference Engine 和 Creative Search 留出空间。**

---

# 23. 最终产品定义

photo-agent 最终不是：

> 一个更好的相册。

也不是：

> 一个 AI 九宫格工具。

也不是：

> 一个简单的 AI 审美打分器。

长期愿景是：

> **一个理解用户视觉世界、能够从真实照片和 AI 生成海量候选中寻找最有价值内容的个人视觉智能系统。**

用户可以说：

> **“这次我想让别人看到这样的我。”**

也可以说：

> **“帮我从这一万张 AI 生成结果里找出真正值得留下的那几个。”**

前者是 Photo Agent；后者是 Creative Search。两者共享同一个核心能力：

> **理解什么值得被看见。**
