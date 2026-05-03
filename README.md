<div align="center">

# astrbot_plugin_TaJiDuo
# 插件维护者bvzrays已跑路异环，项目只保持最低限度可用
### *TaJiDuo 异环 / 幻塔 AstrBot 插件*

[![AstrBot](https://img.shields.io/badge/AstrBot-Plugin-E8B04C?style=for-the-badge&logo=python)](https://github.com/Soulter/AstrBot)
![Version](https://img.shields.io/badge/version-1.2.0-5B7FFF?style=for-the-badge)

### 登录管理 · 社区签到 · 游戏签到 · 异环档案 · 幻塔档案 · 商城查询

</div>

---

## 目录

- [特性一览](#特性一览)
- [安装与依赖](#安装与依赖)
- [配置说明](#配置说明)
- [命令说明](#命令说明)
- [项目结构](#项目结构)
- [渲染模板](#渲染模板)
- [说明](#说明)

---

## 特性一览

- 支持 `tjd网页登录` 与手机号验证码登录。
- 支持多账号保存、主账号切换、删除、刷新。
- 支持幻塔 / 异环社区签到、游戏签到、状态查询、补签。
- 支持异环档案渲染，当前使用 `render/personal_card`。
- 支持幻塔档案渲染，当前使用 `render/huanta_record`。
- 支持塔吉多资料卡、绑定概览、商城商品、商品详情、塔吉多币与记录查询。
- 支持网页登录消息艾特发起者，并在可用平台上自动撤回登录链接消息。

---

## 安装与依赖

在 AstrBot 插件目录中放入本插件后，确保已安装 Playwright Chromium：

```bash
playwright install chromium
```

---

## 配置说明

插件默认不内置 `api_key`。请在 AstrBot 插件配置中手动填写测试 key 或你自己的正式 key。

| 配置项 | 类型 | 说明 |
| :-- | :-- | :-- |
| `base_url` | string | TaJiDuo API 服务地址 |
| `api_key` | string | TaJiDuo API Key |
| `request_timeout_ms` | number | 普通请求超时，单位毫秒 |
| `community_task_timeout_ms` | number | 社区任务总超时，单位毫秒 |
| `community_poll_interval_ms` | number | 社区任务轮询间隔，单位毫秒 |
| `render_timeout` | number | 渲染超时，单位毫秒 |
| `login_server_enabled` | bool | 是否启用网页登录壳 |
| `login_server_port` | number | 网页登录本地端口 |
| `login_server_public_link` | string | 发给用户访问的网页登录地址 |
| `action_delay_ms` | number | 社区任务动作间隔 |
| `step_delay_ms` | number | 社区任务步骤间隔 |
| `auto_community_sign_enabled` | bool | 是否启用自动社区签到 |
| `auto_community_sign_time` | string | 自动社区签到时间 |
| `auto_community_sign_notify_target` | string | 自动社区签到通知目标 |

---

## 命令说明

> 手机号验证码登录和 6 位验证码消费仅支持私聊。
> `tjd网页登录`、账号查询类命令可以在群聊使用。

### 登录与账号

| 命令 | 说明 |
| :-- | :-- |
| `tjd登录 【手机号】` | 发送验证码并等待下一条 6 位验证码 |
| `tjd网页登录` | 创建网页登录链接 |
| `tjd账号` | 查看当前账号 |
| `tjd账号列表` | 查看已保存账号列表 |
| `tjd切换账号 [序号]` | 切换主账号 |
| `tjd刷新登录` | 刷新当前登录态 |
| `tjd删除账号 [序号]` | 删除指定账号 |
| `tjd退出登录` | 清空当前 AstrBot 用户已保存账号 |
| `tjd绑定列表` | 查看塔吉多与游戏绑定概览 |
| `tjd资料` | 查看塔吉多资料卡 |

### 社区功能

| 命令 | 说明 |
| :-- | :-- |
| `tjd社区签到` | 依次执行幻塔与异环社区签到 |
| `幻塔社区签到` | 执行幻塔社区签到 |
| `异环社区签到` | 执行异环社区签到 |
| `tjd签到查询` | 查看全部社区签到任务状态 |
| `幻塔签到查询` | 查看幻塔社区签到任务状态 |
| `异环签到查询` | 查看异环社区签到任务状态 |

### 异环

| 命令 | 说明 |
| :-- | :-- |
| `异环档案 [角色]` | 渲染异环档案，使用 `render/personal_card` |
| `异环签到 [角色]` | 执行异环游戏签到 |
| `异环签到状态` | 查看异环游戏签到状态 |
| `异环补签 [角色ID]` | 执行异环单角色补签 |

### 幻塔

| 命令 | 说明 |
| :-- | :-- |
| `幻塔档案 [角色] [武器/拟态/时装/载具]` | 渲染幻塔档案 |
| `幻塔签到 [角色]` | 执行幻塔游戏签到 |
| `幻塔签到状态` | 查看幻塔游戏签到状态 |
| `幻塔补签 [角色ID]` | 执行幻塔单角色补签 |

### 商城查询

| 命令 | 说明 |
| :-- | :-- |
| `tjd兑换码` | 查看兑换码 |
| `tjd商城 [分区] [数量]` | 查看商城商品 |
| `tjd币` | 查看塔吉多币状态 |

---

## 项目结构

当前插件目录结构如下：

```text
astrbot_plugin_TaJiDuo/
├─ __init__.py                    # 插件包入口
├─ main.py                        # AstrBot 插件主入口，命令注册与业务路由
├─ metadata.yaml                  # 插件元数据
├─ _conf_schema.json              # AstrBot WebUI 配置 schema
├─ requirements.txt               # Python 依赖
├─ README.md                      # 插件说明文档
│
├─ core/                          # 核心逻辑层
│  ├─ __init__.py
│  ├─ client.py                   # TaJiDuo API 客户端封装
│  ├─ render.py                   # Playwright + Jinja 渲染器
│  ├─ storage.py                  # 本地账号与状态持久化
│  └─ web_login.py                # 单开网页登录壳与回调处理
│
├─ img/                           # 多模板复用的公共图片资源
│  ├─ bg/                         # 背景图、按钮底图、立绘等
│  ├─ bind/                       # 幻塔 / 异环绑定 UI 图标
│  ├─ ui/                         # 通用头像、图标、占位素材
│  ├─ touxiangkuang.png           # 头像框素材
│  └─ YH_UI_Share_PlatForm_Icon_qq.png
│
├─ render/                        # 按功能拆分的渲染模板目录
│  ├─ menu/                       # 帮助菜单模板
│  │  ├─ index.html
│  │  └─ style.css
│  ├─ bindings/                   # 账号绑定概览模板
│  │  ├─ index.html
│  │  ├─ style.css
│  │  └─ img/
│  │     ├─ ht_link.png
│  │     ├─ yh_link.png
│  │     └─ UI_YH_Bond_MainUI_Details_Bkg_01_cropped.png
│  ├─ signin/                     # 社区签到结果模板
│  │  ├─ index.html
│  │  └─ style.css
│  ├─ personal_card/              # 异环档案模板
│  │  ├─ index.html
│  │  ├─ style.css
│  │  └─ img/                     # 异环档案专用底图与装饰图
│  ├─ account_profile/            # 塔吉多资料卡模板
│  │  ├─ index.html
│  │  └─ style.css
│  ├─ huanta_record/              # 幻塔档案模板
│  │  ├─ index.html
│  │  └─ style.css
│  └─ choukafenxi/                # 抽卡分析模板
│     ├─ index.html
│     ├─ style.css
│     └─ img/
│        ├─ YH_UI_bg_furniture_collect_multi.png
│        └─ YH_UI_bg_furniture_collect_stretched.png
│
├─ render_cache/                  # 运行期渲染输出缓存目录
│  └─ render_*.png
│
└─ ttf/
   └─ HYWenHei-85W-1.ttf          # 渲染使用字体
```

说明：

- `main.py` 负责命令入口、账号流程、查询逻辑与渲染数据组装。
- `core/client.py` 是所有后端接口的统一封装层，新增 API 优先加在这里。
- `render/` 下按功能拆目录，避免不同页面模板和资源混在一起。
- `img/` 放跨模板复用资源；只被单一模板使用的资源优先放到对应模板目录下。
- `render_cache/` 是运行时截图缓存，不属于模板源码。

---

## 渲染模板

当前模板目录按功能分开：

```text
render/
├─ menu/              帮助菜单
├─ bindings/          账号绑定概览
├─ signin/            社区签到结果
├─ personal_card/     异环档案
├─ account_profile/   塔吉多资料卡
├─ huanta_record/     幻塔档案
└─ choukafenxi/       抽卡分析
```

---

