# 字幕配音小助手

基于阿里云 DashScope API 的 AI 字幕配音工具。上传音视频自动提取字幕，或导入 SRT 字幕文件，选用 **CosyVoice** 196 种音色生成**按时间轴精确对齐**的配音音频。Web 界面操作，支持音色试听、收藏、Instruct 情感指令控制。

![Python](https://img.shields.io/badge/Python-3.10+-blue)
![Flask](https://img.shields.io/badge/Flask-2.x-green)
![License](https://img.shields.io/badge/License-MIT-yellow)

## ✨ 功能特性

### 📝 字幕生成与编辑
- **音视频转字幕** — 上传音频/视频，通过 Paraformer V2 模型自动识别生成带时间戳的字幕
- **SRT 导入** — 直接上传 `.srt` 字幕文件，跳过转录步骤
- **字幕编辑** — 在线调整时间轴、修改文本、增删字幕条目
- **SRT 预览/下载** — 一键预览并导出 SRT 文件

### 🎙️ 时间轴对齐配音
- **逐段精确对齐** — 每段语音精确定位到字幕起始时间，字幕间隙自动填充静音
- **间隙感知裁剪** — 当语音超出字幕窗口时，优先利用相邻字幕间的空白区域，避免句尾被截断（吃字）
- **实时进度追踪** — Web 端合成时显示逐段进度

### 🎵 音色管理
- **196 种音色** — 严格按[阿里云 CosyVoice 音色列表文档](https://docs.bailian.console.aliyun.com/zh/model-studio/cosyvoice-voice-list)配置
  - CosyVoice V3 Flash：88 个音色
  - CosyVoice V3 Plus：2 个音色
  - CosyVoice V2：106 个音色
- **音色卡片** — 展示完整信息：名称、描述、年龄、语言、适用场景、特性支持（SSML / Instruct / 时间戳）
- **点击试听** — 卡片右上角试听按钮，一键合成样本并播放，多个试听互斥
- **收藏持久化** — 星标收藏音色，数据保存到服务器 `favorites.json`，刷新页面/换设备不丢失
- **我的收藏** — 模型下拉菜单含"⭐ 我的收藏"选项，跨模型统一展示已收藏音色

### 🎭 Instruct 情感指令（仅支持 Instruct 的音色）
选中支持 Instruct 的音色时显示指令设置面板，提供 4 种模板快速生成严格符合文档格式的中文指令：

| 模板 | 格式 | 示例 |
|------|------|------|
| 设置情感 | `你说话的情感是<情感值>。` | `你说话的情感是happy。` |
| 场景 + 情感 | `你正在进行<场景>，你说话的情感是<情感值>。` | `你正在进行新闻播报，你说话的情感是neutral。` |
| 角色 + 情感 | `你现在说话的角色是<角色>，你说话的情感是<情感值>。` | `你现在说话的角色是一个旁白，你说话的情感是surprised。` |
| 身份 + 情感 | `你正在以一个<身份>的身份说话，你说话的情感是<情感值>。` | `你正在以一个故事机的身份说话，你说话的情感是neutral。` |

**支持的槽位值**：
- 情感：`neutral` / `fearful` / `angry` / `sad` / `surprised` / `happy` / `disgusted`
- 场景：闲聊互动 / 新闻播报 / 广告促销 / 比赛解说 / 一些儿童内容解说 / 语音导航 / 脱口秀表演
- 角色：一个旁白
- 身份：故事机

> 指令必须使用中文，须严格按格式填写，**不可遗漏结尾句号**。前端实时校验槽位合法性，非法值会红字提示并拦截试听/合成。

## 🛠️ 技术栈

- **后端**：Python 3.10+ / Flask
- **TTS / ASR**：阿里云 DashScope API（CosyVoice + Paraformer V2）
- **音频处理**：FFmpeg
- **前端**：原生 HTML / CSS / JavaScript（Claymorphism �软黏土风格）
- **数据持久化**：`favorites.json`（收藏数据）

## 📦 安装

### 环境要求

- Python 3.10+
- FFmpeg
- [DashScope API Key](https://dashscope.console.aliyun.com/)

### 步骤

```bash
# 1. 克隆仓库
git clone https://github.com/wangyouwei2016/audio_workspace.git
cd audio_workspace

# 2. 安装 Python 依赖
pip3 install dashscope flask

# 3. 确认 ffmpeg 已安装
ffmpeg -version

# 4. 配置 API Key
cp .env.example .env
# 编辑 .env，填入你的 DASHSCOPE_API_KEY
```

环境变量：

| 变量名 | 用途 | 获取地址 |
|--------|------|---------|
| `DASHSCOPE_API_KEY` | CosyVoice TTS + Paraformer ASR | [DashScope 控制台](https://dashscope.console.aliyun.com/) |

## 🚀 使用方式

### Web 界面（推荐）

```bash
# 前台启动（Ctrl+C 停止）
./start.sh

# 后台启动（日志写入 app_runtime.log）
./start.sh --bg

# 停止后台进程
./start.sh --stop

# 指定端口（默认 5000）
./start.sh --port 8080
```

启动后访问 http://localhost:5000

**操作流程**：
1. **Step 1 上传** — 上传音视频文件自动转录，或点击 Step 2 直接导入 SRT
2. **Step 2 编辑** — 审阅、调整时间轴和文本
3. **Step 3 配音** — 选择模型 → 选音色（可试听/收藏）→ 设置 Instruct 指令（可选）→ 开始合成

### 命令行工具

```bash
# 用法
python3 srt_to_tts.py --srt 字幕.srt --output 配音.mp3 --model cosyvoice-v3-flash --voice longanyang

# 参数
--srt      SRT 字幕文件路径（必填）
--output   输出音频文件路径（默认 output.mp3）
--model    TTS 模型（默认 cosyvoice-v3-flash）
--voice    音色 ID（默认 longanyang）
```

## 📁 项目结构

```
audio_workspace/
├── app.py                  # Flask 主应用,含 VOICES 配置、API 路由、合成逻辑
├── srt_to_tts.py           # 命令行工具 + SRT 解析 + 音频工具函数
├── start.sh                # 启动脚本(前台/后台/停止/端口)
├── .env.example            # 环境变量示例
├── favorites.json          # 用户收藏数据(运行时自动生成,已 gitignore)
├── templates/
│   └── index.html          # Web UI 模板
├── static/
│   ├── app.js              # 前端逻辑(卡片渲染、试听、收藏、Instruct 面板)
│   └── style.css           # Claymorphism 风格样式
└── uploads/                # 运行时临时文件(已 gitignore)
```

## 🔌 API 接口

| 方法 | 路径 | 说明 |
|------|------|------|
| GET | `/` | Web UI 主页 |
| GET | `/api/voices` | 获取全部音色列表（196 个，含完整字段） |
| POST | `/api/upload` | 上传音视频文件并自动转录为字幕 |
| POST | `/api/upload-srt` | 上传 SRT 字幕文件 |
| POST | `/api/synthesize` | 单段文本合成（用于试听），支持 `instruction` 参数 |
| POST | `/api/synthesize-timeline` | 按时间轴批量合成（异步任务），支持 `instruction` 参数 |
| GET | `/api/synthesize-status/<job_id>` | 查询时间轴合成进度 |
| GET | `/api/synthesize-download/<job_id>` | 下载时间轴合成结果 |
| GET | `/api/favorites` | 获取全部收藏音色 |
| POST | `/api/favorites` | 添加收藏 |
| DELETE | `/api/favorites/<voice_id>` | 删除收藏 |

## 📝 注意事项

- **API Key 安全**：`.env` 文件已加入 `.gitignore`，请勿提交真实密钥
- **收藏数据**：`favorites.json` 为运行时生成，已加入 `.gitignore`；备份时直接复制该文件即可
- **Instruct 指令**：仅 CosyVoice V3 系列的部分音色支持（如 `longanyang`、`longanhuan`、`longhuhu_v3`），前端会自动识别并显示指令面板
- **时间轴对齐**：合成时如某段语音超出字幕窗口，会优先利用相邻字幕间隙；如仍超出，自动裁剪避免覆盖下一段
- **音色兼容性**：CosyVoice V2 的音色不能用于 V3 模型，反之亦然，前端按模型分组展示避免误选

## 📄 License

MIT
