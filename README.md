# 简历筛选助手

通过岗位配置和模型评估辅助处理 BOSS 候选人。默认在 Apple Silicon Mac 上操作 iOS 版 BOSS 客户端，保留原有浏览器后端。

## 安装与启动

客户端模式需要已安装并登录的 iOS 版 BOSS、Python 环境和 Xcode 命令行工具。本轮实测使用 Python 3.12。

```bash
pip install -r requirements.txt
xcode-select --install
```

在项目根目录创建 `.env`：

```dotenv
OPENAI_API_KEY=your_api_key
LLM_MODEL=gpt-5-mini
LOG_LEVEL=INFO
# 使用兼容服务时设置对应地址及模型名称
# OPENAI_BASE_URL=http://127.0.0.1:8000/v1
```

先手动打开 iOS 版 BOSS，完成登录及验证，再运行：

```bash
# 默认使用 params.json，允许正常联系候选人
python main.py

# 指定配置文件
python main.py -c path/to/config.json

# 使用示例配置，只读取和评估，不联系或标记候选人
python main.py -c params.native.example.json --read-only

# 逐人显示淘汰建议摘要，经人工确认后标记不合适
python main.py -c path/to/config.json --review-rejections
```

默认启用新招呼和推荐流程，并允许正常联系；`--allow-contact` 保留为显式别名。`--read-only` 和 `--review-rejections` 仅用于客户端模式，不能同时使用。

**打开简历详情会消耗平台查看额度。** `--read-only` 只关闭联系和标记动作，仍可能打开详情并消耗额度。检查列表、筛选器和卡片关键词时不需要打开详情。

## 客户端准备与权限

- 使用 Mac 上的 iOS 版 BOSS，目标标识为 `com.hpbr.bosszhipin`。
- 登录由用户手动完成。脚本复用客户端登录状态，遇到登录、验证或账号异常提示时停止。
- 在“系统设置 → 隐私与安全性 → 辅助功能”中，为启动脚本的终端或执行程序授权。
- 正常流程通过辅助功能读取文字，不截图，不需要屏幕录制权限；截图诊断和可选符号复核工具需要该权限。
- 保持 Mac 解锁、BOSS 窗口打开。辅助功能操作支持后台和窗口被覆盖的情况；运行结束不会退出客户端。

Python 调用仓库内的 Swift 桥接程序，不依赖 Codex 或计算机操作插件。桥接程序首次使用时自动编译，同一次运行复用编译结果。

## 配置

配置由 `commentjson` 读取，支持注释和尾逗号。可提供单个岗位对象或岗位数组；程序按数组顺序处理，不额外排序。示例见 [params.native.example.json](params.native.example.json)。

| 字段 | 含义 |
|---|---|
| `job_title` | 客户端显示的岗位名称 |
| `max_idx` | 推荐流程最多扫描人数，默认 120，包含提前跳过的卡片 |
| `job_requirements.selector_job_title` | 可选的界面岗位名称，须完整、唯一匹配 |
| `job_requirements.cv_required_keywords` | 卡片关键词条件，支持下述树状规则 |
| `job_requirements.cv_requirements` | 交给模型评估的完整岗位要求 |
| `job_requirements.maximum_salary` | 本地薪资上限；大于 0 时启用，要求候选人期望最低薪资严格小于该值 |
| `job_requirements.education` | 本地最低学历：0 不限制；1 初中及以下、2 高中、3 中专/中技、4 大专、5 本科、6 硕士、7 博士 |
| `job_requirements.off_the_job` | 大于 0 时，本地检查要求“离职-随时到岗” |
| `job_requirements.age_lower_bound` / `age_upper_bound` | 原有本地年龄检查范围，默认 0–100 |
| `job_requirements.gender` | 原有本地明确字段检查；默认不限制，仅接受简历中明确的性别字段 |
| `url` | 仅浏览器模式使用，客户端模式忽略 |

客户端岗位名称与网页下拉框中的“岗位、城市、薪资”组合标签不同，不能直接混用。重复岗位名无法唯一匹配时停止。

除卡片关键词外，上述本地字段检查目前在打开详情后执行。程序加载平台保存的筛选条件，不会把这些配置字段自动转换成平台选项。

### 关键词规则

使用区分大小写的子串匹配。空列表不限制；普通列表表示任意一个命中：

```json
"cv_required_keywords": ["A", "B", "C"]
```

父节点与子条件之间为“且”，同层分支之间为“或”。例如 `A 且 (B 或 C 或 D)`：

```json
"cv_required_keywords": {"A": ["B", "C", "D"]}
```

继续嵌套可表达 `(A 且 B 且 (C 或 D)) 或 (E 且 F)`：

```json
"cv_required_keywords": {
  "A": {"B": ["C", "D"]},
  "E": ["F"]
}
```

`{"A": []}` 只要求 A。`["B", "C", "D"]` 表示三个词任选其一，`["BCD"]` 表示匹配完整字符串 `BCD`。

## 推荐流程

1. 按配置顺序切换岗位，进入“推荐”。
2. 打开筛选器，出现“是否应用该职位上次的筛选条件？”时，点击唯一的“应用”，加载该职位自己的记录；没有提示时沿用当前条件。
3. 确保“近期没有看过 → 近14天没有”开启，保留其他条件，确认后核对仍是目标岗位列表。
4. 在列表卡片的简介、技能标签、公司和职位文字上执行关键词初筛。未命中直接跳过，不打开详情、不调用模型，计入 `card_prefilter_skipped`。
5. 通过后才打开详情，展开并滚动读取完整简历，执行本地字段检查和全文关键词核验，再调用模型。
6. 联系模式下，对符合条件者点击“立即沟通”。客户端会发送已设置的岗位招呼语，脚本确认同一会话中消息“送达”或“已读”后计数，不再补发常用语。
7. 返回当前岗位列表，继续下一位。

请先在客户端分别设置并保存各职位的筛选条件和自动招呼语。求职状态、学历、年龄、薪资等平台选项目前通过已保存的筛选器复用；脚本尚未自动逐项设置。

历史筛选只覆盖近14天的查看记录。程序还会在本次运行内去重，并跳过卡片明确含“已看过/已沟通”的候选人。列表未展示的经历不参与卡片关键词初筛，这与原网页版的卡片初筛顺序一致。

详情文字按完整经历单元合并翻页内容，保留不同雇主下相同的职位和职责。读取前后均确认回到顶部，并通过滚动内容和位置是否停止变化判断边界。隐藏“求职期望”、显示“最近关注”的简历也能读取；最近沟通过岗位的薪资不会填入候选人期望薪资。

本地薪资解析支持元、万元、K 和千分位形式。启用 `maximum_salary` 时，面议或缺失薪资不通过该检查；模型还会按 `cv_requirements` 执行自身的薪资评估规则。

## 新招呼流程

仅进入“未读”列表；没有“未读”入口时结束本轮，不回退处理全部会话。打开会话会使客户端将其标为已读。

程序从会话确认实际岗位，打开详细简历、展开经历并滚动读取全文，再调用模型。符合条件时，点击已保存常用语同一行的“发送”按钮。请提前保存完整文案：

> 您好，感谢您的主动联系！方便发一份完整简历，进一步了解一下您的相关经历吗？

常用语按完整文字唯一匹配，缺失或有歧义时停止，不修改已有草稿。只有当前候选人聊天历史中的该消息数量增加，才计为发送成功；未确认时停止，不重复发送。

模型在同一次评估中仍返回 `greeting_message`，但主流程当前使用固定常用语。个性化输入函数保留，尚未接通实际客户端，主流程不调用。

模型给出的淘汰结论默认汇总到 `pending_rejections`，继续处理后续候选人。`--review-rejections` 会在确认前输出建议，控制台保留80字摘要，完整理由和原因分类写入 `boss_hire.log`；只有逐人输入 `y/yes` 才允许提交。真实客户端选择原因后会返回列表，标记成功状态的适配仍在联调。

可在 `.env` 中分别禁用流程：

```dotenv
DISABLE_GREETINGS_LOOP=true
DISABLE_RECOMMEND_LOOP=true
```

## 异常、日志与诊断

- 权限、登录、验证、账号异常、无法返回及未知错误停止流程。
- 简历加载超时后，仅在确认已返回列表时跳过当前候选人。
- 筛选刷新可能使列表节点失效。仅对桥接程序明确报告的点击前路径或身份失效重新定位，连续第三次失败停止，成功打开前不增加扫描计数。
- 返回按钮执行成功但页面未变时，只在确认仍是原页面后重试一次。联系和提交不使用节点失效重试。
- 消息列表可能保留连接失败横幅。列表导航、筛选和返回允许带横幅执行；进入会话、读取详情和发送仍执行严格检查，登录与验证提示始终停止。
- 操作间隔、轮询及模型请求退避带随机抖动，超时截止时间和稳定窗口沿用原有判定。

控制台显示处理进度，模型日志折成单行并保留80字摘要；`boss_hire.log` 保存完整的模型日志、警告和错误。退出时按岗位汇总查看、卡片初筛跳过、联系和待复核等统计。

可选卡片诊断会保存简历文字，仅在需要时启用，并使用专用目录：

```dotenv
SAVE_RESUME_DIAGNOSTICS=true
RESUME_DIAGNOSTICS_PATH=diagnostics/resume_cards.jsonl
```

## 可选的符号人工复核

这是独立工具，不向主流程的 `gender` 字段写入识别结果，也不发送消息或标记候选人。工具会打开详情做本地检查，因此同样涉及查看额度。

```bash
python native_symbol_review.py --candidate-index 1
python native_symbol_review.py -c path/to/config.json --job-title 市场专员
python native_symbol_review.py --model-fallback
```

工具先做本地资料检查，最后截图识别明确的 `♂/♀` 符号。默认使用本地模板匹配；只有显式启用 `--model-fallback` 且发现可能的符号形状时，才把小块图片发送给配置的模型。颜色本身不决定符号。

裁切依据当前窗口、卡片位置、固定控件比例和实际截图像素比例，截图前后核对窗口及候选人。未找到徽标或符号不清时返回未知；窗口、候选人或比例锚点校验失败时停止。需要屏幕录制权限；小图默认保存到 `~/codex/artifacts/`，可用 `--output` 指定位置。

## 保留的浏览器模式

```bash
python main.py --backend web -c path/to/config.json
```

该模式通过 zendriver 启动 Chrome，等待用户完成登录，读取卡片并执行本地初筛，再把完整简历截图交给模型。它保留原有自动联系流程，不能使用客户端的 `--read-only` 或 `--review-rejections` 控制行为。

浏览器简历截图沿嵌套 iframe 的滚动容器和裁切边界分段拼接，按小数尺寸映射输出行，并在完成、异常或取消时恢复滚动位置。每段使用9个原生 CDP 命中点检查遮挡；完成截图在文档、字体、图片和网络加载检查后，须连续稳定至少2秒。这是异步绘制的启发式判断，无法完整显示时停止。

## 测试与验证范围

以下回归使用模拟界面和传输，不操作真实账号：

```bash
python -m unittest test_main_native test_native_driver test_native_workflow test_native_greetings test_native_llm test_llm_utils -v
python -m unittest test_native_symbol_review test_badge_recognition test_symbol_review -v
python -m unittest test_driver_utils test_workflow_errors -v
```

浏览器截图验证使用本机服务与独立 Chrome 配置；符号实机测试只截图当前推荐页，不打开详情。运行前确认所选测试与工具的范围：

```bash
RUN_BROWSER_TESTS=1 python -m unittest test_resume_capture -v
RUN_NATIVE_UI_TESTS=1 python -m unittest test_native_symbol_review_live -v
```

| 范围 | 当前验证情况 |
|---|---|
| 完整简历、滚动与返回 | 已实机验证，包括“最近关注”布局 |
| 推荐逐项处理与切岗 | 三个职位各连续两份，核对简历内容不同及返回原岗位 |
| 各职位上次筛选与查看历史 | 三个职位均已实机验证，不打开详情 |
| 卡片关键词初筛 | 已用真实列表验证不命中项跳过，详情打开数为0 |
| 模型接口 | 已使用配置中的接口取得真实评估结果 |
| 推荐自动招呼、新招呼常用语 | 均已实机确认送达 |
| 不合适标记 | 原因面板与选择已验证，成功状态适配仍在联调 |
| 每日沟通上限、薪资通过与拒绝专项 | 尚未完成实机验证 |
| 窗口尺寸变化 | 小图裁切已验证两种尺寸，完整流程未遍历全部尺寸 |
| 浏览器截图拼接 | 已做合成页面验证，当前 BOSS 实际页面尚未验证 |

## 许可证

使用 [MIT 许可证](LICENSE)。
