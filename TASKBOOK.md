# AutoLearn 前端兼容型网课模块与 fuckZHS 融合任务书

| 项目 | 内容 |
| --- | --- |
| 文档版本 | v0.2 |
| 文档状态 | 已实施 |
| 编写日期 | 2026-10-23 |
| 目标项目 | AutoLearn |
| 网课源项目 | `D:\Ddesktop\AIandCODE\code\fuckZHS` |
| 核心原则 | 优先让后端适配现有前端；只有继续适配会扭曲 ZHS 业务语义时，才修改前端 |
| **代码来源原则** | **① fuckZHS 能复用的部分直接复用（原样调用，不改不包）；② AutoLearn 已有的代码复用后修改（改那块，不复制）；③ 只有前两级都不成立才新写，且必须写明理由。执行细则见 `docs/TASKS.md` §1.1** |
| 主要目标 | 在尽量不修改 `fuckZHS` 核心逻辑的前提下，增加与其平级的网课模块 |

---

## 1. 修订说明

本版本替换上一版偏理想化的任务中心方案。

上一版方案引入了完整的 `workspace` 聚合层、网站注册中心、独立状态机、SQLite 和多套前端组件，实施成本偏高。

本版本调整为：

```text
复用现有前端结构与轮询逻辑
→ 网课后端输出与现有任务接口兼容的 DTO
→ 新增少量任务类型和课程详情分支
→ fuckZHS 整体嵌入子进程
→ 不重写 fucker.py 的核心业务
```

优先级如下：

1. 不破坏 `fuckZHS` 的业务逻辑；
2. 尽量不改现有做题流程；
3. 尽量复用现有任务列表、详情轮询、事件卡片和模型档案；
4. 必须新增的前端改动仅限任务类型、网站选择、课程表单和课程详情展示。

---

## 2. 当前前端逻辑分析

### 2.1 页面结构

当前 WebUI 是单页任务工作区：

```text
index.html
├── 顶栏
│   ├── 关闭服务
│   ├── 模型与设置
│   └── 新建任务
├── 左侧任务栏
│   ├── 搜索
│   ├── 站点筛选
│   ├── 状态筛选
│   ├── 队列摘要
│   └── 任务列表
└── 右侧任务详情
```

当前“新建任务”弹窗固定包含：

```text
任务名称
站点
作业网址
模型档案
```

当前任务详情假定任务属于做题流程，包含：

```text
登录验证
抓取题目
检查题包
模型授权
AI 解题
复核答案
提交确认
```

### 2.2 前端启动与轮询

启动顺序：

```text
GET /api/meta
→ GET /api/overview
→ GET /api/profiles
→ GET /api/tasks/{task_id}
→ GET /api/tasks/{task_id}/runtime-events
```

之后每 1600 毫秒刷新一次任务概览和当前任务详情。

### 2.3 前端依赖的任务摘要字段

任务列表和任务卡片至少依赖：

```text
task_id
site_id
title
status
current_step
created_at
updated_at
last_safe_error_category
last_operation
interrupted_operation
pending_operation
interaction_retry_authorized
queue_state
queue_position
```

因此网课任务必须转换成相同字段结构，现有任务列表才能直接复用。

### 2.4 前端依赖的任务详情结构

详情接口使用：

```json
{
  "task": {},
  "cookie_status": {
    "captured": true,
    "configured": true
  },
  "queue": null,
  "events": []
}
```

网课详情可以增加 `course` 字段，但不能删除现有字段。

### 2.5 前端依赖的运行时事件

当前前端读取：

```json
{
  "events": [
    {
      "sequence": 1,
      "task_id": "...",
      "run_id": "...",
      "stage": "solve",
      "status": "completed",
      "completed_count": 1,
      "asset_count": 0,
      "safe_error_category": null
    }
  ],
  "latest_sequence": 1
}
```

网课模块应复用该接口形态，避免修改轮询逻辑。

---

## 3. 前端与 ZHS 的冲突及处理策略

| 冲突点 | 当前前端 | fuckZHS 现状 | 处理策略 |
| --- | --- | --- | --- |
| 任务类型 | 没有任务类型字段 | 只有网课 | 任务摘要增加 `task_type` |
| 新建任务 | 必须有作业网址 | 网课不使用作业网址 | 前端根据任务类型切换字段 |
| 网站选择 | 固定做题站点列表 | 智慧树是网课站点 | `/api/meta` 为站点增加 `task_types` |
| 状态机 | 做题专用状态较多 | 网课有学习、考试、部分完成 | 前端增加课程状态显示，不强行映射成“解题中” |
| 步骤条 | 做题七步流程 | 登录、课程、学习、考试、结果 | 前端增加课程专用步骤条 |
| 登录 | 任务级 Cookie 抓取 | 账号级智慧树 Cookie | 复用 Cookie Manager，但按当前任务触发 |
| 进度 | 目前只展示解题阶段 | 视频、资源、知识点、考试进度 | 统一写入运行时事件，并增加课程进度卡片 |
| 停止 | 安全停止做题操作 | 停止课程执行进程 | 课程详情使用“停止学习”文案 |
| 并发 | 做题队列可排队 | 同一智慧树账号不宜并发 | 前端只允许一个课程任务处于运行状态 |
| 输出 | JSON 接口 | 终端打印 | 子进程桥接层转换为 JSONL |
| 结果 | 做题有明确确认状态 | 部分视频或考试可能部分完成 | 前端支持 `partial` 状态 |

处理原则：

> 不把网课强行改成做题状态机；由网课模块输出自己的状态值，前端增加对应标签和课程详情分支。

---

## 4. 总体设计

```text
现有 WebUI
├── 任务列表
│   ├── 做题任务
│   └── 网课任务
├── 新建任务
│   ├── 做题
│   └── 网课
├── 做题详情
│   └── 复用现有流程
└── 网课详情
    ├── 登录
    ├── 课程信息
    ├── 学习进度
    ├── 考试进度
    └── 结果

后端
├── 现有做题模块
│   ├── TaskStore
│   ├── TaskRuntimeController
│   └── /api/tasks 的做题部分
│
└── 新增网课模块
    ├── CourseJobStore
    ├── CourseRuntimeController
    ├── 前端兼容 DTO
    └── fuckZHS 子进程桥接
```

### 4.1 网课任务与做题任务平级

- 做题任务继续使用现有 `TaskStore` 和运行时。
- 网课任务使用独立 `CourseJobStore` 和网课运行时。
- 两者不共享状态机和运行数据表。
- 对外读取时统一转换为前端兼容 DTO。

### 4.2 统一读取、独立执行

统一的是：

- 任务摘要字段；
- 任务详情外壳；
- 事件外壳；
- 状态和步骤显示接口。

不统一的是：

- 数据存储；
- 任务状态机；
- 执行器和执行流程；
- 进度计算方式；
- 登录后的抓取和填写流程。

---

## 5. 用户操作流程

### 5.1 新建做题任务

```text
点击“新建任务”
→ 任务类型选择“做题”
→ 选择做题网站
→ 填写任务名称、作业网址、模型档案
→ 创建任务
→ 进入现有做题详情流程
```

### 5.2 新建网课任务

```text
点击“新建任务”
→ 任务类型选择“网课”
→ 选择网站“智慧树”
→ 选择课程类型：共享课 / 校内课 / AI 课程
→ 填写课程网址、课程 ID、班级 ID
→ 设置播放速度、完成阈值、运行时限、考试处理
→ AI 课程选择模型档案
→ 创建任务
→ 进入网课详情
→ 点击“登录智慧树”
→ 点击“开始学习”
```

### 5.3 网课任务详情

详情页展示：

```text
任务名称
任务类型
网站
状态
课程类型
课程名称或课程 ID
当前阶段
当前视频或知识点
已完成数量 / 总数量
百分比
最近事件
```

操作按钮：

```text
登录智慧树
更新登录
开始学习
停止学习
重新运行
归档任务
删除任务
```

---

## 6. 前端需要修改的范围

前端只做必要改动，不重写现有做题界面。

### 6.1 `index.html`

新建任务弹窗增加：

- 任务类型选择；
- 动态站点选择；
- 做题配置区域；
- 网课配置区域。

建议结构：

```text
任务类型
├── 做题
└── 网课

网站
├── 根据任务类型动态填充

任务名称

做题配置
├── 作业网址
└── 模型档案

网课配置
├── 课程类型
├── 课程网址或课程 ID
├── 班级 ID
├── 播放速度
├── 完成阈值
├── 运行时限
└── 是否处理考试
```

页面只保留一个“新建任务”入口。

### 6.2 任务筛选

增加“任务类型”筛选：

```text
全部类型
做题
网课
```

站点筛选继续使用现有 `siteFilter`，但站点列表来自 `/api/meta`。

### 6.3 任务卡片

任务卡片增加任务类型标签：

```text
[做题] 超星学习通
[网课] 智慧树
```

任务卡片继续使用现有：

- 标题；
- 状态；
- 站点；
- 更新时间；
- 操作菜单。

### 6.4 状态和步骤显示

增加课程状态：

```text
ready            待开始
running          学习中
stopping         正在停止
completed        已完成
partial          部分完成
cancelled        已取消
failed           失败
archived         已归档
```

复用现有状态：

```text
draft            草稿
authenticating   登录验证中
queued           等待执行
```

课程步骤：

```text
authentication   登录
catalog          课程信息
learning         学习
exam             考试
result           结果
```

网课任务不使用做题的题包、模型授权、答案复核和提交确认步骤。

### 6.5 任务详情分支

`renderDetail()` 在读取任务类型后分流：

```text
task_type === "homework"
→ 现有 renderDetail 流程

task_type === "course"
→ renderCourseDetail
```

课程详情组件负责：

- 课程步骤条；
- 课程信息；
- 当前项目；
- 学习进度；
- 事件记录；
- 登录、开始、停止、重新运行。

### 6.6 前端函数调整清单

| 文件 | 函数 | 调整 |
| --- | --- | --- |
| `app.js` | `STATUS_LABELS` | 增加课程状态 |
| `app.js` | `STEP_LABELS` | 增加课程阶段 |
| `app.js` | `state` | 增加任务类型、课程列表和当前任务类型 |
| `app.js` | `renderMeta` | 读取站点支持的任务类型 |
| `app.js` | `renderTaskList` | 增加类型标签和类型筛选 |
| `app.js` | `refreshOverview` | 使用后端返回的混合任务列表 |
| `app.js` | `refreshDetail` | 根据任务类型设置详情和轮询上下文 |
| `app.js` | `renderDetail` | 做题/网课详情分流 |
| `app.js` | `isTaskBusy` | 加入课程 `running` 和 `stopping` |
| `app.js` | `startAuthentication` | 网课使用同一接口或课程认证接口 |
| `app.js` | `stopTask` | 网课显示“停止学习” |
| `app.js` | `archiveTask` | 网课使用课程归档接口 |
| `app.js` | `openDeleteTaskDialog` | 网课文案改为“运行记录” |
| `app.js` | `openNewTask` | 初始化任务类型和动态站点 |
| `app.js` | `submitNewTask` | 按任务类型构造不同请求 |
| `app.js` | `bindEvents` | 任务类型和站点联动 |
| `styles.css` | 任务与进度样式 | 增加课程状态和课程进度卡片样式 |

### 6.7 不修改的前端部分

- 现有题目列表和答案编辑器；
- 现有模型授权流程；
- 现有提交确认流程；
- 现有 Cookie 弹窗主体；
- 现有事件卡片；
- 现有任务搜索、归档和删除基础交互。

---

## 7. 后端兼容层设计

### 7.1 目标目录

```text
AutoLearn/
├── core/
│   ├── webui/
│   │   ├── app.py                    # 扩展 meta、overview 和任务分发
│   │   └── static/
│   │       ├── index.html
│   │       ├── app.js
│   │       └── styles.css
│   │
│   ├── auth/
│   │   ├── cookie_manager.py         # 支持课程认证提供器
│   │   └── provider_registry.py      # 可选的新增注册表
│   │
│   ├── tasks/                        # 现有做题模块，不改状态机
│   ├── adapters/
│   ├── solver/
│   ├── interaction/
│   │
│   └── courses/
│       ├── __init__.py
│       ├── domain.py                 # CourseJob、CourseStatus、CoursePhase
│       ├── store.py                  # JSON 任务状态和运行目录
│       ├── runtime.py                # 子进程生命周期和事件消费
│       ├── service.py                # 创建、认证、启动、停止、归档
│       ├── dto.py                    # 转换为前端任务 DTO
│       ├── router.py                 # 课程专用接口
│       ├── auth.py                   # 智慧树 Cookie 认证适配
│       └── providers/
│           └── zhihuishu/
│               ├── bridge.py         # 子进程入口
│               ├── progress.py       # 事件与进度适配
│               └── vendor/
│                   └── fuckZHS/
│                       ├── fucker.py
│                       ├── ObjDict.py
│                       ├── zd_utils.py
│                       ├── sign.py
│                       ├── utils.py
│                       ├── logger.py
│                       ├── push.py
│                       ├── LICENSE
│                       └── UPSTREAM.md
```

### 7.2 网课任务存储

第一版为降低工作量，使用 JSON 文件而不是新增复杂 SQLite 表：

```text
%LOCALAPPDATA%\AutoLearn\courses\
├── index.json
├── runtime.json
└── jobs\
    └── <job_id>\
        ├── job.json
        ├── events.jsonl
        ├── runtime-events.jsonl
        └── run\
```

要求：

- `job.json` 原子写入；
- `index.json` 保存任务摘要；
- 运行时事件追加到 `runtime-events.jsonl`；
- 单个智慧树账号只允许一个课程任务处于 `running`；
- 后续任务量大时再迁移到 SQLite。

### 7.3 网课任务模型

```text
job_id
task_type = "course"
site_id = "zhihuishu"
title
mode
course_id
class_id
status
phase
current_title
progress_percent
completed_count
total_count
failed_count
run_id
options
created_at
updated_at
last_safe_error_category
```

任务 ID 建议使用 `course-<uuid>`，避免与现有做题任务 ID 混淆。

### 7.4 网课状态

```text
draft
authenticating
ready
queued
running
stopping
completed
partial
failed
cancelled
archived
```

### 7.5 网课阶段

```text
setup
authentication
catalog
learning
exam
result
```

---

## 8. 前端兼容 DTO

### 8.1 任务摘要

网课任务必须转换为：

```json
{
  "task_id": "course-...",
  "task_type": "course",
  "site_id": "zhihuishu",
  "title": "高等数学网课",
  "authentication_hint": "请在浏览器中完成智慧树登录并停留在课程页面。",
  "model_profile_id": null,
  "run_id": "...",
  "current_step": "learning",
  "status": "running",
  "created_at": "...",
  "updated_at": "...",
  "last_safe_error_category": null,
  "last_operation": "learn",
  "interrupted_operation": null,
  "pending_operation": null,
  "interaction_retry_authorized": false,
  "queue_state": null,
  "queue_position": null
}
```

现有做题摘要增加：

```json
{
  "task_type": "homework"
}
```

### 8.2 任务详情

```json
{
  "task": {},
  "cookie_status": {
    "captured": true,
    "configured": true
  },
  "queue": null,
  "events": [],
  "course": {
    "mode": "shared",
    "course_id": "...",
    "class_id": null,
    "course_name": "...",
    "phase": "learning",
    "current_title": "第 2 章 / 第 3 节 / 视频 4",
    "progress_percent": 46,
    "completed_count": 8,
    "total_count": 17,
    "failed_count": 0
  }
}
```

### 8.3 运行时事件

```json
{
  "events": [
    {
      "sequence": 12,
      "task_id": "course-...",
      "run_id": "...",
      "stage": "learning",
      "status": "progress",
      "completed_count": 8,
      "asset_count": null,
      "safe_error_category": null
    }
  ],
  "latest_sequence": 12
}
```

### 8.4 事件记录

```json
{
  "event_id": 1,
  "task_id": "course-...",
  "occurred_at": "...",
  "event_type": "course_progress",
  "from_status": "running",
  "to_status": "running",
  "operation": "learn",
  "safe_error_category": null
}
```

事件卡片继续复用现有 `renderEventCard()`。

---

## 9. 后端接口

### 9.1 `/api/meta`

扩展返回任务类型和站点支持情况：

```json
{
  "task_types": [
    {"task_type_id": "homework", "label": "做题"},
    {"task_type_id": "course", "label": "网课"}
  ],
  "adapters": [
    {"site_id": "chaoxing", "label": "超星学习通", "task_types": ["homework"]},
    {"site_id": "cjyuketang", "label": "长江雨课堂", "task_types": ["homework"]},
    {"site_id": "zhihuishu", "label": "智慧树", "task_types": ["course"]}
  ],
  "port": 8877
}
```

### 9.2 `/api/overview`

返回混合任务列表：

```text
现有做题任务摘要 + 网课任务摘要
```

前端不需要分别请求两个任务列表。

`queue` 字段保持现有做题队列结构；网课任务通过 `status=running` 表示正在运行。

### 9.3 `POST /api/tasks`

扩展现有创建接口：

```json
{
  "task_type": "course",
  "site_id": "zhihuishu",
  "title": "高等数学网课",
  "course": {
    "mode": "shared",
    "course_id": "...",
    "class_id": null,
    "speed": 1.5,
    "threshold": 0.91,
    "time_limit_minutes": 0,
    "process_exam": true,
    "model_profile_id": null
  }
}
```

规则：

- 未提供 `task_type` 时默认为 `homework`；
- `homework` 请求继续使用原有字段；
- `course` 请求不要求 `page_url`；
- 后端根据 `task_type` 分发到对应服务。

### 9.4 任务详情和通用操作

```text
GET    /api/tasks/{task_id}
GET    /api/tasks/{task_id}/runtime-events
POST   /api/tasks/{task_id}/authenticate
POST   /api/tasks/{task_id}/stop
POST   /api/tasks/{task_id}/archive
DELETE /api/tasks/{task_id}
```

这些接口根据任务 ID 前缀或任务类型分发：

```text
homework → 现有做题模块
course-* → 网课模块
```

这样前端大部分现有调用路径无需修改。

### 9.5 课程专用接口

```text
POST /api/tasks/{task_id}/course/start
GET  /api/courses/catalog
POST /api/courses/catalog/refresh
PUT  /api/tasks/{task_id}/course-settings
```

第一版可以只实现手工课程 ID 创建，课程列表接口作为增强项。

---

## 10. fuckZHS 适配方式

### 10.1 不修改核心业务

第一版不修改：

- `fucker.py` 的请求逻辑；
- 视频进度提交；
- 弹题处理；
- 校内课资源遍历；
- AI 课程 API；
- `ExamCtx`；
- `zd_utils.py`；
- `sign.py`；
- `main.py`。

`main.py` 不进入 AutoLearn 调用链。只使用 `fucker.Fucker`。

### 10.2 整体嵌入

将运行所需文件整体复制到：

```text
core/courses/providers/zhihuishu/vendor/fuckZHS/
```

不拆分 `fucker.py`，不重写导入关系。

子进程启动时才将 vendor 路径加入 `sys.path`：

```python
sys.path.insert(0, vendor_dir)
from fucker import Fucker
```

这样不会污染 FastAPI 主进程命名空间。

### 10.3 子进程运行

每个网课任务通过子进程启动：

```text
python bridge.py
```

要求：

- 任务参数从 stdin 读取；
- stdout 只输出 JSONL 事件；
- 上游 `print` 重定向到 stderr；
- 停止时终止子进程；
- 结束后由父进程更新 `job.json`；
- 不使用 `main.py` 的全局配置和 `exit()`。

### 10.4 不修改业务代码的进度适配

`fucker.py` 中进度条函数是模块级变量，可以在 bridge 中替换：

```python
import fucker as zhs

def emit_progress(iteration, total, prefix="", suffix="", progressbar_view=True):
    emit({
        "type": "progress",
        "stage": "learning",
        "current": iteration,
        "total": total,
        "message": prefix,
    })

zhs.progressBar = emit_progress
```

同时设置：

```text
tree_view=False
progressbar_view=True
```

这样进度仍然由 ZHS 原始算法产生，但输出转成前端事件。

### 10.5 运行目录隔离

为了避免修改上游路径代码，bridge 每次启动时可以将所需 vendor 文件复制到：

```text
<course run dir>/upstream/
```

然后从该目录导入 `fucker`。

这样上游的：

```text
logs/
aiexamAnswer/
临时缓存
```

都会落在本次网课运行目录中，不写入源码目录。

如果后续希望避免重复复制，可以改为在 `utils.getDir()` 中增加环境变量支持，但这属于可选优化。

### 10.6 Cookie 注入

使用 AutoLearn Cookie Manager 获取智慧树 Cookie 后，由 bridge 执行：

```python
zhs = Fucker(
    cookies=cookies,
    speed=speed,
    end_thre=threshold,
    limit=time_limit_minutes,
    tree_view=False,
    progressbar_view=True,
)
```

不要调用 `Fucker.login()`。

要求：

- Cookie 必须包含 `CASLOGC`；
- Cookie 无效时返回 `auth_required`；
- Cookie 只通过 stdin 传入；
- 不写入任务摘要、事件或日志。

### 10.7 显式课程类型

不使用 `fuckCourse()` 的正则自动判断，bridge 根据前端提交的 `mode` 直接调用：

```text
shared → fuckZhidaoCourse(course_id)
hike   → fuckHikeCourse(course_id)
ai     → fuckAiCourse(course_id, class_id, ai_config)
```

这避免 `course_id` 格式变化导致课程被错误分发。

### 10.8 AI 配置

第一版复用上游 AI 路径，将 AutoLearn 模型档案映射为：

```python
ai_config = {
    "enabled": True,
    "use_zhidao_ai": False,
    "openai": {
        "api_base": profile["base_url"],
        "api_key": profile["api_key"],
        "model_name": profile["model"],
    },
    "ppt_processing": {
        "provide_to_ai": False,
        "moonShot": {
            "base_url": "",
            "api_key": "",
        },
    },
    "use_stream": True,
}
```

限制：

- 第一版只支持 OpenAI 兼容模型档案；
- 不支持的模型类型在创建或开始任务前拒绝；
- 后续再将上游 `Openai` 类替换为共享模型客户端。

---

## 11. 子进程事件协议

### 11.1 阶段

```json
{"type":"phase","stage":"authentication","status":"started"}
{"type":"phase","stage":"catalog","status":"started"}
{"type":"phase","stage":"learning","status":"started"}
{"type":"phase","stage":"exam","status":"started"}
{"type":"phase","stage":"result","status":"started"}
```

### 11.2 当前项目

```json
{
  "type": "current",
  "stage": "learning",
  "title": "第 2 章 / 第 3 节 / 视频 4"
}
```

### 11.3 进度

```json
{
  "type": "progress",
  "stage": "learning",
  "current": 120,
  "total": 600,
  "percent": 20,
  "completed_count": 3,
  "total_count": 12
}
```

### 11.4 结果

```json
{
  "type": "result",
  "status": "completed",
  "completed_count": 12,
  "failed_count": 0
}
```

父进程必须：

- 校验事件结构；
- 限制事件大小；
- 只保存白名单字段；
- 对事件做节流，避免每秒事件全部写入长期记录；
- 只保留最近一段事件供前端查看。

---

## 12. 前端课程详情设计

### 12.1 课程步骤条

```text
登录 → 课程信息 → 学习 → 考试 → 结果
```

不同课程类型可以跳过“考试”。

### 12.2 课程信息卡

显示：

```text
课程类型
课程 ID
班级 ID
课程名称
播放速度
完成阈值
运行时限
```

### 12.3 学习进度卡

显示：

```text
当前项目
已完成 / 总数
百分比
失败数量
```

有具体百分比时使用现有 `.progress-track` 样式；没有具体百分比时使用不确定进度条。

### 12.4 操作按钮

```text
未登录：登录智慧树
已登录、未开始：开始学习
运行中：停止学习
失败或部分完成：重新运行
完成：刷新状态
```

### 12.5 事件记录

直接复用现有 `renderEventCard()`，因为网课详情也提供同结构 `events`。

---

## 13. 安全要求

1. 不向前端返回 Cookie 或 API Key。
2. 不在任务摘要、详情和事件中返回完整敏感 URL。
3. 网课子进程 stderr 不直接持久化为前端事件。
4. 上游日志默认至少为 `WARNING`。
5. 网课运行数据必须写入用户数据目录。
6. 删除课程任务时清理对应运行目录。
7. 停止任务后确认子进程已退出。
8. 只允许 Loopback、正确 Host 和正确 Origin 的写请求。
9. 模型档案继续由后端读取，前端只使用档案 ID。
10. 同一智慧树账号默认只允许一个课程任务处于运行状态。

---

## 14. 非功能要求

### 14.1 兼容性

- 保持 Windows 用户会话；
- 保持 Python 3.12 x64；
- 保持现有 FastAPI、Uvicorn 和 Scrapling 依赖；
- 做题任务和 `/api/tasks` 现有行为保持兼容；
- `fuckZHS` 的 Python 3.10+ 代码可直接运行在 3.12。

### 14.2 性能

- 网课任务不得阻塞 FastAPI 主线程；
- 任务概览读取不得扫描大型运行文件；
- 每次 1.6 秒轮询只读取任务状态和最近事件；
- 子进程 stdout 必须实时消费，避免管道阻塞；
- 事件写入需要节流并限制历史长度。

### 14.3 可维护性

- 不直接修改 `fucker.py` 业务逻辑；
- 所有上游代码集中在 vendor 目录；
- 上游版本记录在 `UPSTREAM.md`；
- 前端兼容 DTO 单独放在 `dto.py`；
- 后续增加网站时可以新增课程供应器而不修改做题模块。

---

## 15. 验收标准

### 15.1 新建任务

- [ ] 点击“新建任务”首先出现任务类型选择。
- [ ] 任务类型包含“做题”和“网课”。
- [ ] 选择“做题”后显示现有做题网站和作业网址字段。
- [ ] 选择“网课”后只显示支持网课的站点。
- [ ] 选择“智慧树”后显示课程类型、课程 ID、班级 ID 和网课参数。
- [ ] 选择课程类型为 AI 课程时可以显示模型档案。
- [ ] 未填写作业网址不会阻止创建网课任务。
- [ ] 网课任务创建到网课存储，不写入做题任务表。

### 15.2 任务列表

- [ ] 做题任务和网课任务显示在同一任务列表。
- [ ] 任务卡片显示任务类型和网站。
- [ ] 可以按任务类型筛选。
- [ ] 可以按网站和状态筛选。
- [ ] 网课任务摘要字段与现有前端兼容。
- [ ] 任务卡片不返回 Cookie、API Key 或完整敏感 URL。

### 15.3 网课详情

- [ ] 点击网课任务进入课程详情，而不是做题向导。
- [ ] 课程详情显示登录、课程信息、学习、考试、结果步骤。
- [ ] 未登录时显示“登录智慧树”。
- [ ] 登录成功后显示“开始学习”。
- [ ] 运行中显示当前项目、进度和停止按钮。
- [ ] 事件记录复用现有事件卡片。
- [ ] 失败和部分完成状态能正确显示。

### 15.4 ZHS 后端兼容

- [ ] `fucker.py` 的核心请求逻辑未被重写。
- [ ] 不使用 `main.py` 作为运行入口。
- [ ] 共享课、校内课、AI 课程均使用原上游方法执行。
- [ ] 视频进度由上游原有逻辑计算和提交。
- [ ] 播放速度、阈值、时限参数能够传入。
- [ ] Cookie 能正确注入 `Fucker.cookies`。
- [ ] 停止任务通过终止子进程实现。

### 15.5 做题模块回归

- [ ] 现有超星学习通流程保持不变。
- [ ] 现有长江雨课堂流程保持不变。
- [ ] 现有答案编辑、模型授权、提交确认功能不变。
- [ ] 现有做题任务查询和操作接口保持兼容。

### 15.6 安全与运行

- [ ] Cookie 和 API Key 不出现在前端响应中。
- [ ] 网课日志不写入源码目录。
- [ ] 停止任务后子进程退出。
- [ ] 网课任务运行不阻塞做题任务。
- [ ] 同一时间只有一个智慧树课程任务运行。

---

## 16. 实施阶段

### 第一阶段：前端兼容契约

- 扩展 `/api/meta`，增加任务类型和站点能力。
- 扩展 `/api/overview`，支持混合任务摘要。
- 为现有做题任务摘要增加 `task_type`。
- 定义网课摘要、详情和事件 DTO。
- 不修改做题状态机。

### 第二阶段：新建任务前端

- 修改 `index.html` 新建任务表单。
- 增加任务类型和动态网站选择。
- 增加网课配置字段。
- 修改 `app.js` 的创建和编辑逻辑。
- 保持做题请求格式不变。

### 第三阶段：网课任务骨架

- 新增 `core/courses`。
- 新增 JSON 任务存储和任务索引。
- 新增课程认证接口。
- 新增课程详情和操作接口。
- 接入智慧树 Cookie Manager。
- 在任务列表中显示网课任务。

### 第四阶段：fuckZHS 子进程

- 嵌入 `fuckZHS` vendor 代码。
- 新增 `bridge.py`。
- 替换 `progressBar` 为事件输出。
- 重定向 stdout 和日志。
- 实现共享课和校内课执行。
- 实现停止和重新运行。

### 第五阶段：网课详情前端

- 增加课程专用步骤条。
- 增加课程进度卡片。
- 增加登录、开始、停止和重新运行按钮。
- 复用现有事件卡片。
- 增加课程状态和类型标签。

### 第六阶段：AI 课程

- 映射共享模型档案。
- 调用上游 `fuckAiCourse()`。
- 接入 AI 考试进度。
- 标记 AI 失败和随机答案状态。

### 第七阶段：加固

- 事件节流；
- 子进程退出处理；
- 日志清理；
- 任务删除清理；
- 集成测试；
- 做题模块回归测试。

---

## 17. 不在本期范围

- 将 `fucker.py` 拆分为多个包。
- 重写 `fuckZhidaoVideo()`、`fuckHikeVideo()` 或 `ExamCtx`。
- 通过解析终端文本驱动前端状态。
- 将网课任务写入现有 `tasks.sqlite3`。
- 多智慧树账号并发运行。
- 多课程并发执行。
- 完整的课程列表管理后台。
- 全量迁移现有做题目录。
- 直接替换上游 AI 客户端。

---

## 18. 风险与应对

| 风险 | 影响 | 应对 |
| --- | --- | --- |
| 上游终端输出混入 JSON | 前端事件解析失败 | stdout/stderr 分离，JSONL 单通道 |
| 上游错误被内部吞掉 | 任务显示完成但实际部分失败 | 第一版保守显示，后续增加统计钩子 |
| 子进程未及时退出 | 任务长期停留在 stopping | terminate 后超时 kill |
| Cookie 无 `CASLOGC` | 认证启动失败 | 开始前检查 Cookie 完整性 |
| AI 档案协议不兼容 | AI 课程失败 | 第一版限制 OpenAI 兼容档案 |
| 前端仍按做题步骤渲染 | 网课详情显示错误 | 根据 `task_type` 强制分流 |
| 做题状态被网课状态污染 | 现有功能回归 | 网课状态独立，仅在展示层兼容 |
| 任务概览扫描过慢 | 页面轮询变慢 | 使用 index.json 摘要，不扫描运行目录 |

---

## 19. 完成定义

本任务完成需同时满足：

1. 前端仍只有一个“新建任务”入口。
2. 用户先选“做题/网课”，再选网站。
3. 做题任务继续进入现有工作流。
4. 网课任务进入独立课程工作流。
5. 网课后端输出兼容当前前端轮询和事件结构。
6. `fuckZHS` 核心业务逻辑未被重写，只通过 bridge 和子进程接入。
7. 支持智慧树登录、共享课、校内课和 AI 课程。
8. 网课详情支持登录、开始、停止、重新运行和进度显示。
9. 做题模块通过完整回归测试。
10. Cookie、API Key 和敏感请求信息不出现在前端响应和日志中。

---

## 20. 最终结论

本方案以现有前端为兼容目标，但不牺牲 ZHS 的业务语义：

```text
现有前端任务列表和轮询结构
→ 复用
网课业务状态和课程步骤
→ 新增专用显示
fuckZHS 核心请求、视频和考试逻辑
→ 保留原实现
与前端之间的通信
→ 通过 bridge 转换为兼容 DTO 和 JSONL 事件
```

最终实现应是：

```text
新建任务
→ 选择做题/网课
→ 选择网站
→ 填写对应配置
→ 创建任务
→ 做题任务进入现有流程
→ 网课任务进入课程进度详情
```
