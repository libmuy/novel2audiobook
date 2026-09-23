(function () {
    const { reactive, ref, computed, onMounted, onBeforeUnmount, provide } = Vue;

    const STATUS_CLASS = (status) => {
        if (!status) return 'missing';
        if (status.startsWith('STALE')) return 'stale';
        if (status.startsWith('OK')) return 'ok';
        return 'missing';
    };

    window.N2A_AssetsPage = {
        name: 'AssetsPage',
        setup() {
            const cat = window.N2A_useCategoryTree({
                getTree: API.getAssetCategoryTree.bind(API),
                putTree: API.putAssetCategoryTree.bind(API),
                itemNoun: '素材',
            });
            provide('catCtx', {
                collapsed: cat.collapsed, filterPath: cat.filterPath, pathOf: cat.pathOf,
                toggleCollapse: cat.toggleCollapse, select: cat.select, addChild: cat.addChild,
                rename: cat.rename, remove: cat.remove,
            });

            const specs = ref([]);
            const tagFilters = ref([]);
            const tags = ref([]);
            const genTask = ref(null); // 当前正在跑的全局 asset_gen 任务

            async function loadSpecs() { specs.value = (await API.getAssetSpecs()).specs || []; }
            async function loadTags() { tags.value = (await API.getAssetTags()).tags || []; }
            async function loadAll() { await Promise.all([cat.refresh(), loadSpecs(), loadTags()]); }
            onMounted(loadAll);

            function onTaskUpdate(e) {
                const task = e.detail && e.detail.task;
                if (!task || task.type !== 'asset_gen') return;
                if (['succeeded', 'failed', 'cancelled'].includes(task.state)) {
                    if (genTask.value && genTask.value.id === task.id) genTask.value = null;
                    if (task.state === 'succeeded') N2A.toast('success', '素材生成完成');
                    if (task.state === 'failed') N2A.toast('error', `素材生成失败：${task.error || ''}`);
                    loadSpecs();
                } else {
                    genTask.value = task;
                }
            }
            window.addEventListener('n2a:task-update', onTaskUpdate);
            onBeforeUnmount(() => window.removeEventListener('n2a:task-update', onTaskUpdate));

            const visibleAssets = computed(() => {
                const filterPath = cat.filterPath.value;
                return specs.value
                    .filter((a) => !filterPath || a.category === filterPath || (a.category || '').startsWith(filterPath + '/'))
                    .filter((a) => tagFilters.value.length === 0 || tagFilters.value.some((t) => (a.tags || []).includes(t)));
            });
            function toggleTagFilter(t) {
                tagFilters.value = tagFilters.value.includes(t) ? tagFilters.value.filter((x) => x !== t) : [...tagFilters.value, t];
            }

            async function openCreate() {
                const res = await N2A.openModal({
                    type: 'assetCreate', width: 480, title: '新增素材',
                    showTextField: true, textFieldLabel: '素材名称', textValue: '',
                    showAssetFields: true, assetKind: 'ambience', category: cat.filterPath.value || '', duration: 8, seed: 0, prompt: '', negativePrompt: '', description: '',
                    categoryOptions: cat.flatOptions.value,
                    showTagsField: true, tagsDraft: [],
                    confirmLabel: '创建',
                });
                if (!res || !res.textValue || !res.textValue.trim()) return;
                try {
                    await API.createAssetSpec({
                        kind: res.assetKind, name: res.textValue.trim(), prompt: res.prompt,
                        negative_prompt: res.negativePrompt, duration_sec: res.duration, seed: res.seed,
                        description: res.description, category: res.category, tags: res.tagsDraft,
                    });
                    N2A.toast('success', '素材已创建');
                    loadAll();
                } catch (e) { N2A.toast('error', e.message); }
            }
            async function openEdit(asset) {
                const res = await N2A.openModal({
                    type: 'assetEdit', width: 480, title: '编辑素材',
                    showTextField: true, textFieldLabel: '素材名称', textValue: asset.name,
                    showAssetFields: true, assetKind: asset.kind, category: asset.category, duration: asset.duration_sec,
                    seed: asset.seed, prompt: asset.prompt, negativePrompt: asset.negative_prompt || '', description: asset.description || '',
                    categoryOptions: cat.flatOptions.value,
                    showTagsField: true, tagsDraft: [...(asset.tags || [])],
                    confirmLabel: '保存',
                });
                if (!res) return;
                try {
                    await API.updateAssetSpec(asset.kind, asset.name, {
                        description: res.description, prompt: res.prompt, negative_prompt: res.negativePrompt,
                        duration_sec: res.duration, seed: res.seed, category: res.category, tags: res.tagsDraft,
                    });
                    N2A.toast('success', '已保存');
                    loadAll();
                } catch (e) { N2A.toast('error', e.message); }
            }
            async function openDelete(asset) {
                const ok = await N2A.confirmDialog({
                    title: '删除素材', body: `确定要删除素材「${asset.name}」吗？此操作不可恢复。`,
                    confirmLabel: '删除', danger: true,
                });
                if (!ok) return;
                try {
                    await API.deleteAssetSpec(asset.kind, asset.name, true);
                    N2A.toast('success', '素材已删除');
                    loadAll();
                } catch (e) { N2A.toast('error', e.message); }
            }
            async function generateAll() {
                try {
                    const res = await API.createTask({ type: 'asset_gen', params: {} });
                    genTask.value = res.tasks[0];
                    N2A.toast('success', '已提交生成任务');
                } catch (e) { N2A.toast('error', e.message); }
            }
            async function regenerate(asset) {
                try {
                    const res = await API.createTask({ type: 'asset_gen', params: { kinds: [asset.kind], only: [asset.name], force: true } });
                    genTask.value = res.tasks[0];
                    N2A.toast('success', `已提交重新生成「${asset.name}」`);
                } catch (e) { N2A.toast('error', e.message); }
            }

            return {
                tree: cat.tree, filterPath: cat.filterPath, selectAll: cat.selectAll, addRoot: () => cat.addChild(null),
                visibleAssets, tags, tagFilters, toggleTagFilter, genTask,
                openCreate, openEdit, openDelete, generateAll, regenerate,
                statusClass: STATUS_CLASS,
                kindLabel: (k) => (k === 'ambience' ? '背景音' : '音效'),
                audioUrl: (a) => API.getAssetAudioUrl(a.kind, a.name),
            };
        },
        components: { CategoryNode: window.N2A_CategoryNode },
        template: `
        <section data-screen-label="背景音与音效库" style="flex:1;display:flex;flex-direction:column;min-height:0">
            <div class="page-pad" style="padding:20px 24px 16px;border-bottom:1px solid var(--border)"><h2 style="margin:0;font-size:26px">背景音与音效库</h2></div>
            <div v-if="genTask" class="gen-banner">
                <span>正在生成素材…{{ genTask.progress && genTask.progress.message ? genTask.progress.message : '' }}</span>
                <div class="task-progress-track" style="width:160px">
                    <div class="task-progress-fill" :style="{ width: (genTask.progress && genTask.progress.total ? Math.round(genTask.progress.done / genTask.progress.total * 100) : 0) + '%' }"></div>
                </div>
            </div>
            <div class="n2a-split">
                <div class="n2a-treepane">
                    <div class="category-pane">
                        <div class="tree-all-row" :class="{ selected: !filterPath }" @click="selectAll">全部</div>
                        <category-node v-for="node in tree" :key="node.id" :node="node" :depth="0" />
                        <div class="tree-add-row" style="padding:5px 10px">
                            <button @click="addRoot">+ 新建分类</button>
                        </div>
                    </div>
                </div>
                <div class="n2a-contentpane page-pad" style="padding:20px 24px">
                    <div class="page-head" style="margin-bottom:14px">
                        <h3 style="margin:0;font-size:16px">{{ filterPath || '全部素材' }}</h3>
                        <div style="display:flex;gap:8px">
                            <button class="btn" @click="generateAll">生成缺失素材</button>
                            <button class="btn-primary btn" @click="openCreate">+ 新增素材</button>
                        </div>
                    </div>
                    <div v-if="tags.length" class="tag-filter-row">
                        <span class="tag-filter-label">标签筛选</span>
                        <button v-for="t in tags" :key="t.name" class="tag-filter-chip" :class="{ active: tagFilters.includes(t.name) }" @click="toggleTagFilter(t.name)">{{ t.name }}</button>
                    </div>
                    <div class="card-grid">
                        <div v-for="asset in visibleAssets" :key="asset.kind + ':' + asset.name" class="asset-card">
                            <div class="asset-card-head">
                                <div class="asset-card-name">{{ asset.name }}</div>
                                <span class="badge">{{ kindLabel(asset.kind) }}</span>
                            </div>
                            <div class="asset-card-sub">
                                分类：{{ asset.category || '未分类' }} &middot; {{ asset.duration_sec }}s &middot; {{ asset.engine }}
                                <span :class="['asset-status-badge', statusClass(asset.status)]" style="margin-left:6px">{{ asset.status }}</span>
                            </div>
                            <div v-if="asset.engine && asset.engine !== '-' && asset.expected_engine && asset.expected_engine !== asset.engine" class="asset-engine-drift">
                                引擎已变更：当前配置是 {{ asset.expected_engine }}，这份音频是用 {{ asset.engine }} 生成的，建议重新生成
                            </div>
                            <div class="asset-card-desc">{{ asset.description }}</div>
                            <div class="tag-chip-row" v-if="(asset.tags || []).length">
                                <span v-for="tag in asset.tags" :key="tag" class="tag-chip">{{ tag }}</span>
                            </div>
                            <audio v-if="asset.status && asset.status !== 'MISSING'" controls :src="audioUrl(asset)" style="width:100%;height:30px"></audio>
                            <div class="card-footer">
                                <button class="btn-text" @click="openEdit(asset)">编辑</button>
                                <button class="btn-text" @click="regenerate(asset)">{{ asset.status === 'MISSING' ? '生成' : '重新生成' }}</button>
                                <button class="btn-danger-text" @click="openDelete(asset)">删除</button>
                            </div>
                        </div>
                    </div>
                    <div v-if="!visibleAssets.length" class="empty-state">
                        <div class="empty-state-title">暂无素材</div>
                    </div>
                </div>
            </div>
        </section>
        `,
    };
})();
