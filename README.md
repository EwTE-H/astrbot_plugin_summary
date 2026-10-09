# astrbot_plugin_summary

> QQ 群消息「回顾 / 摘要 / 归档」插件。按关键词或时间区间拉取群聊（含合并转发、图片），
> 经 LLM 生成中文摘要，可选写入 Miraheze wiki 页面并附图。

---

## 功能特性

- **群聊回顾**：按时间区间或引用一条消息作为起点，LLM 生成中文摘要，以合并转发发回群。
- **归档上传**：把摘要写入 Miraheze wiki 页面（页面已存在则追加，不存在则新建），正文引用的图片一并上传。
- **图片识别**：自动登记图片并编号 `[图#N]`，记录 `subType`（0=普通图 / 1=表情包 / 2=GIF / 未知）。
- **调试指令**：扫描区间内图片的 `subType` 分布，验证「表情包筛除」可行性（不调 LLM、不上传）。

---

## 整体架构

`main.py` 只放 **`@filter.command` 指令方法 + `__init__`/`terminate`**（module_path 决定注册，不能拆走）。
其余逻辑按职责拆进 `core/` 的 9 个 Mixin，靠 `PluginSummary(..., Star)` 多重继承组合：

| Mixin | 文件 | 职责（一句话） |
|---|---|---|
| TimeUtilsMixin | `core/time_utils.py` | 时间字符串解析、`_normalize_times` 补全、`_fmt_ts` 格式化 |
| ArgsMixin | `core/args.py` | `_parse_args`：回顾类指令的「时间 / 引用 / 默认」参数解析 |
| MessageMixin | `core/message.py` | 拉消息、合并转发递归解析、图片登记、`_fetch_and_filter`、合并转发发送 |
| ImageMixin | `core/images.py` | 图片本地化（`_materialize_images`）、清单构建、扩展名判断 |
| WikiMixin | `core/wiki.py` | Miraheze API 封装（写页面、单图上传） |
| PackMixin | `core/pack.py` | 「测试wiki打包上传」：合并转发 → 节点 → 下载图 → 打包上传 |
| SummaryMixin | `core/summary.py` | `_execute_summary`：串联解析→拉取→组 prompt，是 `/回顾` 与 `/回顾上传` 的公共内核 |
| LLMMixin | `core/llm.py` | `_run_summary_llm`：调 LLM 生成摘要（含图文附图） |
| ReviewMixin | `core/review.py` | `_review_up_parse_args` + `_review_up_publish`（wiki 页面新建 / 追加） |

---

## 指令一览

| 指令 | 参数格式 | 作用 |
|---|---|---|
| `/回顾` | `<开始> <结束> [关键词]` / `<关键词>`(引用) / 无参(默认 2 天) | LLM 生成群聊摘要，合并转发发回群 |
| `/回顾上传` | `<主题> <页面名> <开始> <结束>` / `<主题> <页面名>`(引用) | **直连 GLM API（图文混排）**生成摘要并写入 wiki，引用图一并上传；群内只回归档链接（进度/错误入日志） |
| `/回顾上传backup` | 同 `/回顾上传` | 旧版：经 AStrBot 代调 LLM 生成摘要并上传；保留备用 |
| `/回debug顾` | 同 `/回顾` | 调试：扫描区间图片 `subType`，输出表情包筛除清单（**不调 LLM、不上传**） |
| `/测试合并转发` | — | 把 main.py 源码当合并转发发回（验证转发通道） |
| `/测试wiki上传` | — | 把 main.py 写入 Miraheze 测试页（账号密码走插件配置项） |
| `/测试wiki打包上传` | `<页面名>`(引用一条合并转发) | 把转发里图文打包上传到指定 wiki 页 |

> 页面名空格用下划线 `_` 代替；wiki 账号密码在 AStrBot 管理面板配置，不硬编码。

---

## 关键数据流

**`/回顾` 流程**
```
event → _parse_args → _execute_summary(拉消息+组prompt)
     → _run_summary_llm(附本地化图 file://) → _send_as_forward(摘要)
```

**`/回顾上传` 流程**
```
_review_up_parse_args → _execute_summary(attached=True)
  → _run_summary_llm → 正文引用编号 → _review_up_publish(写/追加 wiki + 上传引用图)
  → 群发归档链接 + (_send_as_forward_with_images 渲染 [图#N] 真实图)
```

**图片登记**：`_register_image` 给每张图编 `#N`，`image_meta` 记 `url/local/subType/sender/ts/ctx`；
`subType`：`0`=普通图 `1`=表情包 `2`=GIF。

---

## 开发约定（代码层）

- **`@filter.command` 必须留 main.py**；辅助方法拆进 `core/`，用多重继承。
- **实例方法禁止误标 `@staticmethod`**——曾致 `self` 不绑定、`event` 被 `int` 顶替而崩溃。
- **reload 走插件 API，不依赖 watchdog 热重载**（框架热重载会失败且不重试）。
- **图片编号不设上限**：超限的新图会被误判成 `[空消息]`。
- **日志只保留进出 / 累计 / 汇总 / 错误几行**，禁止逐条刷（会冲爆日志文件）。
- **群内输出只发归档链接**；字节数 / 版本号 / 图片成败等技术信息只写日志。
- **归档链接中的中文需 percent 编码**（`urllib.parse.quote`）。

---

## LLM 与图床

- 摘要经 AStrBot 代调 GLM 5.3 Flash；图片以本地 `file://` 路径附给模型（防直链失效错位）。
- **已知问题**：单批附图有上限，图多时模型只引前几张；分批调 LLM / 直调 API / Plan B 兜底方案待定。
- **QQ 图床直传**：`multimedia.nt.qq.com.cn` 不防盗链，wiki 可直拉原图（绕过压缩、画质无损，但增加存储）。

---

## 待办 / 未决

- [ ] 验证 `subType` 是否真被协议端透传 `0/1/2`（决定表情包筛子能否接进 `/回顾`/`/回顾上传`）。
- [ ] 图多时 LLM 漏引：分批 / 直调 API / Plan B 兜底，待拍板。
- [ ] （可选）让 `/回debug顾` 支持「纯时间区间、无关键词」。

---

## 部署

本地部署、服务器、重载与日志查看等运维流程**不随本仓库公开**，见团队内部文档。

---

*框架文档，非逐行注释；具体实现以源码为准。*
