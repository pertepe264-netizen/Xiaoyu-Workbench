// 💾 H3 续拍存档等节点的参数中文名。
//
// 为什么需要这个文件：参数配置里的 "label"（INPUT_TYPES options）在新版
// ComfyUI 前端里根本不读；官方语言包机制（locales/zh/nodeDefs.json）又
// 被 CustomNodeManager 的 lru_cache 缓存，插件热重载清不掉，必须整进程
// 重启才生效。这个扩展在控件创建那一刻直接把中文名写到 widget.label 上
// ——三条路里最直接的一条，刷新页面即生效，不依赖重启与缓存。
import { app } from "../../scripts/app.js";

const SAVE_NODE = "MiniMaxH3MotionContextSaveLatent";

const LABELS = {
  [SAVE_NODE]: {
    // 「保存路径」「本片段编号」两个控件已从节点上撤掉（各自只有一个正确
    // 取值，注册层固定成 h3_context/clip + 自动编号）—— 这里不再给它们起
    // 中文名。真要是老节点实例还残留着这两个控件，让它显英文原名，
    // 反而一眼看得出是个已经作废的旋钮。
    latent: "潜空间",
  },
  MiniMaxH3MotionContextLoadLatent: {
    latent_path: "存档路径",
    clip_index: "续写自第几段",
  },
};

app.registerExtension({
  name: "Banzhang.H3ZhLabels",
  beforeRegisterNodeDef(nodeType, nodeData) {
    const map = LABELS[nodeData?.name];
    if (map) {
      const onNodeCreated = nodeType.prototype.onNodeCreated;
      nodeType.prototype.onNodeCreated = function () {
        const r = onNodeCreated?.apply(this, arguments);
        for (const w of this.widgets || []) {
          const zh = map[w.name];
          if (zh) w.label = zh;
        }
        return r;
      };
    }

    // 💾 续拍存档：顶部状态信息面板已于 2026-10-12 移除，节点现在没有任何
    // 控件，只剩「潜空间画布 / 顺序锚」两个输入口和「潜空间」输出口。
    // 老工作流存档里的节点高度是当年被面板撑到 220 的（节点尺寸随工作流
    // 存档保存），不主动重算就会永久留一大截空白框体 —— 加载后按纯端口
    // 自然高度收回，宽度不动（用户可能手动拉宽过）。
    if (nodeData?.name === SAVE_NODE) {
      const shrinkToPorts = function () {
        const node = this;
        // 还有可见控件（极老版本残留的旋钮）就不收敛，避免把控件裁掉。
        if ((node.widgets || []).some((w) => !w.hidden)) return;
        try {
          const cs = node.computeSize?.();
          const targetH = Math.ceil((cs?.[1] || 0) + 4);
          // 40 是异常值兜底：纯端口节点自然高度本就远大于它。
          if (targetH > 40 &&
              Math.abs((node.size?.[1] || 0) - targetH) > 2) {
            node.setSize?.([node.size?.[0] || 340, targetH]);
            node.setDirtyCanvas?.(true, true);
          }
        } catch (e) { /* 布局算不出就维持原样，不强推 */ }
      };

      const prevCreated = nodeType.prototype.onNodeCreated;
      nodeType.prototype.onNodeCreated = function () {
        const r = prevCreated?.apply(this, arguments);
        shrinkToPorts.call(this);
        return r;
      };

      const prevConfigure = nodeType.prototype.onConfigure;
      nodeType.prototype.onConfigure = function () {
        const r = prevConfigure?.apply(this, arguments);
        const node = this;
        // v2 布局/字体晚就绪：多轮重算，把存档旧高度收干净。
        shrinkToPorts.call(node);
        requestAnimationFrame(() => shrinkToPorts.call(node));
        setTimeout(() => shrinkToPorts.call(node), 60);
        setTimeout(() => shrinkToPorts.call(node), 300);
        return r;
      };
    }
  },
});
