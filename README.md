# 小语的工作台

ComfyUI 自定义节点整合包，包含中文化的 H3 Studio 导演台及音视频、图像和工作流工具。此仓库是本地整合版本，不是 [CGlide 原仓库](https://github.com/CGlide/ComfyUI-CGlide)的官方发行版。

## 安装

将仓库克隆到 `ComfyUI/custom_nodes/Xiaoyu-Workbench`，按需安装 `requirements.txt` 中的依赖，并重启 ComfyUI。部分节点还需要单独安装相应的模型或可选依赖。

本仓库不包含本地缓存、模型权重、字体、音效及大型离线 wheel。相应功能需要用户自行取得这些资源并遵守原始发布条款。公开版还未包含 SelfLift 系列节点及依赖它的图片分块二采节点，因为相关上游代码未附再分发许可证。

`templates/producer_video_h3*.json` 是本地工作流模板，仍引用 SelfLift 节点；使用这些模板前需自行安装对应上游节点，或在 ComfyUI 中替换该节点。此公开副本未经过独立环境运行验证。

## 来源与许可

本包整合、改编了多个独立项目。主要来源见 [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md)。`LICENSE` 收录 GPL-3.0 文本；原组件的版权声明和许可条件仍分别适用。请勿将本仓库误认为各上游项目的官方版本。
