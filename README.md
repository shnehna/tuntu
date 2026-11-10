# 吞吐 tuntu

我的 Token 都去哪了？——一个本机运行的 Claude Code token 用量看板。

A local, zero-dependency dashboard for your Claude Code token usage. It reads the session logs already on your machine and shows them in a playful one-page dashboard. Nothing leaves your computer. (The UI is in Chinese.)

## 运行

需要 Python 3.9 或更高版本，没有任何第三方依赖。

不用安装，一行运行（需要 [uv](https://docs.astral.sh/uv/)）：

```bash
uvx tuntu
```

或者装成命令：

```bash
pipx install tuntu
```

```bash
tuntu
```

也可以直接从源码运行，不用安装：

```bash
git clone <本仓库地址>
cd <仓库目录>
python3 -m tuntu
```

它会扫描 `~/.claude/projects` 下的会话日志，然后在浏览器里打开 `http://127.0.0.1:8787/`。

没有日志，或者只是想先看看长什么样（也适合截图）：

```bash
tuntu --demo
```

| 参数 | 作用 |
|---|---|
| `--demo` | 用虚构数据演示，不读取任何日志 |
| `--dir PATH` | 指定日志目录（默认 `~/.claude/projects`，也会识别 `CLAUDE_CONFIG_DIR`） |
| `--port N` | 端口，默认 8787，被占用时自动往后找 |
| `--no-open` | 不自动打开浏览器 |
| `--web-fonts` | 从 Google Fonts 加载展示字体，观感更接近设计稿（会产生外网请求） |
| `--json` | 把聚合结果打印成 JSON 后退出 |
| `--version` | 显示版本号 |

## 能看到什么

- **总量**：一共吞了多少 token，平均每秒多少，日均多少。
- **读写比**：每写出 1 个 token，先读了多少个。
- **缓存命中率**：缓存读取 ÷ 全部输入。
- **每日吞吐**：可以切换看全部、输出、请求次数或命中率。
- **24 小时钟、一周作息表**：你什么时候用得最多。
- **构成**：缓存读取、缓存写入、输出、新输入各占多少；缓存写入里 1 小时档的占比；子代理的占比。
- **推理强度、模型、项目、工具**：各自的分布。
- **会话马拉松榜**：最耗 token 的会话。点进去能看到上下文长度随对话增长、被自动压缩的过程。
- **成就贴纸**：夜猫子、缓存大师、马拉松选手……

时间范围可以切换：全部、近 30 天、近 7 天、今天。

## 隐私

- 服务只监听 `127.0.0.1`，并且拒绝 Host 不是本机的请求。
- 默认不发出任何外网请求，页面带有禁止外部资源的 CSP。只有加了 `--web-fonts` 才会去请求 Google Fonts。
- 只取用日志里的用量数字、时间、模型名、工具名和工作目录名；对话内容不会被保存、显示或发送到任何地方。
- 页面上的项目只显示目录名，不显示完整路径；日志目录里的家目录显示为 `~`。
- 不写任何文件，不做缓存，关掉就什么都不留。

分享截图前请留意：项目名和工具名来自你自己的机器。想要一张不含个人信息的图，用 `--demo`。

## 数字是怎么算的

- 数据来自 Claude Code 写在本机的会话日志（`*.jsonl`）里每次请求的 `usage` 字段。
- 同一条消息在日志里会出现多行，按消息去重后再累加。
- 续接或分叉出来的会话会带着前半段记录。算总量时每条消息只算一次；但会话榜里每个会话按它自己的全部记录算，所以各会话相加会超过总量。
- 项目按会话启动时的目录归类，worktree 里的会话归到原项目。
- “会话跨度”是第一次请求到最后一次请求之间的时间，中间可能隔了很久没用。
- 时间按本机时区显示。
- Claude Code 默认只保留一段时间的日志（由 `cleanupPeriodDays` 设置决定），更早的用量已经不在本机，这里也就看不到。
- 这里只统计本机的 Claude Code，不包含 claude.ai 网页或手机上的对话。
- 这些数字是从本机日志估算的，不是账单。以官方的用量页面为准。

## 目录结构

```
tuntu/cli.py           扫描日志、聚合、本机 HTTP 服务（只用标准库）
tuntu/web/index.html   页面（单文件，原生 JS，无依赖）
pyproject.toml         打包配置
```

## 发布新版本（维护者）

1. 改 `tuntu/__init__.py` 里的 `__version__`。
2. 构建并上传：

```bash
uv build
```

```bash
uv publish
```

## 说明

这是个人项目，与 Anthropic 无关。Claude 和 Claude Code 是 Anthropic 的商标。

## 许可证

MIT，见 [LICENSE](LICENSE)。
