// ============================================================================
// 👨‍🏫 班长开发工具前端 (Banzhang Dev Utils Frontend)
// 整合自 ComfyUI-Dev-Utils
// 功能: 执行时间显示、日志控制台、重新路由、文件上传
// ============================================================================

import {api} from "../../../scripts/api.js";
import {app} from "../../../scripts/app.js";
import {$el} from "../../../scripts/ui.js";

// ============================================================================
// 1. 执行时间测量
// ============================================================================

// region: Refresh Timer
let refreshTimer = null;
// 数据变化标记：只有 refreshTable 真正写到新数据时置位，定时器才据此触发重绘，
// 避免静止期间每 100ms 无条件重绘一张根本没变的画布。
let needsRedraw = false;
// 当前正在执行的节点ID(全局)：用于本地兜底结算，以及运行期间持续触发重绘
let bzEtRunningNodeId = null;
// 重建表格的节流窗口(毫秒)：大工作流每个节点都整表重建会卡，攒够一拍再画
const BZ_ET_REFRESH_INTERVAL = 150;
let bzEtRefreshTimer = null;
let bzEtLastRefreshAt = 0;

function stopRefreshTimer() {
    if (!refreshTimer) {
        return;
    }
    clearInterval(refreshTimer);
    refreshTimer = null;
}

function startRefreshTimer() {
    stopRefreshTimer();
    refreshTimer = setInterval(function () {
        // 有数据变化、或有节点正在执行(需要实时跳动计时)时才重绘
        if (needsRedraw || bzEtRunningNodeId != null) {
            needsRedraw = false;
            app.graph.setDirtyCanvas(true, false);
        }
    }, 100);
}
// endregion

// 时间显示规则：10秒内保留1位小数，超过则取整到秒，超过1分钟显示分+秒
function formatExecutionTime(time) {
    if (time == null) {
        return "";
    }
    const seconds = time / 1000.0;
    if (seconds < 10) {
        return `${seconds.toFixed(1)}秒`;
    }
    const rounded = Math.round(seconds);
    if (rounded >= 60) {
        const minutes = Math.floor(rounded / 60);
        const remainingSeconds = rounded % 60;
        if (minutes >= 60) {
            const hours = Math.floor(minutes / 60);
            return `${hours}小时${minutes % 60}分${remainingSeconds}秒`;
        }
        return `${minutes}分${remainingSeconds}秒`;
    }
    return `${rounded}秒`;
}

function formatBytes(bytes, decimals) {
    if (bytes === 0) {
        return '0 B';
    }
    const k = 1024,
        dm = decimals || 2,
        sizes = ['字节', 'KB', 'MB', 'GB', 'TB', 'PB', 'EB', 'ZB', 'YB'],
        i = Math.floor(Math.log(bytes) / Math.log(k));
    return parseFloat((bytes / Math.pow(k, i)).toFixed(dm)) + ' ' + sizes[i];
}

function drawBadge(node, orig, restArgs) {
    let ctx = restArgs[0];
    const r = orig?.apply?.(node, restArgs);

    if (!node.flags.collapsed && node.constructor.title_mode != LiteGraph.NO_TITLE) {
        let text = "";
        if (node.bz_et_execution_time !== undefined) {
            text = formatExecutionTime(node.bz_et_execution_time);
            if (node.bz_et_vram_used !== undefined) {
                text += " - 显存 " + formatBytes(node.bz_et_vram_used, 2);
            }
        } else if (node.bz_et_start_time !== undefined) {
            text = formatExecutionTime(LiteGraph.getTime() - node.bz_et_start_time);
        }
        if (!text) {
            return;
        }
        let fgColor = "white";
        let bgColor = "#0F1F0F";
        let visible = true;

        ctx.save();
        ctx.font = "12px sans-serif";
        const textSize = ctx.measureText(text);
        ctx.fillStyle = bgColor;
        ctx.beginPath();
        const paddingHorizontal = 6;
        ctx.roundRect(0, -LiteGraph.NODE_TITLE_HEIGHT - 20, textSize.width + paddingHorizontal * 2, 20, 5);
        ctx.fill();

        ctx.fillStyle = fgColor;
        ctx.fillText(text, paddingHorizontal, -LiteGraph.NODE_TITLE_HEIGHT - paddingHorizontal);
        ctx.restore();
    }
    return r;
}

async function unloadModelsAndFreeMemory() {
    let res = await api.fetchApi(`/free`, {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: '{"unload_models": true, "free_memory": true}'
    });

    if (res.status === 200) {
        app.ui.dialog.show('<span style="color: green;">卸载模型并释放内存成功</span>');
    } else {
        app.ui.dialog.show('[错误] 卸载模型并释放内存失败');
    }
    app.ui.dialog.element.style.zIndex = 10010;
}

// 视频合并节点类型：阶段模式只统计这些节点（班长版 + VHS 版）
const BZ_ET_VIDEO_COMBINE_TYPES = ["banzhangVideoCombine", "VHS_VideoCombine"];

function isVideoCombineNode(node) {
    return !!node && BZ_ET_VIDEO_COMBINE_TYPES.includes(node.type);
}

function setupClearExecutionCacheMenu() {
    // 新版 ComfyUI 前端侧栏类名已从 .comfy-menu 改成 .comfyui-menu，
    // 两个都找不到时直接放弃加按钮，不能让这里抛错——否则后面的监听全注册不上
    const menu = document.querySelector(".comfyui-menu") || document.querySelector(".comfy-menu");
    if (!menu) {
        return;
    }
    const freeButton = document.createElement("button");
    freeButton.textContent = "清除执行缓存";
    freeButton.onclick = async () => {
        await unloadModelsAndFreeMemory();
    };
    menu.append(freeButton);
}

let lastRunningDate = null;
let runningData = null;

function exportTable(table, separator = ',', filename) {
    var rows = table.querySelectorAll('tr');
    var csv = [];
    for (var i = 0; i < rows.length; i++) {
        var row = [], cols = rows[i].querySelectorAll('td, th');
        for (var j = 0; j < cols.length; j++) {
            var data = cols[j].innerText.replace(/(\r\n|\n|\r)/gm, '').replace(/(\s\s)/gm, ' ');
            data = data.replace(/"/g, '""');
            row.push('"' + data + '"');
        }
        csv.push(row.join(separator));
    }
    var csv_string = csv.join('\n');
    // 日期用 2026-09-24 这种格式：toLocaleDateString 在中文系统会带斜杠，文件名非法
    const stamp = new Date();
    const day = `${stamp.getFullYear()}-${String(stamp.getMonth() + 1).padStart(2, '0')}-${String(stamp.getDate()).padStart(2, '0')}`;
    filename = filename || ('执行时间_' + day + '.csv');
    var link = document.createElement('a');
    link.style.display = 'none';
    link.setAttribute('target', '_blank');
    // 加 BOM，否则 Excel 打开中文会乱码
    link.setAttribute('href', 'data:text/csv;charset=utf-8,' + encodeURIComponent('\ufeff' + csv_string));
    link.setAttribute('download', filename);
    document.body.appendChild(link);
    link.click();
    document.body.removeChild(link);
}

function getEtOptions() {
    const opts = {
        hide_zero_time: false,
        hide_zero_vram: false,
        sort_option: "节点执行顺序",
        show_mode: "全部节点"
    };
    app.graph._nodes.forEach(function (node) {
        if (node.comfyClass === "BZ_ExecutionTime") {
            opts.hide_zero_time = node.hide_zero_time || false;
            opts.hide_zero_vram = node.hide_zero_vram || false;
            opts.sort_option = node.sort_option || "none";
            opts.show_mode = node.show_mode || "all";
        }
    });
    return opts;
}

function makeTableShell(headers) {
    const headerThStyle = {"white-space": "nowrap"};
    const tableBody = $el("tbody");
    const tableFooter = $el("tfoot", {style: {"background": "var(--comfy-input-bg)"}});
    const table = $el("table", {
        textAlign: "right",
        border: "1px solid var(--border-color)",
        style: {"border": "none", "border-spacing": "0", "font-size": "14px", "width": "100%"}
    }, [
        $el("thead", {style: {"background": "var(--comfy-input-bg)"}}, [
            $el("tr", headers.map(function (h) {
                return $el("th", {style: headerThStyle, "textContent": h});
            }))
        ]),
        tableBody,
        tableFooter
    ]);
    return {table: table, tableBody: tableBody, tableFooter: tableFooter};
}

function cellStyle(extra) {
    return Object.assign({"textAlign": "right"}, extra || {});
}

// 缓存命中的节点本轮不会重跑，沿用上一轮数值并标灰加注
function staleSuffix(item) {
    return item.stale ? " (缓存)" : "";
}

function staleOpacity(item) {
    return item.stale ? 0.55 : 1;
}

function diffMark(current, pre) {
    let diffText;
    let diffColor;
    if (pre) {
        const diffTime = current - pre;
        const diffPercentText = `${(diffTime * 100 / pre).toFixed(2)}%`;
        if (diffTime > 0) {
            diffColor = 'red';
            diffText = `+${formatExecutionTime(diffTime)} / +${diffPercentText}`;
        } else if (diffPercentText === '0.00%') {
            diffColor = 'white';
            diffText = formatExecutionTime(diffTime);
        } else {
            diffColor = 'green';
            diffText = `${formatExecutionTime(diffTime)} / ${diffPercentText}`;
        }
    }
    return [diffColor, diffText];
}

function buildTableHtml() {
    const opts = getEtOptions();
    if (opts.show_mode === "仅视频合并阶段") {
        return buildStageTableHtml(opts);
    }
    return buildAllTableHtml(opts);
}

function buildAllTableHtml(opts) {
    const shell = makeTableShell(
        ["节点ID", "节点标题", "当前时间", "累计耗时", "上次时间", "当前/上次差异", "显存使用"]);
    const table = shell.table;
    if (runningData?.nodes_execution_time === undefined) {
        return table;
    }

    let max_execution_time = null;
    let max_vram_used = null;
    let sortedData = [...runningData.nodes_execution_time];

    switch (opts.sort_option) {
        case "时长从大到小":
            sortedData.sort((a, b) => b.execution_time - a.execution_time);
            break;
        case "时长从小到大":
            sortedData.sort((a, b) => a.execution_time - b.execution_time);
            break;
        case "显存从大到小":
            sortedData.sort((a, b) => b.vram_used - a.vram_used);
            break;
        case "显存从小到大":
            sortedData.sort((a, b) => a.vram_used - b.vram_used);
            break;
        default:
            break;
    }

    sortedData.forEach(function (item) {
        if (opts.hide_zero_time && item.execution_time < 1) {
            return;
        }
        if (opts.hide_zero_vram && item.vram_used === 0) {
            return;
        }

        const nodeId = item.node;
        const node = app.graph.getNodeById(nodeId);
        const title = node?.title ?? nodeId;
        const preExecutionTime = lastRunningDate?.nodes_execution_time?.find(x => x.node === nodeId)?.execution_time;
        const [diffColor, diffText] = diffMark(item.execution_time, preExecutionTime);

        if (max_execution_time == null || item.execution_time > max_execution_time) {
            max_execution_time = item.execution_time;
        }
        if (max_vram_used == null || item.vram_used > max_vram_used) {
            max_vram_used = item.vram_used;
        }

        shell.tableBody.append($el("tr", {
            style: {"cursor": "pointer", "opacity": staleOpacity(item)},
            onclick: () => {
                if (node) {
                    app.canvas.selectNode(node, false);
                }
            }
        }, [
            $el("td", {style: cellStyle(), "textContent": String(nodeId)}),
            $el("td", {style: cellStyle(), "textContent": title}),
            $el("td", {style: cellStyle(), "textContent": formatExecutionTime(item.execution_time) + staleSuffix(item)}),
            $el("td", {
                style: cellStyle(),
                "textContent": item.cumulative_time != null ? formatExecutionTime(item.cumulative_time) : ""
            }),
            $el("td", {
                style: cellStyle(),
                "textContent": preExecutionTime !== undefined ? formatExecutionTime(preExecutionTime) : undefined
            }),
            $el("td", {style: cellStyle({"color": diffColor}), "textContent": diffText}),
            $el("td", {style: cellStyle(), "textContent": formatBytes(item.vram_used, 2)}),
        ]));
    });

    if (runningData.total_execution_time != null) {
        const [diffColor, diffText] = diffMark(runningData.total_execution_time, lastRunningDate?.total_execution_time);

        shell.tableFooter.append($el("tr", [
            $el("td", {style: cellStyle(), "textContent": '最大值'}),
            $el("td", {style: cellStyle(), "textContent": ''}),
            $el("td", {
                style: cellStyle(),
                "textContent": max_execution_time != null ? formatExecutionTime(max_execution_time) : ''
            }),
            $el("td", {style: cellStyle(), "textContent": ''}),
            $el("td", {style: cellStyle(), "textContent": ''}),
            $el("td", {style: cellStyle({"color": diffColor}), "textContent": ''}),
            $el("td", {
                style: cellStyle(),
                "textContent": max_vram_used != null ? formatBytes(max_vram_used, 2) : ''
            }),
        ]));

        shell.tableFooter.append($el("tr", [
            $el("td", {style: cellStyle(), "textContent": '总计'}),
            $el("td", {style: cellStyle(), "textContent": ''}),
            $el("td", {style: cellStyle(), "textContent": formatExecutionTime(runningData.total_execution_time)}),
            $el("td", {style: cellStyle(), "textContent": ''}),
            $el("td", {
                style: cellStyle(),
                "textContent": lastRunningDate?.total_execution_time ? formatExecutionTime(lastRunningDate?.total_execution_time) : undefined
            }),
            $el("td", {style: cellStyle({"color": diffColor}), "textContent": diffText}),
            $el("td", {style: cellStyle(), "textContent": ""}),
        ]));
    }
    return table;
}

function buildStageTableHtml(opts) {
    const shell = makeTableShell(["阶段", "视频合并节点", "累计耗时", "本阶段耗时", "显存使用"]);
    const table = shell.table;
    if (runningData?.nodes_execution_time === undefined) {
        return table;
    }

    const rows = runningData.nodes_execution_time
        .filter(function (item) {
            return isVideoCombineNode(app.graph.getNodeById(item.node));
        })
        .filter(function (item) {
            return item.cumulative_time != null;
        })
        .sort((a, b) => a.cumulative_time - b.cumulative_time);

    if (rows.length === 0) {
        shell.tableBody.append($el("tr", [
            $el("td", {
                style: {"textAlign": "center", "opacity": 0.7},
                colspan: 5,
                "textContent": "本轮还没有视频合并节点跑完"
            })
        ]));
        return table;
    }

    let prevCumulative = 0;
    rows.forEach(function (item, index) {
        const node = app.graph.getNodeById(item.node);
        const name = node?.title || ("#" + item.node);
        // 本阶段耗时 = 本节点累计 - 上一个视频合并节点的累计
        const stageTime = Math.max(0, item.cumulative_time - prevCumulative);
        prevCumulative = item.cumulative_time;

        shell.tableBody.append($el("tr", {
            style: {"cursor": "pointer", "opacity": staleOpacity(item)},
            onclick: () => {
                if (node) {
                    app.canvas.selectNode(node, false);
                }
            }
        }, [
            $el("td", {style: cellStyle(), "textContent": `第${index + 1}段`}),
            $el("td", {style: cellStyle(), "textContent": name + staleSuffix(item)}),
            $el("td", {style: cellStyle(), "textContent": formatExecutionTime(item.cumulative_time)}),
            $el("td", {style: cellStyle(), "textContent": formatExecutionTime(stageTime)}),
            $el("td", {style: cellStyle(), "textContent": formatBytes(item.vram_used, 2)}),
        ]));
    });

    if (runningData.total_execution_time != null) {
        shell.tableFooter.append($el("tr", [
            $el("td", {style: cellStyle(), "textContent": '全片'}),
            $el("td", {style: cellStyle(), "textContent": `共${rows.length}段`}),
            $el("td", {style: cellStyle(), "textContent": formatExecutionTime(runningData.total_execution_time)}),
            $el("td", {style: cellStyle(), "textContent": ''}),
            $el("td", {style: cellStyle(), "textContent": ''}),
        ]));
    }
    return table;
}

function refreshTable(force) {
    const now = (typeof performance !== "undefined") ? performance.now() : Date.now();
    if (!force && now - bzEtLastRefreshAt < BZ_ET_REFRESH_INTERVAL) {
        if (bzEtRefreshTimer) {
            return;
        }
        bzEtRefreshTimer = setTimeout(function () {
            bzEtRefreshTimer = null;
            refreshTable(true);
        }, BZ_ET_REFRESH_INTERVAL - (now - bzEtLastRefreshAt));
        return;
    }
    bzEtLastRefreshAt = now;
    needsRedraw = true; // 数据/尺寸真正变化，下一拍据此重绘
    app.graph._nodes.forEach(function (node) {
        if (node.comfyClass === "BZ_ExecutionTime" && node.widgets) {
            const tableWidget = node.widgets.find((w) => w.name === "Table");
            if (!tableWidget) {
                return;
            }
            try {
                // 表格宿主 = DOM widget 容器的第一个子元素
                const host = tableWidget.element?.firstElementChild;
                if (host) {
                    const newTable = buildTableHtml();
                    if (host.firstChild) {
                        host.replaceChild(newTable, host.firstChild);
                    } else {
                        host.appendChild(newTable);
                    }
                }
            } catch (e) {
                console.error("[Banzhang-ExecutionTime] 刷新表格出错:", e);
            }

            app.graph.setDirtyCanvas(true);
        }
    });
}


app.registerExtension({
    name: "Banzhang-DevUtils.ExecutionTime",
    async setup() {
        setupClearExecutionCacheMenu();

        // 用于在未收到后端 executed 事件时本地兜底结算耗时
        function finalizeRunningNode() {
            if (bzEtRunningNodeId == null) {
                return;
            }
            const node = app.graph.getNodeById(bzEtRunningNodeId);
            if (node && node.bz_et_start_time !== undefined && node.bz_et_execution_time === undefined) {
                // 后端事件未送达时的兜底：用本地时钟冻结该节点耗时
                node.bz_et_execution_time = LiteGraph.getTime() - node.bz_et_start_time;
                if (node.bz_et_vram_used === undefined) {
                    node.bz_et_vram_used = 0;
                }
            }
            bzEtRunningNodeId = null;
        }

        api.addEventListener("executing", ({detail}) => {
            // 兼容新版 ComfyUI: executing payload 为 {node, display_node, prompt_id} 字典
            const nodeId = (detail && typeof detail === "object") ? detail.node : detail;
            // 下一个节点开始(或 nodeId 为空=流程结束)时，先结算上一个节点
            finalizeRunningNode();
            app.graph.setDirtyCanvas(true, false);
            if (!nodeId) {
                return;
            }
            const node = app.graph.getNodeById(nodeId);
            if (node) {
                // 该节点重新运行：清除上一次的结果，重新开始计时
                delete node.bz_et_execution_time;
                delete node.bz_et_vram_used;
                delete node.bz_et_cumulative_time;
                delete node.bz_et_formatted_cumulative_time;
                node.bz_et_start_time = LiteGraph.getTime();
                // 执行到该节点后才显示，并一直保留到下一次点击运行
                bzEtRunningNodeId = nodeId;
            }
        });

        api.addEventListener("Banzhang-DevUtils.ExecutionTime.executed", ({detail}) => {
            const node = app.graph.getNodeById(detail.node);
            if (node) {
                node.bz_et_execution_time = detail.execution_time;
                node.bz_et_vram_used = detail.vram_used;
                node.bz_et_cumulative_time = detail.cumulative_time;
                node.bz_et_formatted_cumulative_time = detail.formatted_cumulative_time;
            }
            if (!runningData) {
                runningData = {
                    nodes_execution_time: [],
                    total_execution_time: null
                };
            }
            if (!runningData.nodes_execution_time) {
                runningData.nodes_execution_time = [];
            }
            const index = runningData.nodes_execution_time.findIndex(x => x.node === detail.node);
            const data = {
                node: detail.node,
                class_type: detail.class_type,
                execution_time: detail.execution_time,
                cumulative_time: detail.cumulative_time,
                vram_used: detail.vram_used,
                stale: false
            };
            if (index >= 0) {
                runningData.nodes_execution_time[index] = data;
            } else {
                runningData.nodes_execution_time.push(data);
            }
            refreshTable();
        });

        api.addEventListener("execution_start", ({detail}) => {
            // 仅归档上一轮完整结束(收到 execution_end)的数据，避免用中断的半截
            // 数据覆盖"上次时间"列；统计表本身每轮都重建
            if (runningData && runningData.total_execution_time != null) {
                lastRunningDate = runningData;
            }
            // 新一轮开始：清空所有节点上的单次耗时/显存显示，之后只有在本次
            // 真正执行到的节点上才会重新出现。
            // 注意：累计耗时(cumulative_time)不在此清空——固定种子导致缓存命中时，
            // 被整段跳过的节点收不到 executed 事件，若在此清空则值会被删掉而无人重新赋值，
            // 详情面板的「累计耗时」行会因值为空而不渲染。跳过的节点应保留上次累计值；
            // 真正重新运行的节点会在 executing 事件中清空并在 executed 中重新赋值。
            bzEtRunningNodeId = null;
            app.graph._nodes.forEach(function (node) {
                delete node.bz_et_start_time;
                delete node.bz_et_execution_time;
                delete node.bz_et_vram_used;
            });
            app.graph.setDirtyCanvas(true, false);
            // 缓存命中的节点本轮不会重跑、收不到 executed 事件。这里沿用上一轮的记录
            // 并标记 stale（渲染时标灰加「缓存」），而不是让整行凭空消失。
            const carried = (runningData?.nodes_execution_time || []).map(function (item) {
                return {...item, stale: true};
            });
            runningData = {
                nodes_execution_time: carried,
                total_execution_time: null
            };
            startRefreshTimer();
        });

        api.addEventListener("Banzhang-DevUtils.ExecutionTime.execution_end", ({detail}) => {
            finalizeRunningNode();
            app.graph.setDirtyCanvas(true, false);
            stopRefreshTimer();
            if (runningData) {
                runningData.total_execution_time = detail.execution_time;
            }
            refreshTable(true);
        });

        // 中断或报错时也要收尾，否则刷新定时器会一直空转
        ["execution_interrupted", "execution_error"].forEach(function (evt) {
            api.addEventListener(evt, () => {
                finalizeRunningNode();
                stopRefreshTimer();
                refreshTable(true);
            });
        });
    },
    async nodeCreated(node, app) {
        if (!node.bz_et_swizzled) {
            let orig = node.onDrawForeground;
            if (!orig) {
                orig = node.__proto__.onDrawForeground;
            }
            node.onDrawForeground = function (ctx) {
                drawBadge(node, orig, arguments);
            };
            node.bz_et_swizzled = true;
        }
    },
    async loadedGraphNode(node, app) {
        if (!node.bz_et_swizzled) {
            const orig = node.onDrawForeground;
            node.onDrawForeground = function (ctx) {
                drawBadge(node, orig, arguments);
            };
            node.bz_et_swizzled = true;
        }
    },
    async beforeRegisterNodeDef(nodeType, nodeData, app) {
        if (nodeType.comfyClass === "BZ_ExecutionTime") {
            // 布局完全交给 ComfyUI 官方 DOM widget 契约（对齐视频合并节点的做法）：
            // 手写 computeSize/onResize 反而会和官方 #arrangeWidgets 打架，
            // 所以这里不做任何尺寸覆盖，弹性分配由表格 widget 的 computeLayoutSize 声明。

            const nodeCreated = nodeType.prototype.onNodeCreated;
            nodeType.prototype.onNodeCreated = function () {
                nodeCreated?.apply(this, arguments);

                this.sort_option = "节点执行顺序";
                this.show_mode = "全部节点";

                // 选项值直接用中文：新版前端的 combo 不读 labels，只显示 values
                this.addWidget("combo", "显示模式", this.show_mode, (value) => {
                    this.show_mode = value;
                    refreshTable(true);
                }, {
                    values: ["全部节点", "仅视频合并阶段"]
                });

                // 用「开/关」下拉代替 toggle：新版前端把布尔值渲染成 true/false，
                // 换成 combo 才能显示中文
                this.hide_zero_time = false;
                this.addWidget("combo", "隐藏0秒时长", "关", (value) => {
                    this.hide_zero_time = (value === "开");
                    refreshTable(true);
                }, {
                    values: ["开", "关"]
                });

                this.hide_zero_vram = false;
                this.addWidget("combo", "隐藏0显存占用", "关", (value) => {
                    this.hide_zero_vram = (value === "开");
                    refreshTable(true);
                }, {
                    values: ["开", "关"]
                });

                this.addWidget("combo", "排序方式", this.sort_option, (value) => {
                    this.sort_option = value;
                    refreshTable(true);
                }, {
                    values: ["节点执行顺序", "时长从大到小", "时长从小到大", "显存从大到小", "显存从小到大"]
                });

                this.addWidget("button", "导出表格", null, () => {
                    const w = this.widgets.find((x) => x.name === "Table");
                    const tbl = w?.element?.querySelector("table");
                    if (!tbl) {
                        return;
                    }
                    const suffix = this.show_mode === "仅视频合并阶段" ? "阶段耗时" : "执行时间";
                    const d = new Date();
                    const day = `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, '0')}-${String(d.getDate()).padStart(2, '0')}`;
                    exportTable(tbl, ',', `${suffix}_${day}.csv`);
                });

                this.addWidget("button", "重置尺寸", null, () => {
                    // 宽度回到默认值，高度交给官方弹性布局重新分配
                    this.size[0] = 880;
                    this.setDirtyCanvas(true, true);
                    refreshTable(true);
                });

                // 官方 DOM widget：定位、缩放、平移、裁剪全部由前端布局系统接管。
                // 之前手写 canvas 坐标变换 + 把元素挂到 document.body，画布一缩放/
                // 一平移就必然错位，这是错位的根本原因。
                const container = document.createElement("div");
                container.style = "width:100%; height:100%; display:flex; flex-direction:column;"
                    + " overflow:hidden; box-sizing:border-box; background:var(--comfy-input-bg);"
                    + " border:1px solid var(--border-color); border-radius:4px;";

                const tableHost = document.createElement("div");
                // flex:1 + min-height:0 → 表格填满容器剩余空间，内容多时自己滚动
                tableHost.style = "flex:1 1 auto; min-height:0; overflow:auto; width:100%;";
                container.appendChild(tableHost);

                const tableWidget = this.addDOMWidget("Table", "table", container);
                // 弹性布局契约（照搬视频合并节点验证过的写法）：
                //   minHeight/minWidth 必须是常量，绝不能带 node.size ——
                //   官方拖拽时每帧拿节点最小尺寸当下限钳制，报动态值会让节点越拖越大。
                //   空间不足 → 保底 160px 并把节点撑高；空间富余 → 表格跟着节点一起变大。
                tableWidget.computeLayoutSize = () => ({
                    minHeight: 160,
                    minWidth: 0,
                    maxHeight: 1e6,
                    maxWidth: 1e6
                });

                this.serialize_widgets = false;
                this.isVirtualNode = true;

                tableHost.appendChild(buildTableHtml());
                // 表格有 7 列，给个合适的起始宽度；之后用户想拉宽拉窄都跟随
                this.size[0] = Math.max(this.size[0] || 0, 880);
            };
        }
    }
});


// ============================================================================
// 2. 日志控制台
// ============================================================================

function addScript(src) {
    $el("script", {
        parent: document.head,
        src: src
    });
}

function addCss(src) {
    $el("link", {
        parent: document.head,
        rel: "stylesheet",
        type: "text/css",
        href: src
    });
}

const getValue = (key, defaultValue) => {
    const storageKey = localStorage.getItem("Banzhang-DevUtils.LogConsole." + key);
    if (storageKey && !isNaN(+storageKey)) {
        return storageKey;
    }
    return defaultValue;
};

const setValue = (key, value) => {
    localStorage.setItem("Banzhang-DevUtils.LogConsole." + key, value);
};

let logConsoleEnabled = true;

function generateUUID() {
    var d = new Date().getTime();
    var d2 = ((typeof performance !== 'undefined') && performance.now && (performance.now() * 1000)) || 0;
    return 'xxxxxxxx-xxxx-4xxx-yxxx-xxxxxxxxxxxx'.replace(/[xy]/g, function (c) {
        var r = Math.random() * 16;
        if (d > 0) {
            r = (d + r) % 16 | 0;
            d = Math.floor(d / 16);
        } else {
            r = (d2 + r) % 16 | 0;
            d2 = Math.floor(d2 / 16);
        }
        return (c === 'x' ? r : (r & 0x3 | 0x8)).toString(16);
    });
}

let consoleId = generateUUID();

app.registerExtension({
    name: "Banzhang-DevUtils.LogConsole",
    eventSource: null,
    terminal: null,

    async setup() {
        const showButton = $el("button.comfy-settings-btn", {
            style: {
                right: "16px",
                cursor: "pointer"
            },
        }, [
            $el("img", {
                src: new URL("images/terminal@2x.png", import.meta.url).toString(),
                style: {
                    marginTop: "2px",
                    width: "14px",
                    height: "14px"
                }
            })
        ]);

        document.querySelector(".comfy-settings-btn").after(showButton);

        addScript(new URL("vendor/xterm.min.js", import.meta.url).toString());
        addCss(new URL("vendor/xterm.min.css", import.meta.url).toString());
        addScript(new URL("vendor/xterm-addon-fit.min.js", import.meta.url).toString());
        addScript(new URL("vendor/interact.min.js", import.meta.url).toString());

        addCss(new URL("banzhang_dev_utils.css", import.meta.url).toString());

        const consoleElem = $el('div');
        consoleElem.id = 'banzhang-devutils-log-console';

        const collapseButton = $el("button.expand-button", [
            $el("div.arrow.arrow-down", [$el("i.arrow-icon")])
        ]);
        const closeButton = $el("button.comfy-close-menu-btn", {textContent: '×'});
        const clearButton = $el("button", {
            textContent: '清空',
            style: {fontSize: '14px'},
            onclick: () => {
                this.terminal?.clear();
            }
        });
        const consoleMenuContainer = $el("div.banzhang-devutils-log-console-menu-container", [
            clearButton,
            closeButton
        ]);
        const stateElem = $el("strong");
        stateElem.id = "banzhang-devutils-log-console-state";
        const consoleIdElem = $el("div", {
            textContent: `ID: ${consoleId}`,
            style: {
                fontSize: '12px',
                marginLeft: '8px',
                lineHeight: '20px',
                color: 'var(--descrip-text)'
            }
        });
        const consoleStateElem = $el("div", {style: {display: 'flex', flexDirection: 'row'}}, [
            stateElem,
            consoleIdElem
        ]);
        const headerElem = $el("div.banzhang-devutils-log-console-control", [
            consoleStateElem,
            collapseButton,
            consoleMenuContainer
        ]);
        const containerElem = $el("div.banzhang-devutils-log-console-container", [
            headerElem,
            consoleElem
        ]);
        document.body.append(containerElem);

        const defaultHeight = 240;
        const controlHeight = 20;

        const setPanelCollapsed = (collapsed) => {
            if (collapsed) {
                collapseButton.firstChild.className = "arrow arrow-up";
            } else {
                collapseButton.firstChild.className = "arrow arrow-down";
            }
            containerElem.style.height = `${collapsed ? controlHeight : defaultHeight}px`;
            containerElem.setAttribute('data-y', collapsed ? defaultHeight - controlHeight : 0);
            setValue("Collapsed", collapsed ? '1' : '0');
        };

        collapseButton.onclick = () => {
            const toCollapsed = !collapseButton.firstChild.className.includes("arrow-up");
            setPanelCollapsed(toCollapsed);
        };

        interact(containerElem)
            .resizable({
                edges: {left: false, right: false, bottom: false, top: true},
                listeners: {
                    move(event) {
                        var target = event.target;
                        var y = parseFloat(target.getAttribute('data-y')) || 0;
                        target.style.height = event.rect.height + 'px';
                        y += event.deltaRect.top;
                        target.setAttribute('data-y', y);
                        event.preventDefault();
                    }
                },
                modifiers: [
                    interact.modifiers.restrictEdges({outer: 'parent'}),
                    interact.modifiers.restrictSize({min: {height: controlHeight}})
                ],
                inertia: true
            });

        const collapsed = getValue('Collapsed', '0') === '1';
        setPanelCollapsed(collapsed);

        const setConsoleVisible = (visible) => {
            setValue('Visible', visible ? '1' : '0');
            containerElem.hidden = !visible || !logConsoleEnabled;
            showButton.hidden = visible || !logConsoleEnabled;
        };

        this.setupTerminal(containerElem, consoleElem);

        showButton.onclick = () => {
            setConsoleVisible(!(getValue('Visible', '1') === '1'));
        };
        closeButton.onclick = () => {
            setConsoleVisible(false);
        };

        api.addEventListener("reconnected", () => {
            if (this.eventSource) {
                this.startSSE();
            }
        });

        const onEnabledChange = (value) => {
            logConsoleEnabled = value;
            if (value) {
                this.startSSE();
            } else {
                this.stopSSE();
            }
            const visible = getValue('Visible', '1') === '1';
            setConsoleVisible(visible);

            if (!value) {
                api.fetchApi(`/banzhang-dev-utils/disable-log?console_id=${consoleId}&client_id=${api.clientId}`, {
                    method: "POST"
                });
            }
        };

        logConsoleEnabled = app.ui.settings.addSetting({
            id: "Banzhang-DevUtils.LogConsole.Enabled",
            name: "班长日志控制台",
            type: "boolean",
            defaultValue: true,
            onChange: onEnabledChange
        });
    },
    setupTerminal(containerElem, consoleElem) {
        if (this.terminal) {
            return;
        }
        this.terminal = new Terminal({
            convertEol: true,
        });
        const terminal = this.terminal;
        this.terminal.attachCustomKeyEventHandler((e) => {
            if (e.ctrlKey && e.keyCode === 76) {
                terminal.clear();
                return false;
            }
        });
        const fitAddon = new FitAddon.FitAddon();
        this.terminal.loadAddon(fitAddon);
        this.terminal.open(consoleElem);
        fitAddon.fit();

        const resizeObserver = new ResizeObserver(function (entries) {
            try {
                fitAddon && fitAddon.fit();
            } catch (err) {
                console.log(err);
            }
        });
        resizeObserver.observe(containerElem);
    },
    startSSE() {
        if ([EventSource.OPEN, EventSource.CONNECTING].includes(this.eventSource?.readyState)) {
            return;
        }
        // [临时实验 20261013] 不自动建立 SSE，验证连接池元凶
        console.log("[SSE-实验] 本次跳过 startSSE 自动建立");
        return;
        const logSSEUrl = api.apiURL(`/banzhang-dev-utils/log?console_id=${consoleId}&client_id=${api.clientId}`);
        this.eventSource = new EventSource(logSSEUrl);
        this.eventSource.onopen = () => {
            this.setSSEState(this.eventSource.readyState);
        };
        this.eventSource.onerror = (error) => {
            this.eventSource.close();
            this.setSSEState(this.eventSource.readyState);
        };

        const messageHandler = (event) => {
            this.terminal?.write(event.data);
        };
        this.eventSource.addEventListener("message", messageHandler);
    },
    stopSSE() {
        this.eventSource?.close();
        this.setSSEState(EventSource.CLOSED);
        this.eventSource = null;
    },
    setSSEState(state) {
        const stateElem = document.getElementById("banzhang-devutils-log-console-state");
        if (stateElem) {
            let stateTextColor;
            let stateText;
            if (state === EventSource.OPEN) {
                stateTextColor = "green";
                stateText = "已连接";
            } else if (state === EventSource.CONNECTING) {
                stateTextColor = "gold";
                stateText = "连接中";
            } else if (state === EventSource.CLOSED) {
                stateTextColor = 'red';
                stateText = "已断开";
            }
            stateElem.style.color = stateTextColor;
            stateElem.innerText = stateText;
        }
    }
});


// ============================================================================
// 3. 重新路由功能
// ============================================================================

app.registerExtension({
    name: "Banzhang-DevUtils.Reroute",
    addRerouteMenu(nodeType) {
        const getSlotMenuOptions = nodeType.prototype.getSlotMenuOptions;
        nodeType.prototype.getSlotMenuOptions = function (slot) {
            var menuInfo = [];
            if (getSlotMenuOptions) {
                menuInfo = getSlotMenuOptions.apply(this, [slot]);
            } else {
                if (slot && slot.output && slot.output.links && slot.output.links.length) {
                    menuInfo.push({content: "断开连接", slot: slot});
                }
                var _slot = slot.input || slot.output;
                if (_slot.removable) {
                    menuInfo.push(
                        _slot.locked ? "无法移除" : {content: "移除插槽", slot: slot}
                    );
                }
                if (!_slot.nameLocked) {
                    menuInfo.push({content: "重命名插槽", slot: slot});
                }
            }

            if (slot && slot.output && slot.output.links && slot.output.links.length) {
                menuInfo.push({
                    content: '重新路由',
                    slot: slot,
                    callback: (value, options, event, parentMenu, node) => {
                        const slot = value.slot;
                        node.graph.beforeChange();
                        if (slot && slot.output && slot.output.links && slot.output.links.length) {
                            const rerouteNode = LiteGraph.createNode("Reroute");
                            app.graph.add(rerouteNode);
                            rerouteNode.pos = [
                                node.pos[0] + node.size[0] + 40,
                                node.pos[1]
                            ];
                            app.canvas.selectNode(rerouteNode, false);
                            const linkInfos = [];
                            for (var i = 0, l = slot.output.links.length; i < l; i++) {
                                var linkId = slot.output.links[i];
                                var linkInfo = node.graph.links[linkId];
                                if (!linkInfo) {
                                    continue;
                                }
                                linkInfos.push(linkInfo);
                            }
                            linkInfos.forEach(function (linkInfo) {
                                var targetNode = node.graph.getNodeById(linkInfo.target_id);
                                rerouteNode.connect(0, targetNode, linkInfo.target_slot);
                            });
                            node.connect(slot.slot, rerouteNode, 0);
                        }
                        node.graph.afterChange();
                        node.setDirtyCanvas(true, true);
                    }
                });
            }
            return menuInfo;
        };
    },
    setup() {
        for (const nodeType of Object.values(LiteGraph.registered_node_types)) {
            this.addRerouteMenu(nodeType);
        }
    },
});

const deleteSelectedNodes = LGraphCanvas.prototype.deleteSelectedNodes;
LGraphCanvas.prototype.deleteSelectedNodes = function () {
    this.graph.beforeChange();

    for (var i in this.selected_nodes) {
        var node = this.selected_nodes[i];
        if (node.type != "Reroute") {
            continue;
        }
        if (node.block_delete) {
            continue;
        }
        if (
            node.inputs && node.inputs.length && node.outputs && node.outputs.length &&
            LiteGraph.isValidConnection(node.inputs[0].type, node.outputs[0].type) &&
            node.inputs[0].link && node.outputs[0].links && node.outputs[0].links.length
        ) {
            var input_link = node.graph.links[node.inputs[0].link];
            var output_links = node.outputs[0].links.map(function (link_id) {
                return node.graph.links[link_id];
            });
            var input_node = node.getInputNode(0);
            var output_nodes = node.getOutputNodes(0);

            if (input_node && output_nodes) {
                for (var outputIndex = 0; outputIndex < output_nodes.length; ++outputIndex) {
                    const output_node = output_nodes[outputIndex];
                    const output_link = output_links[outputIndex];
                    input_node.connect(input_link.origin_slot, output_node, output_link.target_slot);
                }
            }
        }
        this.graph.remove(node);
        if (this.onNodeDeselected) {
            this.onNodeDeselected(node);
        }
        delete this.selected_nodes[i];
    }

    this.current_node = null;
    this.highlighted_links = {};
    this.setDirty(true);
    this.graph.afterChange();

    deleteSelectedNodes.apply(this);
};


// ============================================================================
// 4. 文件上传功能
// ============================================================================

async function uploadFile(file) {
    try {
        const body = new FormData();
        const i = file.webkitRelativePath.lastIndexOf('/');
        const subfolder = file.webkitRelativePath.slice(0, i + 1);
        const new_file = new File([file], file.name, {
            type: file.type,
            lastModified: file.lastModified,
        });
        body.append("image", new_file);
        if (i > 0) {
            body.append("subfolder", subfolder);
        }
        const resp = await api.fetchApi("/upload/image", {
            method: "POST",
            body,
        });

        if (resp.status === 200) {
            return resp.status;
        } else {
            alert(resp.status + " - " + resp.statusText);
        }
    } catch (error) {
        alert(error);
    }
}

function addUploadWidget(nodeType, nodeData, widgetName) {
    const onNodeCreated = nodeType.prototype.onNodeCreated;
    nodeType.prototype.onNodeCreated = function () {
        const pathWidget = this.widgets.find((w) => w.name === widgetName);
        const fileInput = document.createElement("input");
        const onRemoved = this.onRemoved;
        this.onRemoved = () => {
            fileInput?.remove();
            onRemoved?.apply(this, arguments);
        };
        Object.assign(fileInput, {
            type: "file",
            accept: "*",
            style: "display: none",
            onchange: async () => {
                if (fileInput.files.length) {
                    if (await uploadFile(fileInput.files[0]) != 200) {
                        return;
                    }
                    const filename = fileInput.files[0].name;
                    pathWidget.options.values.push(filename);
                    pathWidget.value = filename;
                    if (pathWidget.callback) {
                        pathWidget.callback(filename);
                    }
                    app.graph.setDirtyCanvas(true);
                }
            },
        });
        document.body.append(fileInput);
        let uploadWidget = this.addWidget("button", "选择文件上传", "image", () => {
            app.canvas.node_widget = null;
            fileInput.click();
        });
        uploadWidget.options.serialize = false;

        onNodeCreated?.apply(this, arguments);
    };
}

app.registerExtension({
    name: "Banzhang-DevUtils.UploadAnything",
    async beforeRegisterNodeDef(nodeType, nodeData, app) {
        if (nodeData?.name === 'UploadAnything') {
            addUploadWidget(nodeType, nodeData, 'file');
        }
    },
});
