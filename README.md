# astrbot_plugin_pt - 摸头杀插件

AstrBot 摸头杀插件 - 当用户拍机器人时自动生成并发送摸头GIF动图。

## 功能特性
- 🖼️ **动态生成**: 使用用户QQ头像实时生成个性化摸头GIF
- ⌨️ **手动触发**: 支持通过 `/摸摸@用户`或'/摸摸' 命令手动触发
- ✨ **精美动画**: 20帧流畅动画，包含完整手部和挤压效果

## 依赖

- Pillow >= 10.0.0
- aiohttp >= 3.8.0
### 触发
发送命令：
```
/摸摸@用户
/摸摸
```

## 平台支持

- ✅ QQ (通过 NapCat / aiocqhttp 适配器)

## 安装

1. 将插件放入 AstrBot 的 `data/plugins/` 目录
2. 重启 AstrBot 或在插件管理页面重载插件

## 技术实现

- 使用 Pillow 库本地生成GIF动画
- 通过 QQ 官方头像 API 获取用户头像
- 监听 OneBot v11 协议的 `notice.notify.poke` 事件
- 使用 base64 编码发送图片，兼容性更好

## 支持

[AstrBot 官方文档](https://astrbot.app)

[作者的插件群](https://qun.qq.com/universal-share/share?ac=1&authKey=xXhsG4C4PGwGT%2FMJjpEcHmqfMnk4kZLFVWDt3Dy2fIM5RpRwq%2BgqgD5tLiQOESO8&busi_data=eyJncm91cENvZGUiOiIxMDc1OTIwMzIzIiwidG9rZW4iOiJQWnNVUncwMTZJOW4yRS8zYm9WeUlOaDFxQllYTUZRWEhGM0krVkxIK2I5R1RKeXVob3ZzK08zZlpIMHF4NzZQIiwidWluIjoiMTczMTUzODMzNCJ9&data=sFXAGOGNT0e_-FdLFzTnJpEO-GKcDtOnulqYzaSJ15DMYtUxWf3fd9UKekJAG0NnBWQMCTDX9Mb7-Eikfzl8gw&svctype=4&tempid=h5_group_info)
