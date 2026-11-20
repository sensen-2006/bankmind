# BankMind（银账智核）Unified Design System — DESIGN.md

## 1. 品牌视觉定位
- **关键词**：清爽、利落、干净、轻快活泼、金融级可信赖。
- **风格参考**：融合 **Notion**（克制留白与极细微边 `whisper borders`）、**Stripe**（清晰金融数字排版与通透层级）、**Linear**（暗色模式背景亮度阶梯）与 **Apple**（克制主色 + 丝滑弹簧微动效）。
- **明确禁止**：
  - 禁止发黄沉闷的橄榄灰底色（`#eeeee8`）、老气衬线体（Georgia/宋体）、古铜金暗黑 RPG 风（`#0c1c24` + `#d8c392`）、违和的深蓝紫科幻块（`#101a33`）。
  - 禁止紫到蓝的廉价 AI 模板渐变。
  - 禁止框中套框（卡片内嵌套带边框的子框）、禁止小于 12px（Web）/ 22rpx（小程序）的中文字号、禁止负字距挤压中文。

## 2. 双主题定义（严格使用「明亮 / 深色」命名）
- 主题切换控件命名：**`明亮`**（Light，默认，晴日清爽）与 **`深色`**（Dark，静谧专注）。
- 严禁出现 `游戏风`、`经典风`、`切换界面材质` 等令人困惑的文案。
- 交互形式：采用类似 `ludion.ai/en/p/toggle-daylight` 的日月滑动胶囊开关（带柔和弹簧过渡与图标形态变化），在 Web 顶栏与小程序「工作台 / 我的」页均提供清晰入口，持久化到本地存储。

### 2.1 明亮模式 Tokens (`data-theme="light"`)
| Token | 色值 | 用途 |
|---|---|---|
| `--bg-canvas` | `#F6F8FA` | 页面底层画布（极浅清透冷灰白） |
| `--bg-surface` | `#FFFFFF` | 主卡片、面板、弹窗底色 |
| `--bg-subtle` | `#EFF3F6` | 输入框、表头、次级信息块、胶囊底色 |
| `--bg-elevated` | `rgba(255, 255, 255, 0.88)` | 顶栏、悬浮条（配 `blur(16px)`） |
| `--text-primary` | `#111827` | 主标题、核心金额、正文 |
| `--text-secondary` | `#4B5563` | 次级说明、表格正文 |
| `--text-muted` | `#6B7280` | 辅助元数据、时间、占位符（确保 WCAG AA） |
| `--border-subtle` | `rgba(15, 23, 42, 0.08)` | 常规微边（Whisper Border） |
| `--border-strong` | `rgba(15, 23, 42, 0.15)` | 输入框/交互控件描边 |
| `--accent` | `#0D9488` | 品牌主色：清透活力青碧绿（Teal 600） |
| `--accent-hover` | `#0F766E` | 主色悬停态 |
| `--accent-pop` | `#14B8A6` | 活泼高亮、进度条、激活指示器 |
| `--accent-soft` | `rgba(13, 148, 136, 0.10)` | 主色浅底胶囊/选中态背景 |
| `--accent-on` | `#FFFFFF` | 主按钮文字 |
| `--success` / `--success-soft` | `#059669` / `#ECFDF5` | 已完成 / 已匹配 / 核验通过 |
| `--warning` / `--warning-soft` | `#D97706` / `#FFFBEB` | 待复核 / 待确认 / 提醒 |
| `--danger` / `--danger-soft` | `#DC2626` / `#FEF2F2` | 异常 / 退回 / 拒绝 / 错误 |
| `--info` / `--info-soft` | `#2563EB` / `#EFF6FF` | 中性提示 / 辅助信息 |
| `--shadow-sm` | `0 1px 2px rgba(15, 23, 42, 0.04)` | 轻量控件阴影 |
| `--shadow-card` | `0 4px 16px rgba(15, 23, 42, 0.05)` | 卡片轻阴影 |
| `--shadow-float` | `0 16px 40px rgba(15, 23, 42, 0.12)` | 弹窗/下拉浮层阴影 |

### 2.2 深色模式 Tokens (`data-theme="dark"`)
> 遵循 Linear 暗色原则：用**背景亮度阶梯**表达层级，而非厚重阴影；用高明度薄荷青保持深色下的轻快感。
| Token | 色值 | 用途 |
|---|---|---|
| `--bg-canvas` | `#0B0F14` | 页面底层画布 |
| `--bg-surface` | `#121821` | 主卡片、面板 |
| `--bg-subtle` | `#1A222E` | 输入框、表头、次级块、悬浮行 |
| `--bg-elevated` | `rgba(18, 24, 33, 0.86)` | 顶栏、弹窗底色（配 `blur(16px)`） |
| `--text-primary` | `#F3F4F6` | 主标题、核心金额、正文 |
| `--text-secondary` | `#9CA3AF` | 次级说明 |
| `--text-muted` | `#8492A6` | 辅助元数据（保证夜间高可读） |
| `--border-subtle` | `rgba(255, 255, 255, 0.08)` | 极细微光描边 |
| `--border-strong` | `rgba(255, 255, 255, 0.16)` | 控件描边 |
| `--accent` | `#2DD4BF` | 深色主色：清透薄荷青（Teal 400） |
| `--accent-hover` | `#5EEAD4` | 主色悬停态 |
| `--accent-pop` | `#14B8A6` | 进度条/高亮 |
| `--accent-soft` | `rgba(45, 212, 191, 0.14)` | 浅底胶囊/选中态 |
| `--accent-on` | `#042F2E` | 主按钮文字（高对比深青黑） |
| `--success` / `--success-soft` | `#34D399` / `rgba(52, 211, 153, 0.14)` | 已完成 / 已匹配 |
| `--warning` / `--warning-soft` | `#FBBF24` / `rgba(251, 191, 36, 0.14)` | 待复核 / 提醒 |
| `--danger` / `--danger-soft` | `#F87171` / `rgba(248, 113, 113, 0.14)` | 异常 / 错误 |
| `--info` / `--info-soft` | `#60A5FA` / `rgba(96, 165, 250, 0.14)` | 中性提示 |
| `--shadow-sm` / `--shadow-card` | `none`（靠边框与亮度区分） | 干净利落 |
| `--shadow-float` | `0 20px 50px rgba(0, 0, 0, 0.45)` | 弹窗浮层 |

## 3. 字体、圆角、间距与层级宪法
- **字体栈**：`-apple-system, BlinkMacSystemFont, "PingFang SC", "HarmonyOS Sans SC", "Microsoft YaHei", "Inter", sans-serif`
- **数字排版**：所有金额、百分比、笔数、日期强制 `font-variant-numeric: tabular-nums; word-break: keep-all; white-space: nowrap;`（禁止 `word-break: break-all` 截断数字！）。
- **字号底线**：
  - Web：正文 `14px`，辅助/表格头 `12px–13px`，页面主标题 `24px–28px`，核心 KPI 数字 `28px–36px`。**严禁任何 `<12px` 字号**。
  - 小程序：正文 `28rpx`，次级 `24rpx–26rpx`，最小标签 `22rpx`，页面标题 `44rpx–48rpx`，核心 KPI `56rpx–68rpx`。**严禁负字距 `-3rpx`**。
- **圆角体系**：
  - Web：`--radius-sm: 8px`（按钮/胶囊）、`--radius-md: 12px`（输入框/小卡）、`--radius-lg: 16px`（主面板/弹窗）、`--radius-full: 9999px`（状态点/开关）。
  - 小程序：`--radius-sm: 16rpx`、`--radius-md: 24rpx`、`--radius-lg: 32rpx`。
- **Web Z-Index 严格层级**：
  - `--z-sticky: 20` < `--z-dropdown: 40` < `--z-backdrop: 100` < `--z-modal: 110` < `--z-tooltip: 200` < `--z-toast: 300` < `--z-skip: 400`。
- **动效规范（轻快活泼但不干扰工作）**：
  - 统一缓动：`cubic-bezier(0.22, 1, 0.36, 1)`；微交互时长 `150ms–220ms`；页面/弹窗进入 `240ms`。
  - 按钮点击轻回弹 `scale(0.97)`；尊重 `prefers-reduced-motion: reduce`。
  - 禁止表格整体位移跳动、禁止表格行 hover 时修改 `font-weight` 造成横向抖动。
