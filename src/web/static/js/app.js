const { createApp, ref, reactive, computed, watch, onMounted, onUnmounted, nextTick, provide, inject } = Vue;

// 语气标签（值 = 后端 emotion 枚举，label 给人看）。批量改语气的下拉用它。
const EMOTION_OPTIONS = [
    { value: 'neutral', label: '中性' }, { value: 'happy', label: '开心' },
    { value: 'angry', label: '生气' }, { value: 'sad', label: '悲伤' },
    { value: 'serious', label: '严肃' }, { value: 'afraid', label: '害怕' },
    { value: 'surprised', label: '惊讶' }, { value: 'calm', label: '平静' },
];

const app = createApp({
    setup() {
        const currentRoute = ref(window.location.hash || '#/novels');
        const currentPage = ref('novels-page');
        const currentPageProps = ref({});
        const resource = reactive({
            cpu_percent: 0,
            memory_used: 0,
            memory_total: 0,
            gpu_percent: 0,
            gpu_memory_used: 0,
            gpu_memory_total: 0,
        });
        const gpuOwner = ref('idle');
        const sseDisconnected = ref(false);
        const toast = ref(null);
        const confirmDialog = ref(null);
        const promptDialog = ref(null);

        // 深色模式：纯前端偏好，跟 n2a.lastRoute 一样存本地，不经过后端配置。
        // CSS 的 [data-theme] 选择器挂在 :root（即 <html>）上，但 #app 是
        // Vue 的挂载目标本身——挂载目标自己的属性不会被当成响应式模板编译
        // （in-DOM 根模板只编译容器的 innerHTML，容器自身的属性不受管），
        // 所以不能用 :data-theme 模板绑定，必须在这里手动同步到
        // document.documentElement。
        const darkMode = ref(localStorage.getItem('n2a.darkMode') === '1');
        const applyTheme = () => {
            document.documentElement.dataset.theme = darkMode.value ? 'dark' : 'light';
        };
        const toggleDarkMode = () => {
            darkMode.value = !darkMode.value;
            localStorage.setItem('n2a.darkMode', darkMode.value ? '1' : '0');
            applyTheme();
        };

        let eventSource = null;
        let reconnectTimer = null;
        let errorCount = 0;
        const MAX_ERRORS = 3;

        const gpuOwnerClass = computed(() => gpuOwner.value);
        const gpuOwnerText = computed(() => {
            switch (gpuOwner.value) {
                case 'llm': return 'LLM 占用';
                case 'tts': return 'TTS 占用';
                default: return '空闲';
            }
        });

        // src/monitor.py 的 snapshot() 输出单位是字节，不是 MB
        const formatMemory = (bytes) => {
            if (!bytes) return '0MB';
            const mb = bytes / (1024 * 1024);
            if (mb >= 1024) {
                return `${(mb / 1024).toFixed(1)}G`;
            }
            return `${Math.round(mb)}MB`;
        };

        // 后端 /api/monitor 与 SSE 的 resource 事件都是嵌套结构
        // {gpu:{busy_percent,vram_used_bytes,vram_total_bytes}, cpu_percent,
        //  memory:{used_bytes,total_bytes}}，这里统一摊平成 resource 用的字段
        const applyResourceSnapshot = (snap) => {
            if (!snap) return;
            if (typeof snap.cpu_percent === 'number') resource.cpu_percent = snap.cpu_percent;
            if (snap.memory) {
                resource.memory_used = snap.memory.used_bytes || 0;
                resource.memory_total = snap.memory.total_bytes || 0;
            }
            if (snap.gpu) {
                resource.gpu_percent = snap.gpu.busy_percent || 0;
                resource.gpu_memory_used = snap.gpu.vram_used_bytes || 0;
                resource.gpu_memory_total = snap.gpu.vram_total_bytes || 0;
            }
        };

        const routes = {
            '#/novels': { component: 'novels-page', props: {} },
            '#/roles': { component: 'roles-page', props: {} },
            '#/assets': { component: 'assets-page', props: {} },
            '#/settings': { component: 'settings-page', props: {} },
        };

        const parseRoute = (hash) => {
            if (!hash || hash === '#/' || hash === '#') {
                return { component: 'novels-page', props: {} };
            }

            if (hash.startsWith('#/novels/') && hash.includes('/chapters/')) {
                const parts = hash.split('/');
                const novelId = parts[2];
                const chapterId = parts[4];
                return {
                    component: 'workbench-page',
                    props: { novelId, chapterId },
                };
            }

            if (hash.startsWith('#/novels/')) {
                const parts = hash.split('/');
                const novelId = parts[2];
                return {
                    component: 'novel-detail-page',
                    props: { novelId },
                };
            }

            return routes[hash] || { component: 'novels-page', props: {} };
        };

        const navigateTo = (hash) => {
            window.location.hash = hash;
        };

        const handleRouteChange = () => {
            const hash = window.location.hash;
            currentRoute.value = hash;
            
            const route = parseRoute(hash);
            currentPage.value = route.component;
            currentPageProps.value = route.props;

            localStorage.setItem('n2a.lastRoute', hash);
        };

        const restoreLastRoute = () => {
            const lastRoute = localStorage.getItem('n2a.lastRoute');
            if (lastRoute && window.location.hash === '') {
                const route = parseRoute(lastRoute);
                if (route.component !== 'novels-page') {
                    window.location.hash = lastRoute;
                } else {
                    window.location.hash = '#/novels';
                }
            } else if (window.location.hash === '') {
                window.location.hash = '#/novels';
            }
        };

        const connectSSE = () => {
            if (eventSource) {
                eventSource.close();
            }

            eventSource = API.createSSEConnection(
                (data) => {
                    errorCount = 0;
                    sseDisconnected.value = false;

                    switch (data.type) {
                        case 'resource':
                            applyResourceSnapshot(data.payload);
                            break;
                        case 'task_update':
                            window.dispatchEvent(new CustomEvent('task-update', { detail: data.payload }));
                            break;
                        case 'tree_update':
                            window.dispatchEvent(new CustomEvent('tree-update', { detail: data.payload }));
                            break;
                    }
                },
                (error) => {
                    errorCount++;
                    if (errorCount >= MAX_ERRORS) {
                        eventSource.close();
                        sseDisconnected.value = true;
                        startPolling();
                    }
                }
            );
        };

        let pollingTimer = null;

        const startPolling = () => {
            if (pollingTimer) return;

            pollingTimer = setInterval(async () => {
                try {
                    const monitorData = await API.getMonitor();
                    applyResourceSnapshot(monitorData);
                } catch (error) {
                    console.error('轮询监控数据失败:', error);
                }
            }, 3000);
        };

        const stopPolling = () => {
            if (pollingTimer) {
                clearInterval(pollingTimer);
                pollingTimer = null;
            }
        };

        const refreshGpuOwner = async () => {
            try {
                const data = await API.getGpuOwner();
                gpuOwner.value = data.owner || 'idle';
            } catch (error) {
                console.error('获取GPU占用方失败:', error);
            }
        };

        const showToast = (message, type = 'success') => {
            if (toast.value) {
                toast.value.show(message, type);
            }
        };

        const showConfirm = (options) => {
            return confirmDialog.value ? confirmDialog.value.show(options) : Promise.resolve(false);
        };

        // 取代原生 prompt()：文本输入或下拉选择，取消/关闭返回 null
        const showPrompt = (options) => {
            return promptDialog.value ? promptDialog.value.show(options) : Promise.resolve(null);
        };

        // 通过 provide/inject 暴露给子组件，不要用 window.app.__vue_app__.
        // _instance.proxy 这种够 Vue 内部私有属性的写法——顶层 const app 在经典
        // <script> 里不会自动挂到 window 上，之前那样写点了就直接抛异常。
        provide('showConfirm', showConfirm);
        provide('showToast', showToast);
        provide('showPrompt', showPrompt);

        onMounted(() => {
            applyTheme();
            restoreLastRoute();
            handleRouteChange();
            connectSSE();
            refreshGpuOwner();

            window.addEventListener('hashchange', handleRouteChange);
        });

        onUnmounted(() => {
            if (eventSource) {
                eventSource.close();
            }
            stopPolling();
            if (reconnectTimer) {
                clearTimeout(reconnectTimer);
            }
            window.removeEventListener('hashchange', handleRouteChange);
        });

        return {
            currentRoute,
            currentPage,
            currentPageProps,
            resource,
            gpuOwner,
            gpuOwnerClass,
            gpuOwnerText,
            sseDisconnected,
            toast,
            confirmDialog,
            promptDialog,
            darkMode,
            toggleDarkMode,
            formatMemory,
            showToast,
            showConfirm,
            navigateTo,
        };
    },
});

// 注册页面组件
app.component('novels-page', {
    template: `
        <div class="page-pad novels-page">
        <div class="page-header">
            <h1 class="page-title">小说列表</h1>
            <div class="page-actions">
                <button class="btn btn-primary" @click="showCreateDialog">+ 新增小说</button>
            </div>
        </div>
        
        <div class="search-box">
            <input
                type="text"
                class="search-input"
                placeholder="搜索小说名称..."
                v-model="searchQuery"
            >
        </div>

        <div v-if="loading" class="empty-state">
            <div class="loading-spinner"></div>
            <p class="mt-2">加载中...</p>
        </div>

        <div v-else-if="filteredNovels.length === 0" class="empty-state">
            <h3 class="empty-state-title">暂无小说</h3>
            <p class="empty-state-description">点击上方按钮创建第一本小说</p>
        </div>
        
        <div v-else class="card-grid">
            <div
                v-for="novel in filteredNovels"
                :key="novel.novel_id"
                class="card"
                @click="openNovel(novel.novel_id)"
                style="cursor: pointer;"
            >
                <div class="card-header">
                    <h3 class="card-title">{{ novel.title }}</h3>
                    <div class="card-actions" @click.stop>
                        <button class="action-btn" @click="editNovel(novel)">编辑</button>
                        <button class="action-btn danger" @click="deleteNovel(novel)">删除</button>
                    </div>
                </div>
                <div class="card-content">
                    <p v-if="novel.description" class="mb-2">{{ novel.description }}</p>
                    <div class="flex gap-2 mb-2">
                        <span v-if="novel.levels?.part" class="badge badge-primary">部</span>
                        <span v-if="novel.levels?.volume" class="badge badge-primary">卷</span>
                    </div>
                    <div class="text-sm text-gray">
                        共 {{ novel.chapter_count || 0 }} 章
                    </div>
                    <div class="stats-bar mt-2">
                        <div class="stat-item">
                            <span class="stat-value">{{ novel.dubbed_count || 0 }}</span>
                            <span class="stat-label">已配音</span>
                        </div>
                        <div class="stat-item">
                            <span class="stat-value">{{ novel.parsed_count || 0 }}</span>
                            <span class="stat-label">已解析</span>
                        </div>
                        <div class="stat-item">
                            <span class="stat-value">{{ novel.total_count || 0 }}</span>
                            <span class="stat-label">共</span>
                        </div>
                    </div>
                </div>
            </div>
        </div>
        
        <div v-if="showDialog" class="modal-overlay" @click.self="closeDialog">
            <div class="modal">
                <div class="modal-header">
                    <h2 class="modal-title">{{ editingNovel ? '编辑小说' : '新增小说' }}</h2>
                    <button class="modal-close" @click="closeDialog">&times;</button>
                </div>
                <div class="modal-body">
                    <div class="form-group">
                        <label class="form-label">小说名称</label>
                        <input type="text" class="form-input" v-model="form.title" placeholder="请输入小说名称">
                    </div>
                    <div class="form-group">
                        <label class="form-label">简介</label>
                        <textarea class="form-textarea" v-model="form.description" placeholder="请输入小说简介"></textarea>
                    </div>
                    <div class="form-group">
                        <label class="form-checkbox">
                            <input type="checkbox" v-model="form.levels.part">
                            <span>启用部层级</span>
                        </label>
                    </div>
                    <div class="form-group">
                        <label class="form-checkbox">
                            <input type="checkbox" v-model="form.levels.volume">
                            <span>启用卷层级</span>
                        </label>
                    </div>
                </div>
                <div class="modal-footer">
                    <button class="btn btn-secondary" @click="closeDialog">取消</button>
                    <button class="btn btn-primary" @click="saveNovel" :disabled="!form.title.trim()">
                        {{ editingNovel ? '保存' : '创建' }}
                    </button>
                </div>
            </div>
        </div>
        </div>
    `,
    setup() {
        const showConfirm = inject('showConfirm');
        const showToast = inject('showToast');

        const novels = ref([]);
        const loading = ref(true);
        const searchQuery = ref('');
        const showDialog = ref(false);
        const editingNovel = ref(null);
        const form = reactive({
            title: '',
            description: '',
            levels: {
                part: false,
                volume: false,
            },
        });

        const filteredNovels = computed(() => {
            if (!searchQuery.value) return novels.value;
            const query = searchQuery.value.toLowerCase();
            return novels.value.filter(novel =>
                (novel.title || '').toLowerCase().includes(query)
            );
        });

        const loadNovels = async () => {
            loading.value = true;
            try {
                const list = await API.getNovels();
                // list_novels() 本身不带每本书的配音/解析进度，逐本拉一次
                // tree 的 status 摘要来算——个人自用规模的小说库，几十本
                // 也就几十次请求，可以接受
                novels.value = await Promise.all(list.map(async (novel) => {
                    try {
                        const { status } = await API.getNovelTree(novel.novel_id);
                        const chapters = status?.chapters || [];
                        return {
                            ...novel,
                            dubbed_count: chapters.filter((c) => c.mp3).length,
                            parsed_count: chapters.filter((c) => c.final).length,
                            total_count: chapters.length,
                        };
                    } catch (e) {
                        return { ...novel, dubbed_count: 0, parsed_count: 0, total_count: novel.chapter_count || 0 };
                    }
                }));
            } catch (error) {
                console.error('加载小说列表失败:', error);
            } finally {
                loading.value = false;
            }
        };

        const openNovel = (novelId) => {
            window.location.hash = `#/novels/${novelId}`;
        };

        const showCreateDialog = () => {
            editingNovel.value = null;
            form.title = '';
            form.description = '';
            form.levels.part = false;
            form.levels.volume = false;
            showDialog.value = true;
        };

        const editNovel = (novel) => {
            editingNovel.value = novel;
            form.title = novel.title;
            form.description = novel.description || '';
            form.levels.part = novel.levels?.part || false;
            form.levels.volume = novel.levels?.volume || false;
            showDialog.value = true;
        };

        const closeDialog = () => {
            showDialog.value = false;
            editingNovel.value = null;
        };

        const saveNovel = async () => {
            try {
                if (editingNovel.value) {
                    await API.updateNovel(editingNovel.value.novel_id, form);
                } else {
                    await API.createNovel(form);
                }
                closeDialog();
                await loadNovels();
            } catch (error) {
                console.error('保存小说失败:', error);
                showToast?.(`保存失败: ${error.message}`, 'error');
            }
        };

        const deleteNovel = async (novel) => {
            const confirmed = await showConfirm({
                title: '删除小说',
                message: `确定要删除小说「${novel.title}」吗？此操作不可恢复。`,
                warning: `该小说包含 ${(novel.chapter_count || 0)} 个章节，删除后所有章节数据将被移入回收站。`,
                confirmText: '删除',
                confirmClass: 'btn-danger',
            });

            if (confirmed) {
                try {
                    await API.deleteNovel(novel.novel_id, true);
                    await loadNovels();
                } catch (error) {
                    console.error('删除小说失败:', error);
                    showToast?.(`删除失败: ${error.message}`, 'error');
                }
            }
        };

        onMounted(() => {
            loadNovels();
        });

        return {
            novels,
            loading,
            searchQuery,
            filteredNovels,
            showDialog,
            editingNovel,
            form,
            openNovel,
            showCreateDialog,
            editNovel,
            closeDialog,
            saveNovel,
            deleteNovel,
        };
    },
});

// 树节点：递归渲染 part/volume/chapter 及其 children。之前的实现只
// v-for 了顶层 tree 数组，volume/part 底下的章节完全不会显示出来。
// 拖拽用 SortableJS，同一个 group 名允许跨层级拖动；不让 Sortable 自己
// 维护 DOM 顺序（那样会跟 Vue 的响应式渲染打架），拖完直接把结果报给
// API，再由父组件重新拉取真实树覆盖渲染。
app.component('tree-node', {
    name: 'tree-node',
    props: {
        node: { type: Object, required: true },
        depth: { type: Number, default: 0 },
        selectedId: { type: String, default: null },
        selectedIds: { type: Array, default: () => [] },
        // GET /tree 里 status.chapters 按 chapter_id 建的整张表；节点自己的 status
        // 只有一个字符串，看不出「混音时缺素材」，所以整张表往下传
        statusMap: { type: Object, default: () => ({}) },
    },
    emits: ['select', 'toggle-select', 'rename', 'delete', 'reorder'],
    template: `
        <div :data-node-id="node.id">
            <div
                class="tree-node"
                :class="{ selected: selectedId === node.id }"
                :style="{ paddingLeft: (depth * 16) + 'px' }"
                @click="$emit('select', node)"
            >
                <input
                    type="checkbox"
                    class="tree-checkbox"
                    :checked="selectedIds.includes(node.id)"
                    @click.stop
                    @change="$emit('toggle-select', node)"
                >
                <span class="status-dot" :class="statusClass(node)"></span>
                <span class="tree-node-name">{{ node.title }}</span>
                <div class="tree-node-actions">
                    <button class="action-btn" @click.stop="$emit('rename', node)">改</button>
                    <button class="action-btn danger" @click.stop="$emit('delete', node)">删</button>
                </div>
            </div>
            <div v-if="node.children && node.children.length" ref="childrenContainer" :data-parent-id="node.id">
                <tree-node
                    v-for="child in node.children"
                    :key="child.id"
                    :node="child"
                    :depth="depth + 1"
                    :selected-id="selectedId"
                    :selected-ids="selectedIds"
                    :status-map="statusMap"
                    @select="(n) => $emit('select', n)"
                    @toggle-select="(n) => $emit('toggle-select', n)"
                    @rename="(n) => $emit('rename', n)"
                    @delete="(n) => $emit('delete', n)"
                    @reorder="(e) => $emit('reorder', e)"
                ></tree-node>
            </div>
        </div>
    `,
    setup(props, { emit }) {
        const childrenContainer = ref(null);

        const statusClass = (node) => {
            const entry = props.statusMap[node.id];
            const status = (entry && entry.status) || node.status;
            if (!status) return 'pending';
            if (status.startsWith('STALE')) return 'stale';
            if (status === 'completed') {
                // 成品已出，但混音时带了素材且有缺失 → 提示「不完整」
                return entry && entry.mixed_with_assets && entry.missing_assets_count > 0 ? 'missing' : 'dubbed';
            }
            if (status === 'UNKNOWN') return 'pending';
            return 'parsed';
        };

        onMounted(() => {
            if (childrenContainer.value && window.Sortable) {
                new Sortable(childrenContainer.value, {
                    group: 'novel-tree',
                    animation: 150,
                    onEnd: (evt) => {
                        const nodeId = evt.item.dataset.nodeId;
                        const newParentId = evt.to.dataset.parentId || null;
                        emit('reorder', { nodeId, newParentId, newIndex: evt.newIndex });
                    },
                });
            }
        });

        return { childrenContainer, statusClass };
    },
});

app.component('novel-detail-page', {
    props: {
        novelId: String,
    },
    template: `
        <div class="two-pane-layout">
            <div class="tree-pane">
                <div class="tree-header">
                    <h2 class="tree-title">{{ novel?.title || '加载中...' }}</h2>
                    <div class="tree-actions">
                        <button class="btn btn-secondary btn-sm" @click="refreshTree">
                            刷新
                        </button>
                    </div>
                </div>
                <div class="tree-content">
                    <div v-if="loading" class="empty-state">
                        <div class="loading-spinner"></div>
                    </div>
                    <div v-else>
                        <div v-if="novel?.levels?.part" class="mb-2">
                            <button class="btn btn-secondary btn-sm" @click="addPart">
                                + 新增部
                            </button>
                        </div>
                        <div v-if="novel?.levels?.volume" class="mb-2">
                            <button class="btn btn-secondary btn-sm" @click="addVolume">
                                + 新增卷
                            </button>
                        </div>
                        <div class="mb-2">
                            <button class="btn btn-secondary btn-sm" @click="addChapter">
                                + 新增章节
                            </button>
                        </div>
                        <div class="tree-content" ref="treeContainer">
                            <tree-node
                                v-for="node in tree"
                                :key="node.id"
                                :node="node"
                                :selected-id="selectedNode?.id"
                                :selected-ids="selectedNodes"
                                :status-map="statusByChapter"
                                @select="selectNode"
                                @toggle-select="toggleSelect"
                                @rename="renameNode"
                                @delete="deleteNode"
                                @reorder="onReorder"
                            ></tree-node>
                        </div>
                    </div>
                </div>
            </div>
            
            <div class="detail-pane">
                <div class="detail-header">
                    <div v-if="selectedNode">
                        <h3 class="detail-title">{{ selectedNode.title }}</h3>
                        <p class="detail-meta">{{ selectedNode.type === 'chapter' ? '章节' : selectedNode.type === 'part' ? '部' : '卷' }}</p>
                    </div>
                    <div v-else>
                        <h3 class="detail-title">选择节点查看详情</h3>
                    </div>
                </div>

                <div class="content-section">
                    <h4 class="section-title">批量任务</h4>
                    <p class="form-help">范围：{{ scopeLabel }}</p>
                    <div class="flex gap-2">
                        <button class="btn btn-secondary" @click="batchParse">批量解析</button>
                        <button class="btn btn-secondary" @click="batchTTS">批量生成人声</button>
                        <button class="btn btn-secondary" @click="batchMix">批量混音导出</button>
                    </div>
                    <label class="form-checkbox mix-with-assets">
                        <input type="checkbox" v-model="mixWithAssets">
                        <span>混音时叠加背景音/音效（不勾选则遵循「设置」里的默认值，默认只出人声）</span>
                    </label>
                </div>

                <div class="detail-content">
                    <div v-if="selectedChapterIds.length > 1" class="content-section chapter-compare">
                        <h4 class="section-title">已选 {{ selectedChapterIds.length }} 个章节对比</h4>
                        <div class="table-wrap">
                            <table>
                                <thead>
                                    <tr>
                                        <th>章节</th>
                                        <th class="center">原文</th>
                                        <th class="center">解析</th>
                                        <th class="center">配音</th>
                                        <th class="center">分块</th>
                                        <th class="center">已合成</th>
                                        <th class="center">未绑定</th>
                                        <th>混音</th>
                                        <th>状态</th>
                                    </tr>
                                </thead>
                                <tbody>
                                    <tr v-for="row in compareRows" :key="row.id" :data-chapter-id="row.id">
                                        <td>{{ row.title }}</td>
                                        <td class="center">{{ row.raw }}</td>
                                        <td class="center">{{ row.parsed }}</td>
                                        <td class="center">{{ row.dubbed }}</td>
                                        <td class="center">{{ row.segments }}</td>
                                        <td class="center">{{ row.voiced }}</td>
                                        <td class="center" :class="{ danger: row.unboundWarn }">{{ row.unbound }}</td>
                                        <td>{{ row.mix }}</td>
                                        <td>{{ row.status }}</td>
                                    </tr>
                                </tbody>
                            </table>
                        </div>
                    </div>
                    <div v-else-if="selectedNode && selectedNode.type === 'chapter'">
                        <div class="stats-bar mb-4">
                            <div class="stat-item">
                                <span class="stat-value">{{ chapterStats.total_segments || 0 }}</span>
                                <span class="stat-label">总分块</span>
                            </div>
                            <div class="stat-item">
                                <span class="stat-value">{{ chapterStats.dubbed_segments || 0 }}</span>
                                <span class="stat-label">已配音</span>
                            </div>
                            <div class="stat-item">
                                <span class="stat-value">{{ chapterStats.undubbed_segments || 0 }}</span>
                                <span class="stat-label">未配音</span>
                            </div>
                            <div class="stat-item">
                                <span class="stat-value" :class="{ danger: chapterStats.unbound_segments > 0 }">
                                    {{ chapterStats.unbound_segments || 0 }}
                                </span>
                                <span class="stat-label">未绑定角色</span>
                            </div>
                        </div>
                        
                        <div v-if="chapterAssets" class="content-section chapter-assets">
                            <h4 class="section-title">素材</h4>
                            <div class="text-sm">
                                引用背景音 {{ chapterAssets.referenced.bgm.length }} 种、音效
                                {{ chapterAssets.referenced.sfx.length }} 种
                                （{{ chapterAssets.segment_counts.with_bgm }} 个分块带背景音，
                                {{ chapterAssets.segment_counts.with_sfx }} 个带音效）
                            </div>
                            <div v-if="missingAssetNames.length" class="confirm-warning chapter-assets-missing">
                                以下素材还没有生成，混音时会被跳过：{{ missingAssetNames.join('、') }}
                            </div>
                            <div class="text-sm">
                                上次混音：<b>{{ mixStatusLabel }}</b>
                                <span v-if="chapterAssets.mix.mixed_at" class="text-gray">（{{ chapterAssets.mix.mixed_at }}）</span>
                                <span v-if="chapterAssets.mix.stale" class="badge badge-warning">素材引用已变化，需要重新混音</span>
                            </div>
                        </div>

                        <div class="content-section">
                            <h4 class="section-title">章节操作</h4>
                            <div class="flex gap-2">
                                <button class="btn btn-primary" @click="openWorkbench">
                                    进入配音工作台
                                </button>
                                <button class="btn btn-secondary" @click="reimportChapter">
                                    重新导入章节文本
                                </button>
                            </div>
                        </div>
                        
                    </div>
                    <div v-else-if="selectedNode">
                        <div class="content-section">
                            <h4 class="section-title">节点操作</h4>
                            <div class="flex gap-2">
                                <button v-if="selectedNode.type !== 'chapter'" class="btn btn-secondary" @click="addChildNode">
                                    + 新增{{ selectedNode.type === 'part' ? '卷' : '章节' }}
                                </button>
                                <button class="btn btn-secondary" @click="uploadChapter">
                                    上传章节
                                </button>
                            </div>
                        </div>
                    </div>
                    <div v-else class="empty-state">
                        <h3 class="empty-state-title">选择左侧节点查看详情</h3>
                    </div>
                </div>
            </div>
        </div>

        <input type="file" ref="chapterFileInput" accept=".txt,.md" style="display:none" @change="onChapterFileSelected">
        <input type="file" ref="reimportFileInput" accept=".txt,.md" style="display:none" @change="onReimportFileSelected">

        <div class="task-panel" v-if="visibleTasks.length > 0">
            <div class="task-panel-header" @click="toggleTaskPanel">
                <div class="task-panel-title">
                    <span>任务队列</span>
                    <span class="badge badge-primary">{{ visibleTasks.length }}</span>
                </div>
                <span class="task-panel-toggle" :class="{ expanded: taskPanelExpanded }">▼</span>
            </div>
            <div v-if="taskPanelExpanded" class="task-list">
                <template v-for="section in taskSections" :key="section.key">
                    <div v-if="section.groups.length" class="task-section-title">{{ section.label }}</div>
                    <div v-for="group in section.groups" :key="group.id" class="task-group"
                         :class="{ 'task-group-global': section.key === 'global' }">
                        <div class="task-group-header" @click="toggleGroup(group.id)">
                            <div class="task-group-info">
                                <span class="task-group-title">{{ group.title }}</span>
                                <span class="task-group-progress">{{ group.completed }}/{{ group.total }}</span>
                            </div>
                            <div class="task-group-actions">
                                <button class="btn btn-secondary btn-sm" @click.stop="cancelGroup(group.id)">取消</button>
                            </div>
                        </div>
                        <div v-if="expandedGroups.includes(group.id)">
                            <div v-for="task in group.tasks" :key="task.id" class="task-item-wrap">
                                <div class="task-item">
                                    <div class="task-item-info">
                                        <div class="task-item-title">{{ taskTitle(task) }}</div>
                                        <div class="task-item-status">{{ isCancelling(task) ? '取消中…' : taskStateLabel(task.state) }}</div>
                                    </div>
                                    <div class="task-item-actions">
                                        <button class="btn btn-secondary btn-sm task-log-btn" @click="toggleLog(task)">
                                            {{ taskLogs[task.id] && taskLogs[task.id].open ? '收起日志' : '日志' }}
                                        </button>
                                        <button v-if="task.state === 'running' || task.state === 'queued'" class="btn btn-secondary btn-sm task-cancel-btn"
                                                :disabled="isCancelling(task)"
                                                :title="isCancelling(task) ? '正在终止推理进程并恢复 llama-server，最长约 3 分钟' : ''"
                                                @click="cancelTask(task.id)">
                                            {{ isCancelling(task) ? '取消中…' : '取消' }}
                                        </button>
                                    </div>
                                </div>
                                <pre v-if="taskLogs[task.id] && taskLogs[task.id].open" class="task-log">{{ taskLogs[task.id].text || '（暂无日志）' }}</pre>
                            </div>
                        </div>
                    </div>
                </template>
            </div>
        </div>
    `,
    setup(props) {
        const showConfirm = inject('showConfirm');
        const showToast = inject('showToast');
        const showPrompt = inject('showPrompt');

        const novel = ref(null);
        const tree = ref([]);
        const loading = ref(true);
        const selectedNode = ref(null);
        const selectedNodes = ref([]);
        const chapterStats = ref({});
        // chapter_id → 该章状态摘要（GET /tree 的 status.chapters），树状态点和对比表共用
        const statusByChapter = ref({});
        // chapter_id → {segment_count, voiced_count, unbound_count}；只在对比表可见时才去取
        const chapterNumbers = ref({});
        const chapterAssets = ref(null);
        const mixWithAssets = ref(false);
        const missingAssetNames = computed(() => {
            const m = chapterAssets.value?.missing;
            return m ? [...m.bgm, ...m.sfx] : [];
        });
        // mixed_with_assets 为 null 表示没有混音记录（还没混过，或本次改造之前混的）
        const mixStatusLabel = computed(() => {
            const v = chapterAssets.value?.mix.mixed_with_assets;
            if (v === true) return '已叠加背景音/音效';
            if (v === false) return '纯人声';
            return '无记录（未混音或早期产物）';
        });
        const tasks = ref([]);
        const taskPanelExpanded = ref(true);
        const expandedGroups = ref([]);
        const chapterFileInput = ref(null);
        const reimportFileInput = ref(null);
        const pendingChapterTitle = ref('');
        const treeContainer = ref(null);
        let treeSortable = null;

        const CANCEL_WARNING = '排队中的任务会直接取消。配音、素材生成正在运行时，会立即终止推理进程，然后等 llama-server 恢复' +
            '（最长约 3 分钟）任务才显示为已取消；解析等其它任务在当前步骤结束后停下。已合成的语音保留在缓存里，重跑会续上。';

        // Task 数据结构（src/task_queue.py）没有 title/status 字段，
        // 真实字段是 type/state；这里派生一个人类可读的标题和状态文案
        const TASK_TYPE_LABEL = {
            parse: '解析', tts: '生成人声', mix: '混音导出',
            precompute_embedding: '预计算音色', asset_gen: '素材生成',
        };
        const TASK_STATE_LABEL = {
            queued: '排队中', running: '运行中', succeeded: '已完成',
            failed: '失败', cancelled: '已取消',
        };
        // 章节任务：「类型 · 章节 id」；全局任务（asset_gen / precompute_embedding，没有
        // 小说也没有章节）不拼悬空的分隔符，改用 params 里能说明"对谁做"的信息
        const taskDetail = (task) => {
            if (task.chapter_id) return task.chapter_id;
            const p = task.params || {};
            if (task.type === 'asset_gen') return p.only && p.only.length ? p.only.join('、') : '全部待生成的素材';
            if (task.type === 'precompute_embedding') return p.role_id || '';
            return '';
        };
        const taskTitle = (task) => {
            const label = TASK_TYPE_LABEL[task.type] || task.type;
            const detail = taskDetail(task);
            return detail ? `${label} · ${detail}` : label;
        };
        const taskStateLabel = (state) => TASK_STATE_LABEL[state] || state;

        const flattenTree = (nodes) => {
            const out = [];
            for (const node of nodes || []) {
                out.push(node);
                if (node.children) out.push(...flattenTree(node.children));
            }
            return out;
        };

        // 复选框多选的是节点 id，可能混进部/卷；只取真正的章节 id 做批量任务的 scope
        const selectedChapterIds = computed(() =>
            flattenTree(tree.value)
                .filter((n) => n.type === 'chapter' && selectedNodes.value.includes(n.id))
                .map((n) => n.id)
        );

        // 多选对比表：每章一行。「配音」只认 timeline.json（audio_cache_count 只是
        // 缓存 wav 的上界，不代表已配完，所以不拿它当「已配音」）
        const compareRows = computed(() => {
            const mark = (b) => (b ? '✓' : '✗');
            return flattenTree(tree.value)
                .filter((n) => n.type === 'chapter' && selectedNodes.value.includes(n.id))
                .map((n) => {
                    const st = statusByChapter.value[n.id] || {};
                    let mix = '—';
                    if (st.output) {
                        mix = (st.output_format || '').toUpperCase() || '有成品';
                        if (st.mixed_with_assets === true) {
                            mix += st.missing_assets_count > 0
                                ? `（含素材，缺 ${st.missing_assets_count} 个）` : '（含素材）';
                        } else if (st.mixed_with_assets === false) {
                            mix += '（仅人声）';
                        }
                    }
                    // 数字还没取回来时显示 —，不显示 0（0 是一个真实的答案）
                    const num = chapterNumbers.value[n.id];
                    return {
                        id: n.id,
                        title: n.title,
                        raw: mark(st.raw),
                        parsed: st.final ? '定稿' : st.draft ? '初稿' : '✗',
                        dubbed: mark(st.timeline),
                        segments: num ? num.segment_count : '—',
                        voiced: num ? num.voiced_count : '—',
                        unbound: num ? num.unbound_count : '—',
                        unboundWarn: !!num && num.unbound_count > 0,
                        mix,
                        status: st.status || '—',
                    };
                });
        });

        // 批量任务的按钮是常驻的（不依赖具体选中了哪种节点），这里给用户一个
        // 明确的范围提示，跟 buildTaskScope() 的判断逻辑保持一致
        const scopeLabel = computed(() => {
            if (selectedChapterIds.value.length > 0) {
                return `已选中 ${selectedChapterIds.value.length} 个章节`;
            }
            if (selectedNode.value) {
                const typeLabel = selectedNode.value.type === 'chapter' ? '章节'
                    : selectedNode.value.type === 'part' ? '部' : '卷';
                return `${typeLabel}《${selectedNode.value.title}》`;
            }
            return '整本小说';
        });


        // getTasks() 返回队列里所有任务（没有服务端 novel 过滤——SSE 推送也不过滤，
        // 服务端过滤会让 REST 与 SSE 口径不一致），所以这里在客户端只保留本书的任务
        // 和不属于任何小说的全局任务，别的小说的批次不该出现在这本书的面板里
        const visibleTasks = computed(() =>
            tasks.value.filter((t) => t.novel_id === props.novelId || !t.novel_id)
        );

        const taskGroups = computed(() => {
            const groups = {};
            visibleTasks.value.forEach(task => {
                const groupId = task.group_id || task.id;
                if (!groups[groupId]) {
                    groups[groupId] = {
                        id: groupId,
                        global: !task.novel_id,
                        title: task.group_id ? `批量${TASK_TYPE_LABEL[task.type] || task.type}` : taskTitle(task),
                        tasks: [],
                        completed: 0,
                        total: 0,
                    };
                }
                groups[groupId].tasks.push(task);
                groups[groupId].total++;
                if (task.state === 'succeeded' || task.state === 'failed' || task.state === 'cancelled') {
                    groups[groupId].completed++;
                }
            });
            return Object.values(groups);
        });

        const taskSections = computed(() => [
            { key: 'chapter', label: '本书任务', groups: taskGroups.value.filter((g) => !g.global) },
            { key: 'global', label: '全局任务', groups: taskGroups.value.filter((g) => g.global) },
        ]);

        const loadNovel = async () => {
            try {
                novel.value = await API.getNovel(props.novelId);
            } catch (error) {
                console.error('加载小说失败:', error);
            }
        };

        const loadTree = async () => {
            loading.value = true;
            try {
                // GET /tree 返回 {novel: {...真实小说对象，含 tree}, status: {...}}，
                // tree 本身不是数组，取 data.novel.tree 才是真正的节点列表；
                // 后端已经把每个 chapter 节点的 status 内联进去了
                const data = await API.getNovelTree(props.novelId);
                tree.value = data.novel?.tree || [];
                const map = {};
                for (const ch of data.status?.chapters || []) map[ch.chapter_id] = ch;
                statusByChapter.value = map;
            } catch (error) {
                console.error('加载树形结构失败:', error);
            } finally {
                loading.value = false;
                // 每次调用都会经历 loading true->false 的切换，v-if/v-else 因此
                // 每次都会重建 tree-content 的 DOM（不只是首次加载）——之前这里
                // 只在"首次加载完成"时重新挂载 Sortable（用 wasLoading 判断），
                // 导致第一次拖拽触发的 reorder -> loadTree() 刷新会重建 DOM 却
                // 不重新挂 Sortable，第二次开始拖拽全部静默失效。改成每次都重挂。
                nextTick(() => setupTreeSortable());
            }
        };

        const setupTreeSortable = () => {
            if (!treeContainer.value || !window.Sortable) return;
            if (treeSortable) treeSortable.destroy();
            treeSortable = new Sortable(treeContainer.value, {
                group: 'novel-tree',
                animation: 150,
                onEnd: (evt) => {
                    const nodeId = evt.item.dataset.nodeId;
                    const newParentId = evt.to.dataset.parentId || null;
                    onReorder({ nodeId, newParentId, newIndex: evt.newIndex });
                },
            });
        };

        const onReorder = async ({ nodeId, newParentId, newIndex }) => {
            try {
                await API.reorderNodes(props.novelId, { node_id: nodeId, new_parent_id: newParentId, new_index: newIndex });
            } catch (error) {
                console.error('拖拽排序失败:', error);
                showToast?.(`排序失败: ${error.message}`, 'error');
            } finally {
                // 不管成功失败都重新拉一次真实树覆盖渲染：Sortable 已经动过 DOM，
                // 唯一让界面跟服务端状态对齐的办法就是用服务端数据重新画一遍
                await loadTree();
            }
        };

        const loadTasks = async () => {
            try {
                tasks.value = await API.getTasks();
            } catch (error) {
                console.error('加载任务列表失败:', error);
            }
        };

        const refreshTree = () => {
            loadTree();
        };

        // 对比表可见时才取（不进 /tree 热路径）；任务结束后数字会变，所以事件里也刷一次
        const compareVisible = computed(() => selectedChapterIds.value.length > 1);
        const loadChapterNumbers = async () => {
            try {
                chapterNumbers.value = (await API.getChapterStats(props.novelId)).chapters || {};
            } catch (error) {
                console.error('加载章节数值失败:', error);
            }
        };
        watch(compareVisible, (visible) => { if (visible) loadChapterNumbers(); });

        const selectNode = (node) => {
            selectedNode.value = node;
            if (node.type === 'chapter') {
                loadChapterStats(node.id);
            }
        };

        const toggleSelect = (node) => {
            const index = selectedNodes.value.indexOf(node.id);
            if (index === -1) {
                selectedNodes.value.push(node.id);
            } else {
                selectedNodes.value.splice(index, 1);
            }
        };

        const loadChapterStats = async (chapterId) => {
            try {
                // 分块（script_final.json）本身没有"是否已合成"这个字段，
                // 这个信息只在 timeline.json 里；分块列表和时间线是两个独立数据源
                const segments = await API.getSegments(props.novelId, chapterId);
                const total = segments.length;
                const unbound = segments.filter(s => !s.speaker).length;

                let dubbed = 0;
                try {
                    const timeline = await API.getTimeline(props.novelId, chapterId);
                    dubbed = (timeline.items || []).length;
                } catch (e) {
                    // 还没跑过 TTS，timeline.json 不存在，dubbed 保持 0
                }

                chapterStats.value = {
                    total_segments: total,
                    dubbed_segments: dubbed,
                    undubbed_segments: Math.max(total - dubbed, 0),
                    unbound_segments: unbound,
                };
            } catch (error) {
                console.error('加载章节统计失败:', error);
            }
            // 素材引用/缺失/混音状态是独立数据源，失败不影响上面的分块统计
            try {
                chapterAssets.value = await API.getChapterAssets(props.novelId, chapterId);
            } catch (error) {
                chapterAssets.value = null;
            }
        };

        // 新建部/卷/章：弹输入框取名，失败要让用户看到原因（之前只 console.error，界面上毫无反应）
        const createNodeWithPrompt = async (type, label, parentId) => {
            const title = await showPrompt({
                title: `新增${label}`, label: `${label}名称`, placeholder: `请输入${label}名称`, confirmText: '创建',
            });
            if (!title) return;
            try {
                const body = { title, type };
                if (parentId) body.parent_id = parentId;
                await API.createNode(props.novelId, body);
                await loadTree();
            } catch (error) {
                console.error(`创建${label}失败:`, error);
                showToast?.(`创建${label}失败: ${error.message}`, 'error');
            }
        };

        const addPart = () => createNodeWithPrompt('part', '部');
        const addVolume = () => createNodeWithPrompt('volume', '卷');
        const addChapter = () => createNodeWithPrompt('chapter', '章节');

        const renameNode = async (node) => {
            const title = await showPrompt({
                title: '重命名', label: '新名称', value: node.title, confirmText: '保存',
            });
            if (title && title !== node.title) {
                try {
                    await API.updateNode(props.novelId, node.id, { title });
                    await loadTree();
                } catch (error) {
                    console.error('重命名失败:', error);
                    showToast?.(`重命名失败: ${error.message}`, 'error');
                }
            }
        };

        // 删除节点：DELETE 不带 confirm 本来就返回影响预览（{affected_chapters, has_audio, confirmed:false}），
        // 之前是盲确认——用户不知道这一刀会带走几章、有没有已经生成好的音频。
        const deleteNode = async (node) => {
            let preview = null;
            try {
                preview = await API.deleteNode(props.novelId, node.id, false);
            } catch (error) {
                showToast?.(`无法删除: ${error.message}`, 'error');
                return;
            }
            const n = preview.affected_chapters || 0;
            const confirmed = await showConfirm({
                title: '删除节点',
                message: `确定要删除「${node.title}」吗？`,
                warning: (n > 0 ? `将影响 ${n} 个章节${preview.has_audio ? '，其中包含已经生成的音频' : ''}。` : '这里面没有章节。') +
                    '章节数据会被移入回收站（library/<小说>/.trash/），不是永久删除。',
                confirmText: '删除',
                confirmClass: 'btn-danger',
            });
            if (!confirmed) return;
            try {
                await API.deleteNode(props.novelId, node.id, true);
                await loadTree();
                if (selectedNode.value?.id === node.id) {
                    selectedNode.value = null;
                }
            } catch (error) {
                console.error('删除失败:', error);
                showToast?.(`删除失败: ${error.message}`, 'error');
            }
        };

        const addChildNode = () => {
            const type = selectedNode.value.type === 'part' ? 'volume' : 'chapter';
            return createNodeWithPrompt(type, type === 'volume' ? '卷' : '章节', selectedNode.value.id);
        };

        // 章节上传：选中一个部/卷作为父节点时用——先建一个空的 chapter 节点，
        // 再把选中文件的内容 PUT 上去；新节点还没有 raw.txt，upload_raw 会直接
        // 写入，不会触发"重新导入"的覆盖确认。
        // 注意 chapterFileInput.click() 必须还处在「用户手势」窗口里：showPrompt 在确定按钮的
        // 点击处理里同步 resolve，await 之后的这一行仍在同一次点击的手势窗口内。
        const uploadChapter = async () => {
            if (!selectedNode.value) return;
            const title = await showPrompt({
                title: '上传章节', label: '章节名称', placeholder: '请输入章节名称', confirmText: '选择文件',
            });
            if (!title) return;
            pendingChapterTitle.value = title;
            chapterFileInput.value?.click();
        };

        const onChapterFileSelected = async (event) => {
            const file = event.target.files[0];
            event.target.value = '';
            if (!file || !pendingChapterTitle.value) return;
            try {
                const text = await file.text();
                const { node_id } = await API.createNode(props.novelId, {
                    title: pendingChapterTitle.value,
                    type: 'chapter',
                    parent_id: selectedNode.value.id,
                });
                await API.uploadRaw(props.novelId, node_id, text, false);
                showToast?.('章节已创建', 'success');
                await loadTree();
            } catch (error) {
                console.error('上传章节失败:', error);
                showToast?.(`上传失败: ${error.message}`, 'error');
            }
        };

        const openWorkbench = () => {
            window.location.hash = `#/novels/${props.novelId}/chapters/${selectedNode.value.id}`;
        };

        // 重新导入：先不带 confirm 传一次拿到会清掉哪些文件、保留多少缓存，
        // 弹窗把这个真实影响面显示给用户，确认后再带 confirm=true 真正执行
        const reimportChapter = () => {
            if (!selectedNode.value || selectedNode.value.type !== 'chapter') return;
            reimportFileInput.value?.click();
        };

        const onReimportFileSelected = async (event) => {
            const file = event.target.files[0];
            event.target.value = '';
            if (!file) return;
            const chapterId = selectedNode.value.id;
            try {
                const text = await file.text();
                const preview = await API.uploadRaw(props.novelId, chapterId, text, false);
                if (preview.confirmed) {
                    // 该章之前没有 raw.txt，属于新建，已经直接写入了
                    showToast?.('已导入', 'success');
                    await loadTree();
                    await loadChapterStats(chapterId);
                    return;
                }
                const willRemove = (preview.will_remove || []).join('、') || '无';
                const confirmed = await showConfirm({
                    title: '重新导入章节文本',
                    message: `将清空：${willRemove}。`,
                    warning: `已合成的 ${preview.kept_cache_count || 0} 个语音片段会保留在缓存里` +
                        `（正文变了之后不一定还能命中），此操作不可撤销。`,
                    confirmText: '确认导入',
                    confirmClass: 'btn-danger',
                });
                if (!confirmed) return;
                await API.uploadRaw(props.novelId, chapterId, text, true);
                showToast?.('已重新导入', 'success');
                await loadTree();
                await loadChapterStats(chapterId);
            } catch (error) {
                console.error('重新导入失败:', error);
                showToast?.(`重新导入失败: ${error.message}`, 'error');
            }
        };

        // 批量任务范围：优先用复选框多选的章节；没有多选就退到当前选中节点
        // （章节就是那一章，部/卷就是整卷/整部）；都没有就是整本
        const buildTaskScope = () => {
            if (selectedChapterIds.value.length > 0) {
                return { chapter_ids: selectedChapterIds.value };
            }
            if (selectedNode.value) {
                return selectedNode.value.type === 'chapter'
                    ? { chapter_ids: [selectedNode.value.id] }
                    : { node_id: selectedNode.value.id };
            }
            return {};
        };

        const runBatchTask = async (type, label) => {
            const scope = buildTaskScope();
            // 只有混音任务认 params；勾了"叠加素材"才传 with_assets，
            // 不勾就不传，让后端遵循 mixing.voice_only 的配置默认值
            const extra = type === 'mix' && mixWithAssets.value ? { params: { with_assets: true } } : {};
            try {
                const pre = await API.preflightTask({ type, novel_id: props.novelId, scope, ...extra });
                const eta = pre.estimated_gpu_minutes != null
                    ? `约 ${pre.estimated_gpu_minutes} 分钟`
                    : '未知（尚无历史数据）';
                const cacheNote = pre.invalidated_cache_count
                    ? `，将作废 ${pre.invalidated_cache_count} 个已合成片段`
                    : '';
                const confirmed = await showConfirm({
                    title: `批量${label}`,
                    message: `新生成 ${pre.summary.create} 章，${pre.summary.overwrite} 章将被重新生成，` +
                        `${pre.summary.skip} 章无需处理。`,
                    warning: `预计 GPU 耗时：${eta}${cacheNote}`,
                    confirmText: '确认执行',
                    confirmClass: 'btn-primary',
                });
                if (!confirmed) return;
                await API.createTask({ type, novel_id: props.novelId, scope, ...extra });
                showToast?.('任务已提交，可在下方任务队列查看进度', 'success');
                await loadTasks();
            } catch (error) {
                console.error(`批量${label}失败:`, error);
                showToast?.(`提交失败: ${error.message}`, 'error');
            }
        };

        const batchParse = () => runBatchTask('parse', '解析');
        const batchTTS = () => runBatchTask('tts', '生成人声');
        const batchMix = () => runBatchTask('mix', '混音导出');

        // 任务日志：GET /tasks/{id}/log?offset= 是为增量 tail 设计的（返回 next_offset），
        // 只在展开时拉，收起就不再请求——不给几十个任务同时拉日志
        const taskLogs = reactive({});
        const fetchLog = async (id) => {
            const entry = taskLogs[id];
            try {
                const res = await API.getTaskLog(id, entry.offset);
                if (res.text) entry.text += res.text;
                entry.offset = res.next_offset;
            } catch (error) {
                console.error('加载任务日志失败:', error);
            }
        };
        const toggleLog = async (task) => {
            const entry = taskLogs[task.id] || (taskLogs[task.id] = { open: false, text: '', offset: 0, finalFetched: false });
            entry.open = !entry.open;
            if (entry.open) await fetchLog(task.id);
        };

        const toggleTaskPanel = () => {
            taskPanelExpanded.value = !taskPanelExpanded.value;
        };

        const toggleGroup = (groupId) => {
            const index = expandedGroups.value.indexOf(groupId);
            if (index === -1) {
                expandedGroups.value.push(groupId);
            } else {
                expandedGroups.value.splice(index, 1);
            }
        };

        // 已请求取消、但任务还在收尾：要先杀掉推理子进程，再等 llama-server 恢复（最长约 3 分钟）
        const isCancelling = (task) => !!task.cancel_requested && task.state === 'running';

        const cancelGroup = async (groupId) => {
            const group = taskGroups.value.find(g => g.id === groupId);
            if (group) {
                const confirmed = await showConfirm({
                    title: '取消批次',
                    message: `确定要取消该批次的 ${group.tasks.length} 个任务吗？`,
                    warning: CANCEL_WARNING,
                    confirmText: '取消任务',
                    confirmClass: 'btn-danger',
                });
                if (confirmed) {
                    for (const task of group.tasks) {
                        if (task.state === 'running' || task.state === 'queued') {
                            await API.deleteTask(task.id);
                        }
                    }
                    await loadTasks();
                }
            }
        };

        const cancelTask = async (taskId) => {
            const confirmed = await showConfirm({
                title: '取消任务',
                message: '确定要取消该任务吗？',
                warning: CANCEL_WARNING,
                confirmText: '取消任务',
                confirmClass: 'btn-danger',
            });
            if (confirmed) {
                try {
                    await API.deleteTask(taskId);
                    await loadTasks();
                } catch (error) {
                    console.error('取消任务失败:', error);
                }
            }
        };

        // 已处理过终态的混音任务 id：挂载时先把当前已完成的登记进去，之后只对
        // 「新完成」的提示，避免每次进页面都重复 toast
        const seenMixDone = new Set();
        const markMixDone = () => tasks.value
            .filter((t) => t.type === 'mix' && t.state === 'succeeded')
            .map((t) => t.id)
            .filter((id) => !seenMixDone.has(id) && seenMixDone.add(id));

        // task_update SSE 事件在 app.js 里转发成 window 上的 CustomEvent，
        // 收到就整体重新拉一次任务列表（任务量不大，简单可靠优先于精细 patch）
        const onTaskUpdate = async () => {
            await loadTasks();
            if (compareVisible.value) loadChapterNumbers();
            const newlyMixed = tasks.value.filter((t) =>
                t.type === 'mix' && t.novel_id === props.novelId && markMixDone().includes(t.id));
            if (newlyMixed.length) {
                await loadTree();
                const skipped = newlyMixed.filter((t) =>
                    (statusByChapter.value[t.chapter_id] || {}).missing_assets_count > 0).length;
                if (skipped) showToast(`${skipped} 个章节混音时跳过了缺失素材，可到「音效库」生成后重新混音`, 'warning');
                else showToast('混音完成', 'success');
            }
            // 已展开的日志随任务更新增量续拉；任务到终态后再拉最后一次就停
            for (const id of Object.keys(taskLogs)) {
                const entry = taskLogs[id];
                if (!entry.open || entry.finalFetched) continue;
                const task = tasks.value.find((t) => t.id === id);
                await fetchLog(id);
                if (task && ['succeeded', 'failed', 'cancelled'].includes(task.state)) entry.finalFetched = true;
            }
        };

        onMounted(async () => {
            await Promise.all([loadNovel(), loadTree(), loadTasks()]);
            markMixDone();
            window.addEventListener('task-update', onTaskUpdate);
        });

        onUnmounted(() => {
            window.removeEventListener('task-update', onTaskUpdate);
        });

        return {
            novel,
            tree,
            statusByChapter,
            compareRows,
            selectedChapterIds,
            loading,
            selectedNode,
            selectedNodes,
            chapterStats,
            chapterAssets,
            mixWithAssets,
            missingAssetNames,
            mixStatusLabel,
            tasks,
            taskGroups,
            taskSections,
            visibleTasks,
            taskLogs,
            toggleLog,
            taskPanelExpanded,
            expandedGroups,
            chapterFileInput,
            reimportFileInput,
            treeContainer,
            onReorder,
            scopeLabel,
            taskTitle,
            taskStateLabel,
            refreshTree,
            selectNode,
            toggleSelect,
            addPart,
            addVolume,
            addChapter,
            renameNode,
            deleteNode,
            addChildNode,
            uploadChapter,
            onChapterFileSelected,
            onReimportFileSelected,
            openWorkbench,
            reimportChapter,
            batchParse,
            batchTTS,
            batchMix,
            toggleTaskPanel,
            toggleGroup,
            cancelGroup,
            cancelTask,
            isCancelling,
        };
    },
});

app.component('workbench-page', {
    props: {
        novelId: String,
        chapterId: String,
    },
    template: `
        <div class="workbench-layout">
            <div class="segment-list-pane">
                <div class="segment-list-header">
                    <div>
                        <h2 class="segment-list-title">配音工作台</h2>
                        <div class="stats-bar mt-2">
                            <div class="stat-item">
                                <span class="stat-value">{{ stats.total }}</span>
                                <span class="stat-label">总分块</span>
                            </div>
                            <div class="stat-item">
                                <span class="stat-value">{{ stats.dubbed }}</span>
                                <span class="stat-label">已配音</span>
                            </div>
                            <div class="stat-item">
                                <span class="stat-value">{{ stats.undubbed }}</span>
                                <span class="stat-label">未配音</span>
                            </div>
                            <div class="stat-item">
                                <span class="stat-value" :class="{ danger: stats.unbound > 0 }">
                                    {{ stats.unbound }}
                                </span>
                                <span class="stat-label">未绑定角色</span>
                            </div>
                        </div>
                    </div>
                    <div class="segment-list-actions">
                        <button class="btn btn-secondary btn-sm" @click="selectAllUnbound">
                            全选未绑定
                        </button>
                        <button class="btn btn-secondary btn-sm" @click="openNovelDetail">
                            返回小说详情
                        </button>
                    </div>
                </div>
                <div class="segment-list-content" ref="segmentList" @scroll="onSegmentListScroll">
                    <div v-if="loading" class="empty-state">
                        <div class="loading-spinner"></div>
                    </div>
                    <div v-else-if="segments.length === 0" class="empty-state">
                        <h3 class="empty-state-title">暂无分块</h3>
                        <p class="empty-state-description">请先解析该章节</p>
                    </div>
                    <div v-else>
                        <!-- 虚拟滚动：只渲染可视区域附近的卡片，上下用等高的空白 div
                             撑住总滚动高度，长章节（几百上千分块）不会全部塞进 DOM -->
                        <div :style="{ height: topSpacerHeight + 'px' }"></div>
                        <div
                            v-for="segment in visibleSegments"
                            :key="segment.seg_id"
                            class="segment-card"
                            :class="{
                                selected: selectedSegments.includes(segment.seg_id),
                                unbound: !segment.speaker
                            }"
                            @click="selectSegment(segment)"
                        >
                            <div class="segment-header">
                                <span class="segment-id">{{ segment.seg_id }}</span>
                                <span class="segment-status" :class="segment.status"></span>
                                <span v-if="segment.bgm" class="segment-fx" :title="'背景音：' + segment.bgm">bgm</span>
                                <span v-if="segment.sfx" class="segment-fx" :title="'音效：' + segment.sfx">sfx</span>
                            </div>
                            <div class="segment-preview">{{ segment.text?.substring(0, 40) }}...</div>
                            <div class="segment-role" :class="{ unbound: !segment.speaker }">
                                {{ segment.speaker || '⚠ 未绑定' }}
                            </div>
                        </div>
                        <div :style="{ height: bottomSpacerHeight + 'px' }"></div>
                    </div>
                </div>
                <div v-if="selectedSegments.length > 0" class="batch-actions-bar">
                    <div class="batch-actions-info">
                        已选择 {{ selectedSegments.length }} 个分块
                    </div>
                    <div class="batch-actions-buttons">
                        <button class="btn btn-secondary btn-sm" @click="batchBindRole">批量绑定角色</button>
                        <button class="btn btn-secondary btn-sm" @click="batchChangeTone">批量修改语气</button>
                        <button class="btn btn-secondary btn-sm" @click="batchClearRole">批量清空角色</button>
                        <button class="btn btn-secondary btn-sm" @click="openFxDialog">批量设置素材</button>
                        <button class="btn btn-primary btn-sm" @click="batchGenerateTTS">批量生成人声</button>
                    </div>
                </div>
            </div>
            
            <div class="segment-edit-pane">
                <div class="segment-edit-header">
                    <h3 class="segment-edit-title">
                        {{ selectedSegment ? ('编辑分块 ' + selectedSegment.seg_id) : '选择分块进行编辑' }}
                    </h3>
                </div>
                <div class="segment-edit-content">
                    <div v-if="selectedSegment">
                        <div class="form-group">
                            <label class="form-label">原文文本</label>
                            <textarea class="form-textarea" v-model="editForm.text" rows="4"></textarea>
                        </div>
                        
                        <div class="form-group">
                            <label class="form-label">角色绑定</label>
                            <div class="dropdown">
                                <button class="btn btn-secondary dropdown-toggle" @click="showRoleDropdown = !showRoleDropdown">
                                    {{ selectedRoleName || '选择角色' }}
                                </button>
                                <div v-if="showRoleDropdown" class="dropdown-menu">
                                    <div
                                        v-for="role in roles"
                                        :key="role.id"
                                        class="dropdown-item"
                                        @click="selectRole(role)"
                                    >
                                        {{ role.name }}
                                    </div>
                                    <div class="dropdown-divider"></div>
                                    <div class="dropdown-item create-new" @click="createAndBindRole">
                                        + 新建角色并绑定
                                    </div>
                                </div>
                            </div>
                        </div>
                        
                        <div class="form-group">
                            <label class="form-label">语气标签</label>
                            <select class="form-select" v-model="editForm.emotion">
                                <option value="neutral">中性</option>
                                <option value="happy">开心</option>
                                <option value="angry">生气</option>
                                <option value="sad">悲伤</option>
                                <option value="serious">严肃</option>
                                <option value="afraid">害怕</option>
                                <option value="surprised">惊讶</option>
                                <option value="calm">平静</option>
                            </select>
                        </div>
                        
                        <div class="form-row">
                            <div class="form-group">
                                <label class="form-label">背景音</label>
                                <select class="form-select segment-bgm-select" v-model="editForm.bgm">
                                    <option value="">无</option>
                                    <option v-for="opt in bgmOptions" :key="opt.value" :value="opt.value">{{ opt.label }}</option>
                                </select>
                            </div>
                            <div class="form-group">
                                <label class="form-label">音效</label>
                                <select class="form-select segment-sfx-select" v-model="editForm.sfx">
                                    <option value="">无</option>
                                    <option v-for="opt in sfxOptions" :key="opt.value" :value="opt.value">{{ opt.label }}</option>
                                </select>
                            </div>
                        </div>
                        <p class="form-help" v-if="!bgmOptions.length && !sfxOptions.length">
                            素材库里还没有已生成的素材，去「音效库」页定义并生成后再选。
                        </p>

                        <div class="content-section">
                            <h4 class="section-title">试听</h4>
                            <div class="flex gap-2">
                                <button
                                    class="btn btn-secondary"
                                    @click="previewVoice"
                                >
                                    仅人声
                                </button>
                                <button class="btn btn-secondary" @click="previewMixed">
                                    混音预览
                                </button>
                            </div>
                            <audio v-if="audioUrl" :src="audioUrl" controls class="mt-2"></audio>
                        </div>
                        
                        <div class="content-section">
                            <h4 class="section-title">操作</h4>
                            <div class="flex gap-2">
                                <button class="btn btn-secondary" @click="regenerateVoice">
                                    重新生成本块人声
                                </button>
                            </div>
                        </div>
                    </div>
                    <div v-else class="empty-state">
                        <h3 class="empty-state-title">选择左侧分块进行编辑</h3>
                    </div>
                </div>
                <div v-if="selectedSegment" class="segment-edit-actions">
                    <button class="btn btn-secondary" @click="cancelEdit">取消</button>
                    <button class="btn btn-primary" @click="saveSegment">保存</button>
                </div>
            </div>
        </div>

        <div v-if="showFxDialog" class="modal-overlay" @click.self="showFxDialog = false">
            <div class="modal">
                <div class="modal-header">
                    <h2 class="modal-title">批量设置素材（{{ selectedSegments.length }} 个分块）</h2>
                    <button class="modal-close" @click="showFxDialog = false">&times;</button>
                </div>
                <div class="modal-body">
                    <div class="form-group">
                        <label class="form-label">背景音</label>
                        <select class="form-select batch-bgm-select" v-model="fxForm.bgm">
                            <option value="__keep">（不修改）</option>
                            <option value="">（清空）</option>
                            <option v-for="n in assetNames.bgm" :key="n" :value="n">{{ n }}</option>
                        </select>
                    </div>
                    <div class="form-group">
                        <label class="form-label">音效</label>
                        <select class="form-select batch-sfx-select" v-model="fxForm.sfx">
                            <option value="__keep">（不修改）</option>
                            <option value="">（清空）</option>
                            <option v-for="n in assetNames.sfx" :key="n" :value="n">{{ n }}</option>
                        </select>
                    </div>
                </div>
                <div class="modal-footer">
                    <button class="btn btn-secondary" @click="showFxDialog = false">取消</button>
                    <button class="btn btn-primary" @click="applyFx" :disabled="fxForm.bgm === '__keep' && fxForm.sfx === '__keep'">应用</button>
                </div>
            </div>
        </div>
    `,
    setup(props) {
        const showConfirm = inject('showConfirm');
        const showToast = inject('showToast');
        const showPrompt = inject('showPrompt');

        const segments = ref([]);
        const loading = ref(true);
        const selectedSegment = ref(null);
        const selectedSegments = ref([]);
        const roles = ref([]);
        const showRoleDropdown = ref(false);
        const audioUrl = ref('');
        const segmentList = ref(null);
        const dubbedCount = ref(0); // 来自 timeline.json，不是分块自身的字段

        // 虚拟滚动：长章节（100+ 分块）不能把所有卡片都塞进 DOM，否则滚动会卡。
        // 固定行高（跟 CSS 里 .segment-card 的 height 保持一致）+ 只渲染可视区域
        // 附近的卡片，上下各留一段空白 spacer div 撑住总滚动高度，这样滚动条
        // 长度/位置看起来跟真的渲染了全部卡片一样。
        const ROW_HEIGHT = 88; // 必须跟 app.css 里 .segment-card 的 height 对应
        const ROW_BUFFER = 6; // 可视区域上下各多渲染几行，减少快速滚动时的白屏
        const scrollTop = ref(0);
        const viewportHeight = ref(600);

        const visibleRange = computed(() => {
            const total = segments.value.length;
            if (total === 0) return { start: 0, end: 0 };
            const start = Math.max(0, Math.floor(scrollTop.value / ROW_HEIGHT) - ROW_BUFFER);
            const visibleCount = Math.ceil(viewportHeight.value / ROW_HEIGHT) + ROW_BUFFER * 2;
            const end = Math.min(total, start + visibleCount);
            return { start, end };
        });
        const visibleSegments = computed(() =>
            segments.value.slice(visibleRange.value.start, visibleRange.value.end)
        );
        const topSpacerHeight = computed(() => visibleRange.value.start * ROW_HEIGHT);
        const bottomSpacerHeight = computed(() =>
            Math.max(0, segments.value.length - visibleRange.value.end) * ROW_HEIGHT
        );

        const onSegmentListScroll = () => {
            if (segmentList.value) scrollTop.value = segmentList.value.scrollTop;
        };
        const updateViewportHeight = () => {
            if (segmentList.value) viewportHeight.value = segmentList.value.clientHeight || 600;
        };

        const editForm = reactive({
            text: '',
            speaker: '', // 存角色 id，不是显示名
            emotion: 'neutral',
            bgm: '', // 素材名；空串 = 无
            sfx: '',
        });

        // 已生成的素材词表（GET /assets：{bgm:[], sfx:[]}，磁盘上真有 wav 的才在里面）
        const assetNames = ref({ bgm: [], sfx: [] });
        // 分块当前引用的素材如果已经不在词表里（被删了 / 还没生成），也要留在下拉里，
        // 否则打开编辑框那一刻就等于悄悄把它清空了
        const withCurrent = (names, current) => {
            const opts = names.map((n) => ({ value: n, label: n }));
            if (current && !names.includes(current)) opts.unshift({ value: current, label: `${current}（缺失）` });
            return opts;
        };
        const bgmOptions = computed(() => withCurrent(assetNames.value.bgm, editForm.bgm));
        const sfxOptions = computed(() => withCurrent(assetNames.value.sfx, editForm.sfx));

        const loadAssetNames = async () => {
            try {
                assetNames.value = await API.getAssets();
            } catch (error) {
                console.error('加载素材词表失败:', error);
            }
        };

        const selectedRoleName = computed(() => {
            const role = roles.value.find((r) => r.id === editForm.speaker);
            return role ? role.name : editForm.speaker;
        });

        const stats = computed(() => {
            const total = segments.value.length;
            const dubbed = dubbedCount.value;
            const unbound = segments.value.filter(s => !s.speaker).length;
            return {
                total,
                dubbed,
                undubbed: Math.max(total - dubbed, 0),
                unbound,
            };
        });

        const loadSegments = async () => {
            loading.value = true;
            try {
                segments.value = await API.getSegments(props.novelId, props.chapterId);
            } catch (error) {
                console.error('加载分块失败:', error);
            } finally {
                loading.value = false;
            }
            try {
                const timeline = await API.getTimeline(props.novelId, props.chapterId);
                dubbedCount.value = (timeline.items || []).length;
            } catch (error) {
                dubbedCount.value = 0; // 还没跑过 TTS
            }
        };

        const loadRoles = async () => {
            try {
                roles.value = await API.getRoles();
            } catch (error) {
                console.error('加载角色失败:', error);
            }
        };

        const selectSegment = (segment) => {
            selectedSegment.value = segment;
            editForm.text = segment.text || '';
            editForm.speaker = segment.speaker || '';
            editForm.emotion = segment.emotion || 'neutral';
            editForm.bgm = segment.bgm || '';
            editForm.sfx = segment.sfx || '';
            audioUrl.value = '';
        };

        const selectAllUnbound = () => {
            selectedSegments.value = segments.value
                .filter(s => !s.speaker)
                .map(s => s.seg_id);
        };

        const selectRole = (role) => {
            editForm.speaker = role.id; // 存的是角色 id，下拉展示走 selectedRoleName
            showRoleDropdown.value = false;
        };

        const createAndBindRole = async () => {
            showRoleDropdown.value = false;
            const name = await showPrompt({
                title: '新建角色并绑定', label: '角色名称', placeholder: '请输入角色名称', confirmText: '创建并绑定',
            });
            if (!name) return;
            try {
                const { role_id } = await API.createRole({ name });
                editForm.speaker = role_id;
                await loadRoles();
            } catch (error) {
                console.error('创建角色失败:', error);
                showToast?.(`创建角色失败: ${error.message}`, 'error');
            }
        };

        // 分块本身不存 md5，音频文件名（audio_cache/<md5>.wav）是后端按
        // speaker+text+emotion 算出来的，交给后端按 seg_id 去查更可靠
        const previewVoice = () => {
            if (!selectedSegment.value) return;
            audioUrl.value = API.getSegmentAudioUrl(props.novelId, props.chapterId, selectedSegment.value.seg_id);
        };

        // 混音预览就是听当前已有的成品 MP3（如果还没混过音，播放会失败，
        // 提示用户先去小说详情页跑一次批量混音）
        const previewMixed = () => {
            audioUrl.value = API.getOutputAudioUrl(props.novelId, props.chapterId);
        };

        // 分块内容/角色/语气没变的话，TTS 的哈希缓存会直接命中、不会真的
        // 重新合成；真正想强制重录需要先改一下文本或语气再保存，这里只是
        // 把"重新跑一次这一章的增量 TTS"这个动作接上，跟批量生成人声共用
        // 同一条任务提交路径
        const regenerateVoice = async () => {
            if (!selectedSegment.value) return;
            const confirmed = await showConfirm({
                title: '重新生成本块人声',
                message: '会对整章重新跑一次增量 TTS（其余已合成且未改动的分块会命中缓存，不会重新生成）。',
                confirmText: '继续',
                confirmClass: 'btn-primary',
            });
            if (confirmed) {
                try {
                    await API.createTask({ type: 'tts', novel_id: props.novelId, scope: { chapter_ids: [props.chapterId] } });
                    showToast?.('已提交生成任务，可在小说详情页查看进度', 'success');
                } catch (error) {
                    console.error('提交生成任务失败:', error);
                    showToast?.(`提交失败: ${error.message}`, 'error');
                }
            }
        };

        const cancelEdit = () => {
            selectedSegment.value = null;
        };

        const saveSegment = async () => {
            try {
                const payload = {
                    text: editForm.text,
                    speaker: editForm.speaker,
                    emotion: editForm.emotion,
                };
                // 素材字段只在真的改了才发：后端会校验非空素材名必须存在，
                // 分块引用着一个已缺失的素材、用户没动它时，不能因为这条校验保存失败
                const orig = selectedSegment.value;
                const fxChanged = [];
                for (const key of ['bgm', 'sfx']) {
                    if ((editForm[key] || null) !== (orig[key] || null)) {
                        payload[key] = editForm[key] || null;
                        fxChanged.push(key);
                    }
                }
                await API.updateSegment(props.novelId, props.chapterId, orig.seg_id, payload);
                if (fxChanged.length) await syncTimelineAssets();
                await loadSegments();
                selectedSegment.value = null;
            } catch (error) {
                console.error('保存分块失败:', error);
                showToast?.(`保存失败: ${error.message}`, 'error');
            }
        };

        const openNovelDetail = () => {
            window.location.hash = `#/novels/${props.novelId}`;
        };

        const batchBindRole = async () => {
            // 以前是让用户手敲角色名，敲错一个字就只能得到「没有找到」——改成从现有角色里选
            if (roles.value.length === 0) {
                showToast?.('还没有任何角色，请先在角色库或上面的下拉里新建', 'error');
                return;
            }
            const roleId = await showPrompt({
                title: '批量绑定角色', label: `给选中的 ${selectedSegments.value.length} 个分块绑定角色`,
                options: roles.value.map((r) => ({ value: r.id, label: r.name })), confirmText: '绑定',
            });
            if (!roleId) return;
            try {
                await API.batchUpdateSegments(props.novelId, props.chapterId, {
                    seg_ids: selectedSegments.value,
                    set: { speaker: roleId },
                });
                await loadSegments();
                selectedSegments.value = [];
            } catch (error) {
                console.error('批量绑定角色失败:', error);
                showToast?.(`批量绑定失败: ${error.message}`, 'error');
            }
        };

        // 素材改动只写进 script_final.json，而混音读的是 TTS 时打好快照的
        // timeline.json——保存后顺手同步过去，否则改了素材要重新 TTS 才生效。
        // 还没跑过 TTS（404）就没有 timeline 可同步，静默跳过。
        const syncTimelineAssets = async () => {
            try {
                const res = await API.refreshTimelineAssets(props.novelId, props.chapterId);
                if (res.updated) showToast?.('素材已保存，并同步到时间线', 'success');
            } catch (error) {
                // 无时间线是正常情况，不打扰用户
            }
        };

        const showFxDialog = ref(false);
        const fxForm = reactive({ bgm: '__keep', sfx: '__keep' });
        const openFxDialog = () => {
            fxForm.bgm = '__keep';
            fxForm.sfx = '__keep';
            showFxDialog.value = true;
        };
        const applyFx = async () => {
            const set = {};
            if (fxForm.bgm !== '__keep') set.bgm = fxForm.bgm || null;
            if (fxForm.sfx !== '__keep') set.sfx = fxForm.sfx || null;
            try {
                await API.batchUpdateSegments(props.novelId, props.chapterId, {
                    seg_ids: selectedSegments.value, set,
                });
                await syncTimelineAssets();
                await loadSegments();
                selectedSegments.value = [];
                showFxDialog.value = false;
            } catch (error) {
                showToast?.(`批量设置素材失败: ${error.message}`, 'error');
            }
        };

        const batchChangeTone = async () => {
            // 以前是让用户手敲英文标签，写错一个字母后端就存进一个不存在的语气
            const emotion = await showPrompt({
                title: '批量修改语气', label: `给选中的 ${selectedSegments.value.length} 个分块设置语气`,
                options: EMOTION_OPTIONS, confirmText: '修改',
            });
            if (emotion) {
                try {
                    await API.batchUpdateSegments(props.novelId, props.chapterId, {
                        seg_ids: selectedSegments.value,
                        set: { emotion },
                    });
                    await loadSegments();
                    selectedSegments.value = [];
                } catch (error) {
                    console.error('批量修改语气失败:', error);
                    showToast?.(`批量修改失败: ${error.message}`, 'error');
                }
            }
        };

        const batchClearRole = async () => {
            const confirmed = await showConfirm({
                title: '批量清空角色',
                message: `确定要清空选中的 ${selectedSegments.value.length} 个分块的角色绑定吗？`,
                warning: '清空后这些分块会变成「未绑定」，需要重新指派角色才能生成人声。',
                confirmText: '清空',
                confirmClass: 'btn-danger',
            });
            if (confirmed) {
                try {
                    await API.batchUpdateSegments(props.novelId, props.chapterId, {
                        seg_ids: selectedSegments.value,
                        set: { speaker: null },
                    });
                    await loadSegments();
                    selectedSegments.value = [];
                } catch (error) {
                    console.error('批量清空角色失败:', error);
                    showToast?.(`批量清空失败: ${error.message}`, 'error');
                }
            }
        };

        const batchGenerateTTS = async () => {
            const unboundCount = segments.value
                .filter(s => selectedSegments.value.includes(s.seg_id) && !s.speaker)
                .length;

            if (unboundCount > 0) {
                showToast?.(`选中的分块中有 ${unboundCount} 个未绑定角色，请先指派`, 'error');
                return;
            }

            // 分块粒度的 TTS 提交在后端不存在——tts 任务是整章级别的增量合成，
            // 已缓存、未改动的分块会自动跳过；这里就是把"生成本章人声"这个
            // 动作接上，语义上覆盖"批量生成人声"这颗按钮的诉求
            try {
                const pre = await API.preflightTask({ type: 'tts', novel_id: props.novelId, scope: { chapter_ids: [props.chapterId] } });
                const eta = pre.estimated_gpu_minutes != null ? `约 ${pre.estimated_gpu_minutes} 分钟` : '未知（尚无历史数据）';
                const confirmed = await showConfirm({
                    title: '批量生成人声',
                    message: `本章共 ${pre.summary.create + pre.summary.overwrite + pre.summary.skip} 个分块，将增量合成未缓存的部分。`,
                    warning: `预计 GPU 耗时：${eta}`,
                    confirmText: '确认执行',
                    confirmClass: 'btn-primary',
                });
                if (!confirmed) return;
                await API.createTask({ type: 'tts', novel_id: props.novelId, scope: { chapter_ids: [props.chapterId] } });
                showToast?.('任务已提交，可在小说详情页查看进度', 'success');
                selectedSegments.value = [];
            } catch (error) {
                console.error('提交批量生成失败:', error);
                showToast?.(`提交失败: ${error.message}`, 'error');
            }
        };

        onMounted(async () => {
            await Promise.all([loadSegments(), loadRoles(), loadAssetNames()]);
            nextTick(() => updateViewportHeight());
            window.addEventListener('resize', updateViewportHeight);
        });
        onUnmounted(() => {
            window.removeEventListener('resize', updateViewportHeight);
        });

        return {
            segments,
            loading,
            selectedSegment,
            selectedSegments,
            roles,
            showRoleDropdown,
            audioUrl,
            editForm,
            assetNames,
            bgmOptions,
            sfxOptions,
            showFxDialog,
            fxForm,
            openFxDialog,
            applyFx,
            selectedRoleName,
            stats,
            segmentList,
            visibleSegments,
            topSpacerHeight,
            bottomSpacerHeight,
            onSegmentListScroll,
            selectSegment,
            selectAllUnbound,
            selectRole,
            createAndBindRole,
            previewVoice,
            previewMixed,
            regenerateVoice,
            cancelEdit,
            saveSegment,
            openNovelDetail,
            batchBindRole,
            batchChangeTone,
            batchClearRole,
            batchGenerateTTS,
        };
    },
});

// 嵌套分类树的左栏：行列表 + 新建/改名对话框。树的状态与增删改逻辑在
// category-tree.js 的 useCategoryTree 里，页面把 rows/collapsed/save 传进来。
// 对话框放在组件内部——它只关心「输入一个名字」，保存由页面给的 save(mode, row, title)
// 完成（返回 false 表示服务端拒绝了，如重名，对话框留着让用户改）。
app.component('category-tree-pane', {
    props: {
        rows: { type: Array, required: true },
        selectedPath: { type: String, default: null },
        collapsed: { type: Object, default: () => ({}) },
        allLabel: { type: String, default: '全部' },
        addLabel: { type: String, default: '+ 新建分类' },
        // 后端 category_tree.MAX_DEPTH：最多 4 层，第 4 层（depth 3）不能再建子分类
        maxDepth: { type: Number, default: 4 },
        save: { type: Function, required: true },
    },
    emits: ['update:selectedPath', 'toggle-collapse', 'remove'],
    template: `
        <div class="category-pane">
            <div class="category-list">
                <div class="category-row" :class="{ active: selectedPath === null }" @click="$emit('update:selectedPath', null)">
                    <span class="category-row-title">{{ allLabel }}</span>
                </div>
                <div v-for="row in rows" :key="row.id" class="category-row"
                     :class="{ active: selectedPath === row.path }"
                     :style="{ paddingLeft: (10 + row.depth * 18) + 'px' }"
                     :data-category-path="row.path"
                     @click="$emit('update:selectedPath', row.path)">
                    <button class="tree-toggle" @click.stop="$emit('toggle-collapse', row.id)" :style="{ visibility: row.hasChildren ? 'visible' : 'hidden' }">
                        {{ collapsed[row.id] ? '▸' : '▾' }}
                    </button>
                    <span class="category-row-title">{{ row.title }}</span>
                    <button v-if="row.depth < maxDepth - 1" class="action-btn" title="新建子分类" @click.stop="openDialog('create', row)">＋</button>
                    <button class="action-btn" @click.stop="openDialog('rename', row)">改</button>
                    <button class="action-btn danger" @click.stop="$emit('remove', row)">删</button>
                </div>
                <div class="tree-add-row">
                    <button class="tree-add-btn" @click="openDialog('create', null)">{{ addLabel }}</button>
                </div>
            </div>
        </div>

        <div v-if="dialog" class="modal-overlay" @click.self="dialog = null">
            <div class="modal">
                <div class="modal-header">
                    <h2 class="modal-title">{{ dialogTitle }}</h2>
                    <button class="modal-close" @click="dialog = null">&times;</button>
                </div>
                <div class="modal-body">
                    <div class="form-group">
                        <label class="form-label">分类名称</label>
                        <input type="text" class="form-input category-name-input" v-model="dialog.title"
                               @keyup.enter="submit" placeholder="不能包含 /">
                    </div>
                </div>
                <div class="modal-footer">
                    <button class="btn btn-secondary" @click="dialog = null">取消</button>
                    <button class="btn btn-primary" @click="submit" :disabled="!dialog.title.trim()">保存</button>
                </div>
            </div>
        </div>
    `,
    setup(props) {
        const dialog = ref(null);
        const openDialog = (mode, row) => {
            dialog.value = { mode, row, title: mode === 'rename' ? row.title : '' };
        };
        const dialogTitle = computed(() => {
            const d = dialog.value;
            if (!d) return '';
            if (d.mode === 'rename') return '重命名分类';
            return d.row ? '新建子分类（' + d.row.path + '）' : '新建分类';
        });
        const submit = async () => {
            const d = dialog.value;
            if (!d || !d.title.trim()) return;
            if (await props.save(d.mode, d.row, d.title)) dialog.value = null;
        };
        return { dialog, openDialog, dialogTitle, submit };
    },
});

// 角色库：左侧嵌套分类树（GET/PUT /role-category-tree，整树替换）+ 右侧角色卡片。
// 角色自己的 category 字段存分类"路径"字符串（如 "主角/男主"，旧数据是扁平名字如
// "主角"）；按分类筛选时匹配路径本身及其子路径。删除/改名分类节点不会改动角色
// 已有的 category——后端沿用"悬空引用容忍"策略，角色只是引用了一个不在树里的名字。
app.component('roles-page', {
    template: `
        <div class="roles-page">
        <div class="page-header roles-header">
            <h1 class="page-title">全局角色库</h1>
            <div class="page-actions">
                <button class="btn btn-primary" @click="showCreateDialog">+ 新增角色</button>
            </div>
        </div>

        <div class="two-pane-layout">
            <category-tree-pane
                :rows="flatRows"
                v-model:selectedPath="selectedPath"
                :collapsed="collapsed"
                :save="saveNode"
                @toggle-collapse="toggleCollapse"
                @remove="removeNode"
            ></category-tree-pane>

            <div class="content-pane">
                <div class="content-pane-header">
                    <h3 class="content-pane-title">{{ selectedPath === null ? '全部角色' : selectedPath }}</h3>
                </div>

                <div v-if="tagStats.length" class="flex gap-2 mb-4 tag-filter-row" style="flex-wrap: wrap; align-items: center;">
                    <span class="text-sm text-gray">标签筛选</span>
                    <button v-for="t in tagStats" :key="t.name" class="tag-filter-chip"
                            :class="{ active: tagFilters.includes(t.name) }" @click="toggleTagFilter(t.name)">
                        {{ t.name }} {{ t.count }}
                    </button>
                </div>

                <div v-if="loading" class="empty-state"><div class="loading-spinner"></div></div>
                <div v-else-if="filteredRoles.length === 0" class="empty-state">
                    <h3 class="empty-state-title">暂无角色</h3>
                    <p class="empty-state-description">{{ roles.length ? '当前分类/标签下没有角色' : '点击上方按钮创建第一个角色' }}</p>
                </div>
                <div v-else class="card-grid">
                    <div v-for="role in filteredRoles" :key="role.id" class="role-card">
                        <div class="role-card-header">
                            <div>
                                <div class="role-name">{{ role.name }}</div>
                                <div class="role-meta">{{ role.category || '未分类' }} · {{ genderLabel(role.gender) }}</div>
                            </div>
                            <div class="flex gap-2">
                                <button class="btn btn-secondary btn-sm" @click="editRole(role)">编辑</button>
                                <button class="btn btn-danger btn-sm" @click="deleteRole(role)" :disabled="role.name === 'narrator' || role.id === 'narrator'">删除</button>
                            </div>
                        </div>
                        <div v-if="(role.tags || []).length" class="flex gap-2" style="flex-wrap: wrap;">
                            <span v-for="tag in role.tags" :key="tag" class="tag-chip">{{ tag }}</span>
                        </div>
                        <div v-if="role.has_reference" class="audio-player">
                            <audio :src="referenceUrl(role.id)" controls></audio>
                        </div>
                        <div class="role-stats">
                            <span class="embedding-status" :class="embeddingClass(role.embedding_status)">
                                {{ embeddingLabel(role.embedding_status) }}
                            </span>
                            <button v-if="!(role.embedding_status && role.embedding_status.valid)"
                                    class="btn btn-secondary btn-sm precompute-embedding-btn"
                                    :disabled="!role.has_reference || !!embeddingTasks[role.id]"
                                    :title="role.has_reference ? '' : '没有参考音频，无法预计算'"
                                    @click="precomputeEmbedding(role)">
                                {{ embeddingButtonLabel(role) }}
                            </button>
                            <span class="ml-2">
                                被 {{ (role.novels || []).length }} 本小说、{{ role.segment_count || 0 }} 个分块引用
                            </span>
                        </div>
                    </div>
                </div>
            </div>
        </div>

        <div v-if="showDialog" class="modal-overlay" @click.self="closeDialog">
            <div class="modal">
                <div class="modal-header">
                    <h2 class="modal-title">{{ editingRole ? '编辑角色' : '新增角色' }}</h2>
                    <button class="modal-close" @click="closeDialog">&times;</button>
                </div>
                <div class="modal-body">
                    <div class="form-group">
                        <label class="form-label">角色名称</label>
                        <input type="text" class="form-input" v-model="form.name" placeholder="请输入角色名称">
                    </div>
                    <div class="form-row">
                        <div class="form-group">
                            <label class="form-label">分类</label>
                            <select class="form-select role-category-select" v-model="form.category">
                                <option value="">未分类</option>
                                <option v-for="opt in categoryOptions" :key="opt.value" :value="opt.value">{{ opt.label }}</option>
                            </select>
                            <p class="form-help">没有想要的分类？先在左侧分类树里新建</p>
                        </div>
                        <div class="form-group">
                            <label class="form-label">性别</label>
                            <select class="form-select" v-model="form.gender">
                                <option value="">未知</option>
                                <option value="male">男</option>
                                <option value="female">女</option>
                            </select>
                        </div>
                    </div>
                    <div class="form-group">
                        <label class="form-label">标签</label>
                        <div class="flex gap-2 mb-2" style="flex-wrap: wrap;">
                            <span v-for="tag in form.tags" :key="tag" class="tag-chip">
                                {{ tag }}<button class="tag-chip-remove" @click="removeTag(tag)">&times;</button>
                            </span>
                        </div>
                        <input type="text" class="form-input role-tag-input" v-model="tagInput"
                               @keydown.enter.prevent="addTag" placeholder="输入标签后按回车">
                    </div>
                    <div class="form-group">
                        <label class="form-label">语速</label>
                        <input type="number" class="form-input" v-model="form.speed" step="0.1" min="0.5" max="2.0">
                    </div>
                    <div class="form-group">
                        <label class="form-label">备注</label>
                        <textarea class="form-textarea" v-model="form.notes" placeholder="请输入备注"></textarea>
                    </div>
                    <div class="form-group">
                        <label class="form-label">参考音频</label>
                        <input type="file" accept="audio/*" @change="handleFileUpload">
                        <p class="form-help">只允许 1 条参考音频，新上传自动覆盖旧的</p>
                    </div>
                </div>
                <div class="modal-footer">
                    <button class="btn btn-secondary" @click="closeDialog">取消</button>
                    <button class="btn btn-primary" @click="saveRole" :disabled="!form.name.trim()">
                        {{ editingRole ? '保存' : '创建' }}
                    </button>
                </div>
            </div>
        </div>
        </div>
    `,
    setup() {
        const showToast = inject('showToast');
        const showConfirm = inject('showConfirm');

        const roles = ref([]);
        const loading = ref(true);
        const tagStats = ref([]);
        const tagFilters = ref([]);

        const showDialog = ref(false);
        const editingRole = ref(null);
        const tagInput = ref('');
        const form = reactive({
            name: '', category: '', gender: '', speed: 1.0, notes: '',
            tags: [], reference_audio: null,
        });

        // ---- 分类树（逻辑在 category-tree.js，界面是 category-tree-pane）----
        const {
            tree, collapsed, selectedPath, flatRows, pathOptions, matchesPath,
            toggleCollapse, reload: loadTree, saveNode, removeNode,
        } = CategoryTree.useCategoryTree({
            load: () => API.getRoleCategoryTree(),
            save: (t) => API.putRoleCategoryTree(t),
            showToast,
            showConfirm,
            deleteWarning: '已经使用这些分类的角色不会被清空——它们保留原来的分类名，只是不再出现在分类树里（编辑时显示为「未登记」）。',
        });
        // 角色身上还挂着、但已经不在树里的分类名也要出现在下拉里
        const categoryOptions = computed(() => pathOptions(roles.value.map((r) => r.category)));

        // ---- 角色列表 / 筛选 ----
        const filteredRoles = computed(() => roles.value.filter((r) => {
            if (!matchesPath(r.category)) return false;
            if (tagFilters.value.length && !tagFilters.value.some((t) => (r.tags || []).includes(t))) return false;
            return true;
        }));

        const toggleTagFilter = (name) => {
            const i = tagFilters.value.indexOf(name);
            if (i >= 0) tagFilters.value.splice(i, 1); else tagFilters.value.push(name);
        };

        const genderLabel = (g) => ({ male: '男', female: '女' }[g] || '未知');
        const referenceUrl = (roleId) => API.getRoleReferenceUrl(roleId);

        const embeddingLabel = (status) => {
            if (!status) return '⚪ 未知';
            if (status.valid) return '✓ Embedding 有效';
            if (!status.exists) return '⚪ 尚未预计算（首次使用时自动生成）';
            return `✗ Embedding 已过期（${status.stale_reason || '需要重新预计算'}）`;
        };
        const embeddingClass = (status) => {
            if (!status) return 'unknown';
            if (status.valid) return 'valid';
            if (!status.exists) return 'pending';
            return 'invalid';
        };

        const loadRoles = async () => {
            loading.value = true;
            try {
                roles.value = await API.getRoles();
            } catch (error) {
                console.error('加载角色失败:', error);
            } finally {
                loading.value = false;
            }
        };

        // ---- 预计算音色（embedding）----
        // 全局任务（没有小说/章节），所以这里没有别的地方能看到它的进度：用 assets-page 同一套
        // SSE 模式——task-update 事件按 params.role_id 记到一张表里，终态时刷新角色并 toast。
        // 不走 preflight（对全局任务它只返回写死的 create:1，弹窗没有信息量），直接提交。
        const embeddingTasks = reactive({});
        const TERMINAL_STATES = ['succeeded', 'failed', 'cancelled'];
        // 任务可能失败得比 createTask 的响应还快（比如环境没就绪，毫秒级失败）：SSE 的终态事件
        // 先到、把条目清掉，随后响应里那份「排队中」的提交时快照又把它写回去，按钮就永远卡在
        // 「排队中…」。记下已经见过终态的任务 id，响应晚到时不再写回。
        const finishedTaskIds = new Set();

        const embeddingButtonLabel = (role) => {
            const t = embeddingTasks[role.id];
            if (!t) return '预计算音色';
            return t.state === 'queued' ? '排队中…' : '预计算中…';
        };

        const precomputeEmbedding = async (role) => {
            const confirmed = await showConfirm({
                title: `预计算「${role.name}」的音色`,
                message: '会用 IndexTTS 为该角色的参考音频提取音色特征，并写入缓存，之后合成时直接复用。',
                warning: '这是一次 GPU 推理：会临时停掉 llama-server 腾显存、完成后自动恢复；' +
                    'GPU 通道是单线程的，若有解析/配音任务在跑，会排在它们后面。',
                confirmText: '开始预计算',
                confirmClass: 'btn-primary',
            });
            if (!confirmed) return;
            try {
                const res = await API.createTask({ type: 'precompute_embedding', params: { role_id: role.id } });
                if (!finishedTaskIds.has(res.tasks[0].id)) embeddingTasks[role.id] = res.tasks[0];
            } catch (error) {
                showToast?.(`提交失败: ${error.message}`, 'error');
            }
        };

        const onTaskUpdate = (event) => {
            const task = event.detail?.task;
            const roleId = task?.params?.role_id;
            if (!task || task.type !== 'precompute_embedding' || !roleId) return;
            if (!TERMINAL_STATES.includes(task.state)) {
                embeddingTasks[roleId] = task;
                return;
            }
            finishedTaskIds.add(task.id);
            delete embeddingTasks[roleId];
            loadRoles(); // 刷新 embedding_status
            const name = (roles.value.find((r) => r.id === roleId) || {}).name || roleId;
            if (task.state === 'succeeded') showToast?.(`「${name}」的音色预计算完成`, 'success');
            else if (task.state === 'cancelled') showToast?.(`「${name}」的音色预计算已取消`, 'success');
            else showToast?.(`「${name}」的音色预计算失败: ${task.error || '未知错误'}`, 'error');
        };

        const loadTagStats = async () => {
            try {
                tagStats.value = (await API.getRoleTags()).tags || [];
                // 标签被删光后，别让筛选停在一个不存在的标签上
                tagFilters.value = tagFilters.value.filter((t) => tagStats.value.some((s) => s.name === t));
            } catch (error) {
                console.error('加载标签失败:', error);
            }
        };

        // ---- 角色表单 ----
        const showCreateDialog = () => {
            editingRole.value = null;
            Object.assign(form, {
                name: '',
                // 当前正筛选着某个分类时，新建角色默认落在这个分类下
                category: selectedPath.value || '',
                gender: '', speed: 1.0, notes: '', tags: [], reference_audio: null,
            });
            tagInput.value = '';
            showDialog.value = true;
        };

        const editRole = (role) => {
            editingRole.value = role;
            Object.assign(form, {
                name: role.name, category: role.category || '', gender: role.gender || '',
                speed: role.speed || 1.0, notes: role.description || '',
                tags: [...(role.tags || [])], reference_audio: null,
            });
            tagInput.value = '';
            showDialog.value = true;
        };

        const closeDialog = () => {
            showDialog.value = false;
            editingRole.value = null;
        };

        const addTag = () => {
            const t = tagInput.value.trim();
            if (t && !form.tags.includes(t)) form.tags.push(t);
            tagInput.value = '';
        };
        const removeTag = (tag) => {
            form.tags = form.tags.filter((t) => t !== tag);
        };

        const handleFileUpload = (event) => {
            const file = event.target.files[0];
            if (file) form.reference_audio = file;
        };

        const saveRole = async () => {
            addTag(); // 输入框里还没按回车的标签也算上
            try {
                let roleId;
                if (editingRole.value) {
                    // RoleUpdate 只认 name/category/description/speed/tags，没有 gender
                    await API.updateRole(editingRole.value.id, {
                        name: form.name, category: form.category, speed: form.speed,
                        description: form.notes, tags: form.tags,
                    });
                    roleId = editingRole.value.id;
                } else {
                    // RoleCreate 只认 name/gender/category/description/tags，没有 speed
                    // （语速是注册之后再通过 update 设的）
                    const { role_id } = await API.createRole({
                        name: form.name, category: form.category, gender: form.gender,
                        description: form.notes, tags: form.tags,
                    });
                    roleId = role_id;
                    if (form.speed) await API.updateRole(roleId, { speed: form.speed });
                }

                if (form.reference_audio) {
                    await API.uploadRoleReference(roleId, form.reference_audio);
                }

                closeDialog();
                await Promise.all([loadRoles(), loadTagStats()]);
            } catch (error) {
                console.error('保存角色失败:', error);
                showToast?.(`保存角色失败: ${error.message}`, 'error');
            }
        };

        const deleteRole = async (role) => {
            if (role.id === 'narrator' || role.name === 'narrator') {
                showToast?.('narrator 角色不允许删除', 'error');
                return;
            }
            const confirmed = await showConfirm({
                title: '删除角色',
                message: `确定要删除角色「${role.name}」吗？`,
                warning: `该角色被 ${role.segment_count || 0} 个分块引用，删除后这些分块会变成未绑定。`,
                confirmText: '删除',
                confirmClass: 'btn-danger',
            });
            if (!confirmed) return;
            try {
                await API.deleteRole(role.id, true);
                await Promise.all([loadRoles(), loadTagStats()]);
            } catch (error) {
                showToast?.(`删除失败: ${error.message}`, 'error');
            }
        };

        onMounted(async () => {
            window.addEventListener('task-update', onTaskUpdate);
            loadRoles();
            loadTree();
            loadTagStats();
            // 切走再回来 / 刷新页面时，接上还在跑的预计算任务
            try {
                for (const t of await API.getTasks()) {
                    if (t.type === 'precompute_embedding' && t.params?.role_id && !TERMINAL_STATES.includes(t.state)) {
                        embeddingTasks[t.params.role_id] = t;
                    }
                }
            } catch (error) {
                console.error('加载任务失败:', error);
            }
        });
        onUnmounted(() => window.removeEventListener('task-update', onTaskUpdate));

        return {
            roles, loading, tree, collapsed, flatRows, selectedPath, tagStats, tagFilters,
            filteredRoles, categoryOptions, showDialog, editingRole, form, tagInput,
            toggleCollapse, saveNode, removeNode,
            toggleTagFilter, genderLabel, referenceUrl, embeddingLabel, embeddingClass,
            showCreateDialog, editRole, closeDialog, addTag, removeTag, handleFileUpload,
            saveRole, deleteRole,
            embeddingTasks, embeddingButtonLabel, precomputeEmbedding,
        };
    },
});

// 背景音/音效库：素材规格（assets/asset_specs.yaml）的增删改查 + 触发生成任务。
// 布局与角色库一致：左侧嵌套分类树（GET/PUT /asset-category-tree）+ 右侧卡片；
// 素材自己的 category 存路径字符串、tags 存标签数组，都只是整理用元数据，不参与
// spec_hash。删除/改名分类节点不改素材已有的 category（悬空引用容忍）。
// 生成状态 MISSING / STALE / OK 来自后端 get_asset_status_list。
app.component('assets-page', {
    template: `
        <div class="assets-page">
        <div class="page-header roles-header">
            <h1 class="page-title">背景音与音效库</h1>
            <div class="page-actions">
                <button class="btn btn-secondary" @click="generate(null)" :disabled="!!genTask">生成待生成的素材</button>
                <button class="btn btn-primary" @click="openCreate">+ 新增素材</button>
            </div>
        </div>

        <div class="two-pane-layout">
        <category-tree-pane
            :rows="flatRows"
            v-model:selectedPath="selectedPath"
            :collapsed="collapsed"
            :save="saveNode"
            @toggle-collapse="toggleCollapse"
            @remove="removeNode"
        ></category-tree-pane>

        <div class="content-pane">
        <div class="content-pane-header">
            <h3 class="content-pane-title">{{ selectedPath === null ? '全部素材' : selectedPath }}</h3>
        </div>

        <div v-if="genTask" class="info-banner asset-gen-banner">
            <div class="flex items-center justify-between gap-2">
                <span>
                    <b>素材生成{{ genTask.cancel_requested && genTask.state === 'running' ? '取消中…' : genTask.state === 'queued' ? '排队中' : '进行中' }}</b>
                    <span v-if="genTask.progress">
                        {{ genTask.progress.done }}/{{ genTask.progress.total }} {{ genTask.progress.message }}
                    </span>
                </span>
                <button class="btn btn-secondary btn-sm" @click="cancelGen"
                        :disabled="!!genTask.cancel_requested && genTask.state === 'running'">取消</button>
            </div>
            <div class="progress-bar mt-2"><div class="progress-fill" :style="{ width: genPercent + '%' }"></div></div>
        </div>

        <div class="flex gap-2 mb-4" style="flex-wrap: wrap; align-items: center;">
            <span v-for="f in kindFilters" :key="f.value" class="category-tag"
                  :class="{ active: kindFilter === f.value }" @click="kindFilter = f.value">{{ f.label }}</span>
            <template v-if="tagStats.length">
                <span class="text-sm text-gray">标签筛选</span>
                <button v-for="t in tagStats" :key="t.name" class="tag-filter-chip"
                        :class="{ active: tagFilters.includes(t.name) }" @click="toggleTagFilter(t.name)">
                    {{ t.name }} {{ t.count }}
                </button>
            </template>
        </div>

        <div v-if="loading" class="empty-state"><div class="loading-spinner"></div></div>
        <div v-else-if="visibleSpecs.length === 0" class="empty-state">
            <h3 class="empty-state-title">还没有素材</h3>
            <p class="empty-state-description">点击「新增素材」定义一条背景音或音效，再生成音频</p>
        </div>
        <div v-else class="card-grid">
            <div v-for="spec in visibleSpecs" :key="spec.kind + '/' + spec.name" class="asset-card">
                <div class="flex items-center justify-between gap-2">
                    <span class="asset-kind-badge">{{ kindLabel(spec.kind) }}</span>
                    <span class="badge" :class="statusInfo(spec.status).cls">{{ statusInfo(spec.status).label }}</span>
                </div>
                <div class="asset-name">{{ spec.name }}</div>
                <div class="asset-meta">
                    {{ spec.duration_sec }}s · 种子 {{ spec.seed }}<span v-if="spec.engine && spec.engine !== '-'"> · {{ spec.engine }}</span>
                </div>
                <div class="asset-meta">{{ spec.description || '（无描述）' }}</div>
                <div v-if="spec.category" class="asset-meta asset-category">分类：{{ spec.category }}</div>
                <div v-if="(spec.tags || []).length" class="flex gap-2" style="flex-wrap: wrap;">
                    <span v-for="tag in spec.tags" :key="tag" class="tag-chip">{{ tag }}</span>
                </div>
                <div class="asset-prompt" :title="spec.prompt">{{ spec.prompt }}</div>
                <!-- 只要磁盘上有 wav 就能听（含规格已变更、旧音频还在的情况）；key 里带
                     audioNonce：一次生成完成后强制重建元素，不让它继续持有旧音频的缓冲 -->
                <audio v-if="spec.status !== 'MISSING'" :key="spec.kind + '/' + spec.name + '/' + audioNonce"
                       class="asset-audio" controls preload="none"
                       :src="API.getAssetAudioUrl(spec.kind, spec.name)" @play="onAudioPlay"></audio>
                <div v-else class="asset-meta">尚未生成，生成后可在这里试听</div>
                <div v-if="spec.status === 'STALE(引擎已变更)'" class="asset-meta asset-engine-drift">
                    这条是 {{ spec.engine }} 生成的，当前配置的引擎是 {{ spec.expected_engine }}——旧音频仍可听，点「重新生成」才会用新引擎重做
                </div>
                <div v-else-if="spec.status && spec.status.startsWith('STALE')" class="asset-meta">
                    播放的是规格变更前生成的旧音频，重新生成后才是新的
                </div>
                <div class="card-footer">
                    <button class="action-btn" @click="generate(spec)" :disabled="!!genTask">
                        {{ spec.status === 'MISSING' ? '生成' : '重新生成' }}
                    </button>
                    <button class="action-btn" @click="openEdit(spec)">编辑</button>
                    <button class="action-btn danger" @click="openDelete(spec)">删除</button>
                </div>
            </div>
        </div>
        </div>
        </div>

        <div v-if="showDialog" class="modal-overlay" @click.self="showDialog = false">
            <div class="modal">
                <div class="modal-header">
                    <h2 class="modal-title">{{ editing ? '编辑素材' : '新增素材' }}</h2>
                    <button class="modal-close" @click="showDialog = false">&times;</button>
                </div>
                <div class="modal-body">
                    <div class="form-row">
                        <div class="form-group">
                            <label class="form-label">类型</label>
                            <select class="form-select" v-model="form.kind" :disabled="editing">
                                <option value="ambience">背景音</option>
                                <option value="sfx">音效</option>
                            </select>
                        </div>
                        <div class="form-group">
                            <label class="form-label">名称</label>
                            <input type="text" class="form-input asset-name-input" v-model="form.name" :disabled="editing"
                                   placeholder="如 rain_heavy">
                        </div>
                    </div>
                    <p class="form-help" v-if="!editing">名称只能用小写字母、数字、下划线；创建后不能改名（剧本和时间线按名字引用素材）</p>
                    <div class="form-group">
                        <label class="form-label">描述（中文，会进入解析提示词的素材词表）</label>
                        <input type="text" class="form-input" v-model="form.description">
                    </div>
                    <div class="form-group">
                        <label class="form-label">生成提示词 prompt</label>
                        <textarea class="form-textarea asset-prompt-input" v-model="form.prompt" rows="3"></textarea>
                    </div>
                    <div class="form-group">
                        <label class="form-label">反向提示词</label>
                        <input type="text" class="form-input" v-model="form.negative_prompt">
                    </div>
                    <div class="form-row">
                        <div class="form-group">
                            <label class="form-label">时长（秒，0–60）</label>
                            <input type="number" class="form-input" v-model.number="form.duration_sec" step="0.5" min="0.5" max="60">
                        </div>
                        <div class="form-group">
                            <label class="form-label">随机种子</label>
                            <input type="number" class="form-input" v-model.number="form.seed" min="0">
                        </div>
                    </div>
                    <div class="form-group">
                        <label class="form-label">分类</label>
                        <select class="form-select asset-category-select" v-model="form.category">
                            <option value="">未分类</option>
                            <option v-for="opt in categoryOptions" :key="opt.value" :value="opt.value">{{ opt.label }}</option>
                        </select>
                        <p class="form-help">没有想要的分类？先在左侧分类树里新建</p>
                    </div>
                    <div class="form-group">
                        <label class="form-label">标签</label>
                        <div class="flex gap-2 mb-2" style="flex-wrap: wrap;">
                            <span v-for="tag in form.tags" :key="tag" class="tag-chip">
                                {{ tag }}<button class="tag-chip-remove" @click="removeTag(tag)">&times;</button>
                            </span>
                        </div>
                        <input type="text" class="form-input asset-tag-input" v-model="tagInput"
                               @keydown.enter.prevent="addTag" placeholder="输入标签后按回车">
                    </div>
                    <p class="form-help" v-if="editing">改 prompt / 反向提示词 / 时长 / 种子会让已生成的音频失效（需要重新生成）；只改描述不会。</p>
                </div>
                <div class="modal-footer">
                    <button class="btn btn-secondary" @click="showDialog = false">取消</button>
                    <button class="btn btn-primary" @click="save" :disabled="!canSave">{{ editing ? '保存' : '创建' }}</button>
                </div>
            </div>
        </div>

        <div v-if="deleting" class="modal-overlay" @click.self="deleting = null">
            <div class="modal">
                <div class="modal-header"><h2 class="modal-title">删除素材</h2></div>
                <div class="modal-body">
                    <div class="confirm-message">确定删除素材「{{ deleting.name }}」的定义吗？</div>
                    <label v-if="deleting.status !== 'MISSING'" class="form-checkbox">
                        <input type="checkbox" v-model="deleteFiles">
                        <span>同时删除已生成的音频文件</span>
                    </label>
                    <div class="confirm-warning">
                        引用了「{{ deleting.name }}」的章节不会报错，混音时会跳过这条素材（并在章节素材状态里显示为缺失）。
                    </div>
                </div>
                <div class="confirm-footer">
                    <button class="btn btn-secondary" @click="deleting = null">取消</button>
                    <button class="btn btn-danger" @click="confirmDelete">删除</button>
                </div>
            </div>
        </div>
        </div>
    `,
    setup() {
        const showToast = inject('showToast');
        const showConfirm = inject('showConfirm');

        const specs = ref([]);
        const loading = ref(true);
        const tagStats = ref([]);
        const tagFilters = ref([]);
        const kindFilter = ref('all');
        const kindFilters = [
            { value: 'all', label: '全部' },
            { value: 'ambience', label: '背景音' },
            { value: 'sfx', label: '音效' },
        ];
        const kindLabel = (kind) => (kind === 'ambience' ? '背景音' : '音效');

        // ---- 分类树（逻辑在 category-tree.js，界面是 category-tree-pane）----
        const {
            tree, collapsed, selectedPath, flatRows, pathOptions, matchesPath,
            toggleCollapse, reload: loadTree, saveNode, removeNode,
        } = CategoryTree.useCategoryTree({
            load: () => API.getAssetCategoryTree(),
            save: (t) => API.putAssetCategoryTree(t),
            showToast,
            showConfirm,
            deleteWarning: '已经使用这些分类的素材不会被清空——它们保留原来的分类名，只是不再出现在分类树里（编辑时显示为「未登记」）。',
        });
        const categoryOptions = computed(() => pathOptions(specs.value.map((s) => s.category)));

        const visibleSpecs = computed(() => specs.value.filter((s) => {
            if (kindFilter.value !== 'all' && s.kind !== kindFilter.value) return false;
            if (!matchesPath(s.category)) return false;
            if (tagFilters.value.length && !tagFilters.value.some((t) => (s.tags || []).includes(t))) return false;
            return true;
        }));

        const toggleTagFilter = (name) => {
            const i = tagFilters.value.indexOf(name);
            if (i >= 0) tagFilters.value.splice(i, 1); else tagFilters.value.push(name);
        };

        // 状态 -> 展示；STALE(...) 是带括号后缀的前缀匹配
        const statusInfo = (status) => {
            if (status === 'OK') return { label: '已生成', cls: 'badge-success' };
            if (status === 'OK(占位/Mock)') return { label: '占位音', cls: 'badge-warning' };
            if (status === 'STALE(引擎已变更)') return { label: '引擎已变更', cls: 'badge-warning' };
            if (status && status.startsWith('STALE')) return { label: '规格已变更', cls: 'badge-warning' };
            return { label: '未生成', cls: 'badge-neutral' };
        };

        const loadTags = async () => {
            try {
                tagStats.value = (await API.getAssetTags()).tags || [];
                // 筛选中的标签如果已经没有素材在用，去掉，免得筛出一片空白
                tagFilters.value = tagFilters.value.filter((t) => tagStats.value.some((s) => s.name === t));
            } catch (error) {
                console.error('加载标签失败:', error);
            }
        };

        const loadSpecs = async () => {
            try {
                specs.value = (await API.getAssetSpecs()).specs || [];
                loadTags();
            } catch (error) {
                showToast?.(`加载素材失败: ${error.message}`, 'error');
            } finally {
                loading.value = false;
            }
        };

        // ---- 新增 / 编辑 ----
        const showDialog = ref(false);
        const editing = ref(false);
        const blankForm = () => ({
            kind: 'sfx', name: '', description: '', prompt: '',
            negative_prompt: '', duration_sec: 5, seed: 0, category: '', tags: [],
        });
        const form = reactive(blankForm());
        const tagInput = ref('');
        const addTag = () => {
            const t = tagInput.value.trim();
            if (t && !form.tags.includes(t)) form.tags.push(t);
            tagInput.value = '';
        };
        const removeTag = (tag) => { form.tags = form.tags.filter((t) => t !== tag); };

        const canSave = computed(() => form.name.trim() && form.prompt.trim());

        const openCreate = () => {
            editing.value = false;
            Object.assign(form, blankForm());
            tagInput.value = '';
            // 在某个分类下点「新增」，默认就归到这个分类
            form.category = selectedPath.value || '';
            showDialog.value = true;
        };
        const openEdit = (spec) => {
            editing.value = true;
            Object.assign(form, {
                kind: spec.kind, name: spec.name, description: spec.description || '',
                prompt: spec.prompt, negative_prompt: spec.negative_prompt || '',
                duration_sec: spec.duration_sec, seed: spec.seed,
                category: spec.category || '', tags: [...(spec.tags || [])],
            });
            tagInput.value = '';
            showDialog.value = true;
        };
        const save = async () => {
            addTag();  // 输入框里还没按回车的标签也算
            const body = {
                description: form.description, prompt: form.prompt,
                negative_prompt: form.negative_prompt,
                duration_sec: Number(form.duration_sec), seed: Number(form.seed),
                category: form.category, tags: form.tags,  // "" / [] = 清空
            };
            try {
                if (editing.value) {
                    await API.updateAssetSpec(form.kind, form.name, body);
                } else {
                    await API.createAssetSpec({ kind: form.kind, name: form.name.trim(), ...body });
                }
                showDialog.value = false;
                await loadSpecs();
            } catch (error) {
                showToast?.(`保存失败: ${error.message}`, 'error');
            }
        };

        // ---- 删除 ----
        const deleting = ref(null);
        const deleteFiles = ref(false);
        const openDelete = (spec) => {
            deleting.value = spec;
            deleteFiles.value = false;
        };
        const confirmDelete = async () => {
            const spec = deleting.value;
            try {
                await API.deleteAssetSpec(spec.kind, spec.name, deleteFiles.value);
                deleting.value = null;
                await loadSpecs();
            } catch (error) {
                showToast?.(`删除失败: ${error.message}`, 'error');
            }
        };

        // ---- 生成任务：先预检、确认，再提交；进度走 SSE 的 task-update ----
        // 同时只播一个：开始播放某个素材时暂停其余的
        const onAudioPlay = (event) => {
            document.querySelectorAll('.asset-card audio').forEach((el) => {
                if (el !== event.target) el.pause();
            });
        };
        const audioNonce = ref(0);

        const genTask = ref(null);
        // 同 roles-page 预计算任务的竞态：终态事件可能先于 createTask 的响应到达，
        // 响应里的「排队中」旧快照不能再把已清掉的 genTask 写回去
        const finishedGenIds = new Set();
        const genPercent = computed(() => {
            const p = genTask.value?.progress;
            return p && p.total ? Math.round((p.done / p.total) * 100) : 0;
        });

        const generate = async (spec) => {
            // 单条「重新生成」要强制：已生成的（含占位音、引擎已变更）不强制会被当缓存跳过。
            // 只有「规格已变更」和「未生成」不需要——哈希对不上本来就会重做
            const needsForce = (st) => st === 'OK' || st === 'OK(占位/Mock)' || st === 'STALE(引擎已变更)';
            const params = spec ? { only: [spec.name], force: needsForce(spec.status) } : {};
            try {
                const pre = await API.preflightTask({ type: 'asset_gen', params });
                if (pre.summary.create + pre.summary.overwrite === 0) {
                    showToast?.('没有需要生成的素材（都已是最新）', 'success');
                    return;
                }
                const confirmed = await showConfirm({
                    title: spec ? `生成素材「${spec.name}」` : '生成素材',
                    message: `新生成 ${pre.summary.create} 条，${pre.summary.overwrite} 条将被重新生成，` +
                        `${pre.summary.skip} 条无需处理。`,
                    warning: '真实引擎生成时会临时停掉 llama-server 腾显存、完成后自动恢复，期间解析/配音任务会排队等待；' +
                        '引擎环境未就绪时会用占位音代替。',
                    confirmText: '开始生成',
                    confirmClass: 'btn-primary',
                });
                if (!confirmed) return;
                const res = await API.createTask({ type: 'asset_gen', params });
                if (!finishedGenIds.has(res.tasks[0].id)) genTask.value = res.tasks[0];
            } catch (error) {
                showToast?.(`提交失败: ${error.message}`, 'error');
            }
        };

        const cancelGen = async () => {
            if (!genTask.value) return;
            try {
                await API.deleteTask(genTask.value.id);
            } catch (error) {
                showToast?.(`取消失败: ${error.message}`, 'error');
            }
        };

        const TERMINAL = ['succeeded', 'failed', 'cancelled'];
        const onTaskUpdate = (event) => {
            const task = event.detail?.task;
            if (!task || task.type !== 'asset_gen') return;
            if (TERMINAL.includes(task.state)) {
                finishedGenIds.add(task.id);
                genTask.value = null;
                audioNonce.value++;
                loadSpecs();
                if (task.state === 'succeeded') showToast?.('素材生成完成', 'success');
                else if (task.state === 'cancelled') showToast?.('素材生成已取消', 'success');
                else showToast?.(`素材生成失败: ${task.error || '未知错误'}`, 'error');
            } else {
                genTask.value = task;
            }
        };

        onMounted(async () => {
            window.addEventListener('task-update', onTaskUpdate);
            await Promise.all([loadSpecs(), loadTree()]);
            // 页面刷新/切走再回来时，接上还在跑的生成任务
            try {
                const tasks = await API.getTasks();
                genTask.value = tasks.find((t) => t.type === 'asset_gen' && !TERMINAL.includes(t.state)) || null;
            } catch (error) {
                console.error('加载任务失败:', error);
            }
        });
        onUnmounted(() => window.removeEventListener('task-update', onTaskUpdate));

        return {
            specs, loading, kindFilter, kindFilters, kindLabel, visibleSpecs, statusInfo,
            tree, collapsed, selectedPath, flatRows, toggleCollapse, saveNode, removeNode,
            categoryOptions, tagStats, tagFilters, toggleTagFilter, tagInput, addTag, removeTag,
            showDialog, editing, form, canSave, openCreate, openEdit, save,
            deleting, deleteFiles, openDelete, confirmDelete,
            genTask, genPercent, generate, cancelGen, onAudioPlay, audioNonce, API,
        };
    },
});

app.component('settings-page', {
    template: `
        <div class="page-pad settings-page">
        <div class="page-header">
            <h1 class="page-title">系统配置</h1>
        </div>
        
        <div v-if="loading" class="empty-state">
            <div class="loading-spinner"></div>
        </div>
        
        <div v-else class="settings-form">
            <div class="settings-section">
                <h3 class="settings-section-title">TTS 设置</h3>
                <div class="settings-section-divider"></div>
                <div class="form-group">
                    <label class="form-label">TTS 引擎选择</label>
                    <select class="form-select" v-model="form.tts_engine">
                        <option value="indextts">IndexTTS</option>
                        <option value="edge-tts">Edge TTS</option>
                    </select>
                    <p class="form-help">语速/音调是每个角色单独的参数（角色库页面设置），不是全局配置</p>
                </div>
                <div class="form-group">
                    <label class="form-label">句间静音（毫秒）</label>
                    <input type="number" class="form-input setting-segment-gap" v-model.number="form.segment_gap_ms" min="0" max="2000" step="50">
                    <p class="form-help">相邻两句之间插入的静音，默认 200。它在配音时被写进时间线，改动后需要重新执行「批量生成人声」才生效（已合成的语音会命中缓存，只重算时间线，很快）。</p>
                </div>
            </div>

            <div class="settings-section">
                <h3 class="settings-section-title">文件设置</h3>
                <div class="settings-section-divider"></div>
                <div class="form-group">
                    <label class="form-label">文件输出根目录</label>
                    <input type="text" class="form-input" v-model="form.library_root" placeholder="/path/to/library">
                    <p class="form-help">小说数据和生成的音频文件存储位置</p>
                </div>
            </div>
            
            <div class="settings-section">
                <h3 class="settings-section-title">性能设置</h3>
                <div class="settings-section-divider"></div>
                <div class="form-group">
                    <label class="form-label">后台并发任务数</label>
                    <input type="number" class="form-input" v-model="form.cpu_workers" min="1" max="8">
                    <p class="form-help">仅影响混音等 CPU 任务；解析和语音合成受显存限制，恒为串行</p>
                </div>
            </div>
            
            <div class="settings-section">
                <h3 class="settings-section-title">音频设置</h3>
                <div class="settings-section-divider"></div>
                <div class="form-group">
                    <label class="form-checkbox">
                        <input type="checkbox" class="setting-mix-with-assets" v-model="form.mix_with_assets">
                        <span>混音默认叠加背景音/音效</span>
                    </label>
                    <p class="form-help">关闭时成片只有旁白/角色人声（默认）。批量混音时也可以单次勾选覆盖这个默认值；素材需要先在「音效库」里生成。</p>
                </div>
                <div class="form-row">
                    <div class="form-group">
                        <label class="form-label">音频输出格式</label>
                        <select class="form-select" v-model="form.audio_format">
                            <option value="mp3">MP3</option>
                            <option value="wav">WAV</option>
                            <option value="flac">FLAC</option>
                        </select>
                    </div>
                    <div class="form-group">
                        <label class="form-label">混音码率</label>
                        <select class="form-select" v-model="form.mix_bitrate">
                            <option value="128k">128 kbps</option>
                            <option value="192k">192 kbps</option>
                            <option value="256k">256 kbps</option>
                            <option value="320k">320 kbps</option>
                        </select>
                    </div>
                </div>
            </div>

            <div class="settings-section">
                <h3 class="settings-section-title">混音参数</h3>
                <div class="settings-section-divider"></div>
                <p class="form-help mb-4">只在叠加背景音/音效时生效（纯人声混音不受影响）。电平单位都是 dB，数值越小声音越轻。</p>
                <div class="form-row">
                    <div class="form-group">
                        <label class="form-label">闪避触发阈值（dBFS）</label>
                        <input type="number" class="form-input setting-ducking-threshold" v-model.number="form.ducking_threshold" min="-60" max="0" step="1">
                        <p class="form-help">人声响度超过它时压低背景音，范围 −60 ~ 0，默认 −20</p>
                    </div>
                    <div class="form-group">
                        <label class="form-label">闪避衰减量（dB）</label>
                        <input type="number" class="form-input setting-ducking-gain" v-model.number="form.ducking_gain_db" min="-40" max="0" step="0.5">
                        <p class="form-help">闪避时背景音降低多少，填负数或 0（−10.5 ≈ 降到 30% 音量）</p>
                    </div>
                </div>
                <div class="form-row">
                    <div class="form-group">
                        <label class="form-label">闪避渐变时长（毫秒）</label>
                        <input type="number" class="form-input setting-ducking-fade" v-model.number="form.ducking_fade_ms" min="0" max="5000" step="50">
                    </div>
                    <div class="form-group">
                        <label class="form-label">背景音基础电平（dBFS）</label>
                        <input type="number" class="form-input setting-ambience-gain" v-model.number="form.ambience_gain_db" min="-60" max="0" step="1">
                        <p class="form-help">整条背景音轨的目标电平，默认 −18</p>
                    </div>
                </div>
                <div class="form-group">
                    <label class="form-label">音效峰值限幅（dBFS）</label>
                    <input type="number" class="form-input setting-sfx-limit" v-model.number="form.sfx_limit_dbfs" min="-60" max="0" step="0.5">
                    <p class="form-help">音效超过它就被压到它，防止叠加人声后爆音，默认 −3</p>
                </div>
            </div>
            
            <div class="settings-section">
                <h3 class="settings-section-title">监控设置</h3>
                <div class="settings-section-divider"></div>
                <div class="form-group">
                    <label class="form-label">资源监控刷新间隔（秒）</label>
                    <input type="number" class="form-input" v-model="form.monitor_interval" min="1" max="60">
                </div>
            </div>
            
            <div class="settings-actions">
                <button class="btn btn-secondary" @click="resetForm">重置</button>
                <button class="btn btn-primary" @click="saveConfig">保存配置</button>
            </div>
        </div>
        </div>
    `,
    setup() {
        const showToast = inject('showToast');
        const loading = ref(true);
        const form = reactive({
            tts_engine: 'indextts',
            segment_gap_ms: 200,
            library_root: '',
            cpu_workers: 2,
            audio_format: 'mp3',
            mix_bitrate: '192k',
            mix_with_assets: false,
            ducking_threshold: -20,
            ducking_gain_db: -10.46,
            ducking_fade_ms: 300,
            ambience_gain_db: -18,
            sfx_limit_dbfs: -3,
            monitor_interval: 1,
        });

        const originalForm = reactive({});

        // 表单字段 -> 后端点分路径键（PATCH /config 只认白名单里的点分键）+ 转换。
        // number: true 的字段清空后 v-model.number 会给出 ''，Number('') 是 0，
        // 会把「没填」悄悄存成 0——所以数值字段保存前先挡住空值。
        const FIELDS = [
            { field: 'tts_engine', key: 'tts.engine', label: 'TTS 引擎' },
            { field: 'segment_gap_ms', key: 'tts.segment_gap_ms', label: '句间静音', number: true },
            { field: 'library_root', key: 'server.library_root', label: '文件输出根目录' },
            { field: 'cpu_workers', key: 'server.cpu_workers', label: '后台并发任务数', number: true },
            { field: 'audio_format', key: 'mixing.output_format', label: '音频输出格式' },
            { field: 'mix_bitrate', key: 'mixing.bitrate', label: '混音码率' },
            // 配置里存的是 voice_only（默认 true），界面上反过来问「是否叠加素材」
            { field: 'mix_with_assets', key: 'mixing.voice_only', label: '混音叠加素材', toApi: (v) => !v },
            { field: 'ducking_threshold', key: 'mixing.ducking_threshold', label: '闪避触发阈值', number: true },
            { field: 'ducking_gain_db', key: 'mixing.ducking_gain_db', label: '闪避衰减量', number: true },
            { field: 'ducking_fade_ms', key: 'mixing.ducking_fade_ms', label: '闪避渐变时长', number: true },
            { field: 'ambience_gain_db', key: 'mixing.ambience_gain_db', label: '背景音基础电平', number: true },
            { field: 'sfx_limit_dbfs', key: 'mixing.sfx_limit_dbfs', label: '音效峰值限幅', number: true },
            // monitor_interval 界面是"秒"，后端存的是毫秒
            { field: 'monitor_interval', key: 'server.monitor_interval_ms', label: '监控刷新间隔', number: true,
              toApi: (v) => Math.round(Number(v) * 1000) },
        ];

        // 后端 GET /config 返回的是 global_config.yaml 原始的嵌套结构
        // {server:{...}, tts:{...}, mixing:{...}}，不能直接 Object.assign
        // 到这个扁平 form 上——那样嵌套对象只会作为多余字段挂上去，
        // 扁平字段永远还是初始值。数值字段必须用 ?? 而不是 ||：0 dB / 0 ms 是合法值，
        // 但在 || 里是假值，会被悄悄换回默认。
        const fillFormFromConfig = (config) => {
            const mixing = config.mixing || {};
            form.tts_engine = config.tts?.engine || form.tts_engine;
            form.segment_gap_ms = config.tts?.segment_gap_ms ?? 200;
            form.library_root = config.server?.library_root ?? form.library_root;
            form.cpu_workers = config.server?.cpu_workers ?? form.cpu_workers;
            form.audio_format = mixing.output_format || form.audio_format;
            form.mix_bitrate = mixing.bitrate || form.mix_bitrate;
            form.mix_with_assets = mixing.voice_only === false;
            form.ducking_threshold = mixing.ducking_threshold ?? -20;
            // 没设过 ducking_gain_db 时，用旧的线性比例 ducking_volume_ratio 换算出当前
            // 实际生效的衰减量来显示（0.3 → −10.46 dB），这样界面上看到的就是真实值
            const ratio = mixing.ducking_volume_ratio ?? 0.3;
            form.ducking_gain_db = mixing.ducking_gain_db
                ?? (ratio > 0 ? Math.round(20 * Math.log10(ratio) * 100) / 100 : -10);
            form.ducking_fade_ms = mixing.ducking_fade_ms ?? 300;
            form.ambience_gain_db = mixing.ambience_gain_db ?? -18;
            form.sfx_limit_dbfs = mixing.sfx_limit_dbfs ?? -3;
            const intervalMs = config.server?.monitor_interval_ms;
            form.monitor_interval = intervalMs ? intervalMs / 1000 : form.monitor_interval;
        };

        const loadConfig = async () => {
            loading.value = true;
            try {
                const config = await API.getConfig();
                fillFormFromConfig(config);
                Object.assign(originalForm, form);
            } catch (error) {
                console.error('加载配置失败:', error);
                showToast?.(`加载配置失败: ${error.message}`, 'error');
            } finally {
                loading.value = false;
            }
        };

        const resetForm = () => {
            Object.assign(form, originalForm);
        };

        const saveConfig = async () => {
            // 只提交改动过的字段：不然每次保存都会把「界面上显示的默认值」实体化写进
            // 配置文件（比如从旧比例换算出来的 ducking_gain_db），悄悄改变配置来源
            const payload = {};
            const changed = [];
            for (const f of FIELDS) {
                if (form[f.field] === originalForm[f.field]) continue;
                if (f.number && (form[f.field] === '' || form[f.field] === null || Number.isNaN(Number(form[f.field])))) {
                    showToast?.(`「${f.label}」不能为空，请填写数字`, 'error');
                    return;
                }
                payload[f.key] = f.toApi ? f.toApi(form[f.field]) : (f.number ? Number(form[f.field]) : form[f.field]);
                changed.push(f);
            }
            if (changed.length === 0) {
                showToast?.('没有需要保存的改动', 'success');
                return;
            }
            try {
                const result = await API.updateConfig(payload);
                // 只把真的写进去的字段记为「已保存」，被拒的仍然算未保存
                const applied = new Set(result.applied_keys || []);
                for (const f of changed) {
                    if (applied.has(f.key)) originalForm[f.field] = form[f.field];
                }
                const rejected = result.rejected || (result.rejected_keys || []).map((key) => ({ key, reason: '被拒绝' }));
                if (rejected.length > 0) {
                    const labelOf = (key) => (FIELDS.find((f) => f.key === key) || {}).label || key;
                    showToast?.('以下配置未能保存：' + rejected.map((r) => `${labelOf(r.key)}（${r.reason}）`).join('；'), 'error');
                } else {
                    showToast?.('配置已保存', 'success');
                }
            } catch (error) {
                console.error('保存配置失败:', error);
                showToast?.('保存配置失败: ' + error.message, 'error');
            }
        };

        onMounted(() => {
            loadConfig();
        });

        return {
            loading,
            form,
            resetForm,
            saveConfig,
        };
    },
});

app.component('toast', {
    template: `
        <div class="toast-container">
            <div v-for="toast in toasts" :key="toast.id" class="toast" :class="toast.type">
                {{ toast.message }}
            </div>
        </div>
    `,
    setup() {
        const toasts = ref([]);

        const show = (message, type = 'success') => {
            const id = Date.now();
            toasts.value.push({ id, message, type });
            setTimeout(() => {
                toasts.value = toasts.value.filter(t => t.id !== id);
            }, 3000);
        };

        return {
            toasts,
            show,
        };
    },
});

// 取代原生 prompt()。两种形态：文本输入（默认）和下拉选择（传 options）。
// show() 返回 Promise<string|null>：确定 → 值，取消/点遮罩 → null。
// 确定按钮的点击处理里同步 resolve——这样调用方在 await 之后接着做的事（比如触发隐藏
// 文件输入的 click()）仍然处在这次用户点击的「用户手势」窗口内，不会被浏览器拦掉。
app.component('prompt-dialog', {
    template: `
        <div v-if="visible" class="modal-overlay" @click.self="cancel">
            <div class="modal">
                <div class="modal-header">
                    <h2 class="modal-title">{{ title }}</h2>
                </div>
                <div class="modal-body">
                    <div v-if="message" class="confirm-message">{{ message }}</div>
                    <div class="form-group">
                        <label class="form-label">{{ label }}</label>
                        <select v-if="options.length" class="form-select prompt-select" v-model="value">
                            <option v-for="opt in options" :key="opt.value" :value="opt.value">{{ opt.label }}</option>
                        </select>
                        <input v-else ref="inputEl" type="text" class="form-input prompt-input" v-model="value"
                               :placeholder="placeholder" @keyup.enter="confirm" @keyup.esc="cancel">
                    </div>
                    <div v-if="error" class="confirm-warning prompt-error">{{ error }}</div>
                </div>
                <div class="confirm-footer">
                    <button class="btn btn-secondary" @click="cancel">取消</button>
                    <button class="btn btn-primary prompt-confirm" @click="confirm" :disabled="!canConfirm">{{ confirmText }}</button>
                </div>
            </div>
        </div>
    `,
    setup() {
        const visible = ref(false);
        const title = ref('');
        const message = ref('');
        const label = ref('');
        const value = ref('');
        const placeholder = ref('');
        const options = ref([]);
        const confirmText = ref('确定');
        const error = ref('');
        const inputEl = ref(null);
        let validateFn = null;
        let resolvePromise = null;

        const canConfirm = computed(() => options.value.length > 0 || String(value.value).trim().length > 0);

        const show = (opts = {}) => {
            title.value = opts.title || '请输入';
            message.value = opts.message || '';
            label.value = opts.label || '';
            value.value = opts.value ?? (opts.options && opts.options.length ? opts.options[0].value : '');
            placeholder.value = opts.placeholder || '';
            options.value = opts.options || [];
            confirmText.value = opts.confirmText || '确定';
            error.value = '';
            validateFn = opts.validate || null;
            visible.value = true;
            nextTick(() => inputEl.value && inputEl.value.focus());
            return new Promise((resolve) => { resolvePromise = resolve; });
        };

        const finish = (result) => {
            visible.value = false;
            if (resolvePromise) {
                const r = resolvePromise;
                resolvePromise = null;
                r(result);
            }
        };

        const confirm = () => {
            if (!canConfirm.value) return;
            const result = options.value.length ? value.value : String(value.value).trim();
            const problem = validateFn ? validateFn(result) : null;
            if (problem) {
                error.value = problem;
                return;
            }
            finish(result);
        };
        const cancel = () => finish(null);

        return { visible, title, message, label, value, placeholder, options, confirmText, error, inputEl, canConfirm, show, confirm, cancel };
    },
});

app.component('confirm-dialog', {
    template: `
        <div v-if="visible" class="modal-overlay" @click.self="cancel">
            <div class="modal">
                <div class="modal-header">
                    <h2 class="modal-title">{{ title }}</h2>
                </div>
                <div class="modal-body">
                    <div class="confirm-message">{{ message }}</div>
                    <div v-if="warning" class="confirm-warning">
                        <strong>注意：</strong> {{ warning }}
                    </div>
                </div>
                <div class="confirm-footer">
                    <button class="btn btn-secondary" @click="cancel">取消</button>
                    <button class="btn" :class="confirmClass" @click="confirm">{{ confirmText }}</button>
                </div>
            </div>
        </div>
    `,
    setup() {
        const visible = ref(false);
        const title = ref('');
        const message = ref('');
        const warning = ref('');
        const confirmText = ref('确认');
        const confirmClass = ref('btn-primary');
        let resolvePromise = null;

        const show = (options) => {
            title.value = options.title || '确认';
            message.value = options.message || '';
            warning.value = options.warning || '';
            confirmText.value = options.confirmText || '确认';
            confirmClass.value = options.confirmClass || 'btn-primary';
            visible.value = true;

            return new Promise((resolve) => {
                resolvePromise = resolve;
            });
        };

        const confirm = () => {
            visible.value = false;
            if (resolvePromise) {
                resolvePromise(true);
            }
        };

        const cancel = () => {
            visible.value = false;
            if (resolvePromise) {
                resolvePromise(false);
            }
        };

        return {
            visible,
            title,
            message,
            warning,
            confirmText,
            confirmClass,
            show,
            confirm,
            cancel,
        };
    },
});

app.mount('#app');