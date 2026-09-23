(function () {
    const { reactive, ref, computed, onMounted, onBeforeUnmount, provide } = Vue;

    const TASK_LABEL = { parse: '批量解析', tts: '批量生成人声', mix: '批量混音导出' };

    window.N2A_NovelDetailPage = {
        name: 'NovelDetailPage',
        props: { novelId: String },
        emits: ['go-novels', 'open-workbench'],
        setup(props, { emit }) {
            const novel = ref({ title: '', levels: { part: false, volume: false }, tree: [] });
            const selectedNodeId = ref(null);
            const checkedIds = ref([]);
            const collapsed = reactive({});
            const treeWidth = N2A.usePaneSize('n2a.tree.w', { min: 220, max: 640, def: 320, axis: 'x' });
            const rootTreeEl = ref(null);
            let rootSortable = null;
            function rebindRootSortable() {
                if (rootSortable) { rootSortable.destroy(); rootSortable = null; }
                if (!rootTreeEl.value) return;
                rootSortable = Sortable.create(rootTreeEl.value, {
                    animation: 150,
                    handle: '.tree-node',
                    onEnd(evt) {
                        if (evt.oldIndex === evt.newIndex) return;
                        const moved = (novel.value.tree || [])[evt.oldIndex];
                        if (moved) onReorder(moved.id, null, evt.newIndex);
                    },
                });
            }
            const treeHeight = N2A.usePaneSize('n2a.tree.h', { min: 140, max: 600, def: 260, axis: 'y' });
            const chapterStats = ref({});
            const idToTitle = ref({});

            function collectChapters(node) {
                if (!node) return [];
                if (node.type === 'chapter') return [node];
                return (node.children || []).flatMap(collectChapters);
            }
            function findNode(nodes, id) {
                for (const n of nodes) {
                    if (n.id === id) return n;
                    const found = findNode(n.children || [], id);
                    if (found) return found;
                }
                return null;
            }
            function reindexTitles(tree) {
                const map = {};
                (function walk(nodes) {
                    nodes.forEach((n) => { map[n.id] = n.title; walk(n.children || []); });
                })(tree);
                idToTitle.value = map;
            }

            async function load() {
                try {
                    const res = await API.getNovelTree(props.novelId);
                    novel.value = res.novel;
                    reindexTitles(res.novel.tree || []);
                } catch (e) { N2A.toast('error', e.message); }
            }
            onMounted(load);
            Vue.onMounted(rebindRootSortable);
            Vue.onUpdated(rebindRootSortable);
            onBeforeUnmount(() => { if (rootSortable) rootSortable.destroy(); });

            const toastedMixTasks = new Set();
            async function onTaskUpdate(e) {
                const task = e.detail && e.detail.task;
                if (!(task && task.novel_id === props.novelId && ['succeeded', 'failed', 'cancelled'].includes(task.state))) return;
                await load();
                if (checkedIds.value.length > 1) loadChapterStats();
                if (task.type === 'mix' && task.state === 'succeeded' && !toastedMixTasks.has(task.id)) {
                    toastedMixTasks.add(task.id);
                    const withMissing = collectChapters({ type: 'root', children: novel.value.tree })
                        .filter((c) => (c.missing_assets_count || 0) > 0).length;
                    if (withMissing > 0) N2A.toast('warning', `${withMissing} 个章节混音时跳过了缺失素材`);
                }
            }
            window.addEventListener('n2a:task-update', onTaskUpdate);
            onBeforeUnmount(() => window.removeEventListener('n2a:task-update', onTaskUpdate));

            // ---------------- tree 交互 ----------------
            function chapterIdsUnder(node) { return collectChapters(node).map((c) => c.id); }
            function onSelect(node) { selectedNodeId.value = node.id; }
            function onToggleCollapse(node) { collapsed[node.id] = !collapsed[node.id]; }
            function onOpenWorkbench(node) {
                if (node.type !== 'chapter') return;
                selectedNodeId.value = node.id;
                emit('open-workbench', node.id);
            }
            function onCheckToggle(node) {
                const ids = chapterIdsUnder(node);
                const allChecked = ids.length > 0 && ids.every((id) => checkedIds.value.includes(id));
                checkedIds.value = allChecked
                    ? checkedIds.value.filter((id) => !ids.includes(id))
                    : Array.from(new Set([...checkedIds.value, ...ids]));
            }
            async function onAdd(type, parentId) {
                const label = { part: '部', volume: '卷', chapter: '章节' }[type];
                const name = await N2A.promptDialog({ title: `新增${label}`, label: `${label}名称`, confirmLabel: '创建' });
                if (!name) return;
                try {
                    const res = await API.createNode(props.novelId, { type, title: name, parent_id: parentId });
                    N2A.toast('success', '已创建');
                    await load();
                    if (type === 'chapter') selectedNodeId.value = res.node_id;
                } catch (e) { N2A.toast('error', e.message); }
            }
            async function onRename(node) {
                const name = await N2A.promptDialog({ title: '重命名', label: '名称', value: node.title, confirmLabel: '保存' });
                if (!name) return;
                try {
                    await API.updateNode(props.novelId, node.id, { title: name });
                    N2A.toast('success', '已重命名');
                    load();
                } catch (e) { N2A.toast('error', e.message); }
            }
            async function onDeleteNode(node) {
                try {
                    const preview = await API.deleteNode(props.novelId, node.id, false);
                    const body = preview.has_audio
                        ? `确定要删除「${node.title}」吗？其下所有内容会一并删除（含 ${preview.affected_chapters} 个章节，其中包含已生成的音频）。`
                        : `确定要删除「${node.title}」吗？其下所有内容会一并删除（共 ${preview.affected_chapters} 个章节）。`;
                    const ok = await N2A.confirmDialog({ title: '删除节点', body, confirmLabel: '删除', danger: true });
                    if (!ok) return;
                    await API.deleteNode(props.novelId, node.id, true);
                    if (selectedNodeId.value === node.id) selectedNodeId.value = null;
                    N2A.toast('success', '已删除');
                    load();
                } catch (e) { N2A.toast('error', e.message); }
            }
            async function onReorder(nodeId, newParentId, newIndex) {
                try {
                    await API.reorderNodes(props.novelId, { node_id: nodeId, new_parent_id: newParentId, new_index: newIndex });
                    load();
                } catch (e) {
                    N2A.toast('error', e.message);
                    load(); // 失败也要重新拉一次，撤销 Sortable 已经在 DOM 上做的乐观移动
                }
            }

            provide('treeCtx', {
                selectedNodeId, checkedIds, collapsed,
                levels: computed(() => novel.value.levels || {}),
                chapterIdsUnder,
                onSelect, onToggleCollapse, onCheckToggle, onOpenWorkbench, onAdd, onRename, onDeleteNode, onReorder,
            });

            const rootAddLabel = computed(() => {
                if (novel.value.levels && novel.value.levels.part) return '+ 新增部';
                if (novel.value.levels && novel.value.levels.volume) return '+ 新增卷';
                return '+ 新增章节';
            });
            const rootAddType = computed(() => {
                if (novel.value.levels && novel.value.levels.part) return 'part';
                if (novel.value.levels && novel.value.levels.volume) return 'volume';
                return 'chapter';
            });

            // ---------------- 批量任务范围 / 预检 ----------------
            const selectedNode = computed(() => (selectedNodeId.value ? findNode(novel.value.tree || [], selectedNodeId.value) : null));
            const batchScope = computed(() => {
                if (checkedIds.value.length > 0) return { label: `已选中 ${checkedIds.value.length} 个章节`, scope: { chapter_ids: checkedIds.value } };
                if (selectedNode.value) {
                    const kindLabel = selectedNode.value.type === 'chapter' ? '章' : (selectedNode.value.type === 'volume' ? '卷' : '部');
                    return { label: `${kindLabel}《${selectedNode.value.title}》`, scope: { node_id: selectedNodeId.value } };
                }
                return { label: '整本小说', scope: {} };
            });

            async function openPreflight(type) {
                const scopeInfo = batchScope.value;
                let extraParams = {};
                let withAssets = false;
                if (type === 'mix') {
                    withAssets = await N2A.confirmDialog({
                        title: '批量混音导出',
                        body: '是否在混音时叠加背景音/音效？（需要相应素材已生成，否则会跳过并在完成后提示）',
                        confirmLabel: '带素材混音',
                    });
                    extraParams = { with_assets: !!withAssets };
                }
                let preflightResult;
                try {
                    preflightResult = await API.preflightTask({ type, novel_id: props.novelId, scope: scopeInfo.scope, params: extraParams });
                } catch (e) { N2A.toast('error', e.message); return; }
                const s = preflightResult.summary || {};
                const gpuMin = preflightResult.estimated_gpu_minutes;
                const body = `本次涉及范围：${scopeInfo.label}\n` +
                    `新生成 ${s.create || 0} 章，覆盖重跑 ${s.overwrite || 0} 章，跳过 ${s.skip || 0} 章。\n` +
                    `会失效的缓存：${preflightResult.invalidated_cache_count || 0} 项。\n` +
                    `预计 GPU 耗时：${gpuMin != null ? `约 ${gpuMin} 分钟` : '未知（尚无历史数据）'}`;
                const ok = await N2A.confirmDialog({ title: `${TASK_LABEL[type]} · 预检`, body, confirmLabel: '确认提交' });
                if (!ok) return;
                try {
                    await API.createTask({ type, novel_id: props.novelId, scope: scopeInfo.scope, params: extraParams });
                    N2A.toast('success', '任务已提交');
                } catch (e) { N2A.toast('error', e.message); }
            }

            // ---------------- 多选对比表 ----------------
            const showChapterTable = computed(() => checkedIds.value.length > 1);
            async function loadChapterStats() {
                try {
                    const res = await API.getChapterStats(props.novelId);
                    chapterStats.value = res.chapters || {};
                } catch (e) { /* 对比表不是关键路径 */ }
            }
            Vue.watch(showChapterTable, (v) => { if (v) loadChapterStats(); });
            const chapterTableRows = computed(() => {
                const okColor = 'compare-ok', noColor = 'compare-no', partialColor = 'compare-partial';
                return checkedIds.value.map((id) => {
                    const node = findNode(novel.value.tree || [], id) || { title: id, status: 'unparsed' };
                    const stat = chapterStats.value[id];
                    const parsed = node.state && node.state !== 'unparsed';
                    return {
                        id, title: node.title,
                        rawLabel: node.raw ? '✓' : '—', rawColor: node.raw ? okColor : noColor,
                        parsedLabel: parsed ? '✓' : '—', parsedColor: parsed ? okColor : noColor,
                        chunkCount: stat ? stat.segment_count : '—',
                        voiceLabel: stat ? (stat.segment_count && stat.voiced_count >= stat.segment_count ? '✓ 已完成' : (stat.voiced_count > 0 ? `${stat.voiced_count}/${stat.segment_count}` : '—')) : '加载中…',
                        voiceColor: stat && stat.segment_count && stat.voiced_count >= stat.segment_count ? okColor : (stat && stat.voiced_count > 0 ? partialColor : noColor),
                        bgmLabel: stat && stat.bgm_count ? `✓ ${stat.bgm_count}` : '—', bgmColor: stat && stat.bgm_count ? okColor : noColor,
                        mixLabel: stat && stat.mixed_with_assets ? '✓' : (node.state === 'voiced' ? '仅人声' : '—'),
                        mixColor: stat && stat.mixed_with_assets ? okColor : noColor,
                    };
                });
            });

            // ---------------- 单节点详情 ----------------
            const detailIsChapter = computed(() => !!(selectedNode.value && selectedNode.value.type === 'chapter'));
            const detailIsPartOrVolume = computed(() => !!(selectedNode.value && selectedNode.value.type !== 'chapter'));
            const chapterDetailStats = computed(() => {
                const stat = selectedNodeId.value ? chapterStats.value[selectedNodeId.value] : null;
                if (!stat) return null;
                return { total: stat.segment_count, voiced: stat.voiced_count, unvoiced: stat.segment_count - stat.voiced_count, unbound: stat.unbound_count };
            });
            Vue.watch(selectedNodeId, async (id) => {
                if (!id) return;
                const node = findNode(novel.value.tree || [], id);
                if (node && node.type === 'chapter') {
                    try {
                        const res = await API.getChapterStats(props.novelId);
                        chapterStats.value = res.chapters || {};
                    } catch (e) { /* noop */ }
                }
            });

            const reimportBusy = ref(false);
            async function onReimportFile(e) {
                const file = e.target.files && e.target.files[0];
                e.target.value = '';
                if (!file || !selectedNodeId.value) return;
                const text = await file.text();
                try {
                    const preview = await API.uploadRaw(props.novelId, selectedNodeId.value, text, false);
                    if (preview.confirmed) { N2A.toast('success', '正文已导入'); load(); return; }
                    const ok = await N2A.confirmDialog({
                        title: '重新导入章节文本',
                        body: `重新导入会清空：${(preview.will_remove || []).join('、') || '（无下游产物）'}。\n` +
                            `已合成的语音片段会保留 ${preview.kept_cache_count || 0} 个在缓存里，但下次合成不一定命中。此操作不可撤销。`,
                        confirmLabel: '确认导入', danger: true,
                    });
                    if (!ok) return;
                    await API.uploadRaw(props.novelId, selectedNodeId.value, text, true);
                    N2A.toast('success', '正文已重新导入');
                    load();
                } catch (e2) { N2A.toast('error', e2.message); }
            }

            function titleFor(chapterId) { return idToTitle.value[chapterId]; }

            return {
                novelId: props.novelId,
                novel, selectedNodeId, checkedIds, treeWidth, treeHeight, rootTreeEl, rootAddLabel, rootAddType,
                onAdd, batchScope, openPreflight, showChapterTable, chapterTableRows,
                detailIsChapter, detailIsPartOrVolume, selectedNode, chapterDetailStats,
                onReimportFile, titleFor,
                goNovels: () => emit('go-novels'),
                goWorkbench: () => emit('open-workbench', selectedNodeId.value),
                onRename, onDeleteNode,
            };
        },
        components: { TreeNode: window.N2A_TreeNode, TaskPanel: window.N2A_TaskPanel },
        template: `
        <section class="novel-detail-page" data-screen-label="小说详情" style="flex:1;display:flex;flex-direction:column;min-height:0">
            <div class="detail-header">
                <button class="back-link" @click="goNovels">&larr; 小说列表</button>
                <h2>{{ novel.title }}</h2>
            </div>
            <div class="n2a-split">
                <div class="n2a-treepane" :style="{ width: treeWidth.size.value + 'px', '--roleTreeH': treeHeight.size.value + 'px' }">
                    <div class="n2a-treepane-scroll">
                        <div ref="rootTreeEl">
                            <tree-node v-for="node in novel.tree" :key="node.id" :node="node" :depth="0" />
                        </div>
                        <div class="tree-add-row" style="padding:5px 10px">
                            <button @click="onAdd(rootAddType, null)">{{ rootAddLabel }}</button>
                        </div>
                    </div>
                    <div class="n2a-vresizer" @mousedown="treeHeight.startResize" @touchstart="treeHeight.startResize"></div>
                </div>
                <div class="n2a-resizer" @mousedown="treeWidth.startResize"></div>
                <div class="n2a-contentpane">
                    <div class="batch-bar">
                        <div class="batch-scope-label">批量任务范围：<b style="color:var(--text)">{{ batchScope.label }}</b></div>
                        <div class="batch-buttons">
                            <button class="btn" @click="openPreflight('parse')">批量解析</button>
                            <button class="btn" @click="openPreflight('tts')">批量生成人声</button>
                            <button class="btn" @click="openPreflight('mix')">批量混音导出</button>
                        </div>
                    </div>

                    <div v-if="showChapterTable" class="chapter-compare-wrap">
                        <h3>已选中 {{ checkedIds.length }} 个章节</h3>
                        <table class="chapter-compare">
                            <thead><tr>
                                <th>章节</th><th class="center">文本上传</th><th class="center">已解析</th>
                                <th class="center">分块数</th><th class="center">人声生成</th>
                                <th class="center">背景音生成</th><th class="center">混音合成</th>
                            </tr></thead>
                            <tbody>
                                <tr v-for="row in chapterTableRows" :key="row.id" :data-chapter-id="row.id">
                                    <td>{{ row.title }}</td>
                                    <td class="center" :class="row.rawColor">{{ row.rawLabel }}</td>
                                    <td class="center" :class="row.parsedColor">{{ row.parsedLabel }}</td>
                                    <td class="center">{{ row.chunkCount }}</td>
                                    <td class="center" :class="row.voiceColor">{{ row.voiceLabel }}</td>
                                    <td class="center" :class="row.bgmColor">{{ row.bgmLabel }}</td>
                                    <td class="center" :class="row.mixColor">{{ row.mixLabel }}</td>
                                </tr>
                            </tbody>
                        </table>
                    </div>
                    <div v-else-if="detailIsChapter" class="chapter-detail">
                        <h3>{{ selectedNode.title }}</h3>
                        <div class="chapter-stats-line" v-if="chapterDetailStats">
                            总分块 <b class="stat-value">{{ chapterDetailStats.total }}</b> &nbsp;|&nbsp;
                            已配音 <b class="stat-value">{{ chapterDetailStats.voiced }}</b> &nbsp;|&nbsp;
                            未配音 <b class="stat-value">{{ chapterDetailStats.unvoiced }}</b> &nbsp;|&nbsp;
                            未绑定角色 <b class="stat-value" :class="{ danger: chapterDetailStats.unbound > 0 }">{{ chapterDetailStats.unbound }}</b>
                        </div>
                        <div class="chapter-actions">
                            <button class="btn-primary btn" @click="goWorkbench">进入配音工作台</button>
                            <label class="upload-label">
                                {{ selectedNode.raw ? '重新导入章节文本' : '上传章节正文' }}
                                <input type="file" style="display:none" @change="onReimportFile" />
                            </label>
                        </div>
                    </div>
                    <div v-else-if="detailIsPartOrVolume" class="chapter-detail">
                        <h3>{{ selectedNode.title }}</h3>
                        <div class="chapter-actions">
                            <button class="btn" @click="onRename(selectedNode)">重命名</button>
                            <button class="btn" style="color:var(--danger)" @click="onDeleteNode(selectedNode)">删除</button>
                        </div>
                    </div>
                    <div v-else class="empty-detail">选择左侧节点查看详情</div>

                    <task-panel :novel-id="novelId" :title-for="titleFor" />
                </div>
            </div>
        </section>
        `,
    };
})();
