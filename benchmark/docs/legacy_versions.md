# 旧版本下载

当前发布目录只提供 proportional-200-v1 数据与新并发入口。旧版本固定保留在提交 `6ab98adeacd50dc934772a3c64f630b3709f9a24`，不依赖会移动的 main 链接：

- [浏览旧 benchmark](https://github.com/Nonlotus-eater/ami-reproduction/tree/6ab98adeacd50dc934772a3c64f630b3709f9a24/benchmark)
- [下载旧版本 ZIP](https://github.com/Nonlotus-eater/ami-reproduction/archive/6ab98adeacd50dc934772a3c64f630b3709f9a24.zip)
- [旧五 seed 清单与配置](https://github.com/Nonlotus-eater/ami-reproduction/tree/6ab98adeacd50dc934772a3c64f630b3709f9a24/benchmark/configs/reproduction)
- [旧脚本](https://github.com/Nonlotus-eater/ami-reproduction/tree/6ab98adeacd50dc934772a3c64f630b3709f9a24/benchmark/scripts)
- [旧 seed 17 阅读样例](https://github.com/Nonlotus-eater/ami-reproduction/tree/6ab98adeacd50dc934772a3c64f630b3709f9a24/benchmark/samples/seed-17)

ZIP 是该提交的整个仓库，解压后进入 benchmark/。其中包含旧五 seed ID、来源指纹、旧 runtime、脚本及 seed 17 样例，不是所有原始母库文件；重建旧五 seed 需按旧版说明下载来源。

本次只从新版发布目录删除旧文件，不删除 Git 历史，不改写历史实验结果。本地工作区旧实验依赖文件继续保留，导出白名单负责隔离发布范围。

2026-10-26 已核对该提交存在，并对相同提交的 codeload ZIP 地址执行 HEAD，返回 HTTP 200；本次没有创建或推送新 tag。
