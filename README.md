# 游戏卡顿记录 Qt 版（v1.1.0，开发版，未发版）

这是 v1.0.0 Tkinter 版的界面升级版，记录和分析逻辑没有变，全部复用 `recorder.py`。

## 界面按什么做的

- 用户 2026-10-25 晚确认的 Apple 评审版样品：`workspace/your_files/界面样品-Qt主界面-Apple评审版.png`
- 深色模式样品：`workspace/your_files/界面样品-Qt主界面-深色.png`
- 标题栏只写程序名，页面标题是任务“记录一次卡顿”，不重名
- 下拉用系统 chevron，不用字母 v
- 录制时只留主按钮“停止并分析”，“打开记录文件夹”降级为文字链接
- 小字说明用更深的灰色，保证对比度

## 深色模式怎么工作

- 启动时先跟随系统（Qt 6.5+ 的 colorScheme，有就用，没有就默认浅色）
- 右上角“深色 / 浅色”按钮可以手动切换，手动选择会记住（QSettings）
- 深色：背景锌 900、卡片锌 800、边框锌 700、文字近白、主按钮白底黑字
- 生成的报告 HTML 也注入了 `prefers-color-scheme: dark` 样式，浏览器跟随系统深色时报告也是深色

## 运行

- 打包后：双击 `GameStutterRecorder.exe`，不需要装 Python
- 源码运行：`pip install PySide6`，然后 `python app_qt.py`
- 只分析旧文件（不用界面）：`python app_qt.py --analyze 旧文件.csv`

## 还是要说的限制（和 v1.0 一样，不藏着）

- PresentMon.exe 需要你自己从官方下载，放在程序同一文件夹，程序不会替你从不明网站下载 exe
- CPU 温度在 Windows 下不装驱动读不到，第一版不承诺，只用 CPU 占用、内存、GPU 温度功耗判断
- 结论永远是“最可能”，不是百分之百
- 程序不注入游戏，PresentMon 是 ETW 外部采集

## 打包前必须做的事（用户立的规矩）

- 互查提示词在 `互查提示词.md`，先给用户过目批准，再让另一个 AI 独立互查
- P0 修完并复跑测试通过才许打包发版
- 发版时走双处分发：GitHub Release 挂附件 + Google Drive Project/游戏卡顿记录/ 文件夹，文件名按“游戏卡顿记录-版本号.exe”
- GitHub 批量写入走浏览器网页上传，连接器只用来读和查，不承诺审批卡不再出现
