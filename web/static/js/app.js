const { createApp, ref, reactive, computed, watch, onMounted, onUnmounted, nextTick, provide, inject } = Vue;

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

        let eventSource = null;
        let reconnectTimer = null;
        let errorCount = 0;
        const MAX_ERRORS = 3;

        const gpuOwnerClass = computed(() => gpuOwner.value);
        const gpuOwnerIcon = computed(() => {
            switch (gpuOwner.value) {
                case 'llm': return '🟢';
                case 'tts': return '🔵';
                default: return '⚪';
            }
        });
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

        // 通过 provide/inject 暴露给子组件，不要用 window.app.__vue_app__.
        // _instance.proxy 这种够 Vue 内部私有属性的写法——顶层 const app 在经典
        // <script> 里不会自动挂到 window 上，之前那样写点了就直接抛异常。
        provide('showConfirm', showConfirm);
        provide('showToast', showToast);

        onMounted(() => {
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
            gpuOwnerIcon,
            gpuOwnerText,
            sseDisconnected,
            toast,
            confirmDialog,
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
        <div class="page-header">
            <h1 class="page-title">小说列表</h1>
            <div class="page-actions">
                <button class="btn btn-primary" @click="showCreateDialog">
                    <span>+</span> 新增小说
                </button>
            </div>
        </div>
        
        <div class="search-box">
            <span class="search-icon">🔍</span>
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
            <div class="empty-state-icon">📚</div>
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
                        <button class="action-btn" @click="editNovel(novel)">✏️</button>
                        <button class="action-btn danger" @click="deleteNovel(novel)">🗑️</button>
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
                <span class="tree-checkbox" @click.stop="$emit('toggle-select', node)">
                    {{ selectedIds.includes(node.id) ? '☑️' : '☐' }}
                </span>
                <span class="status-dot" :class="statusClass(node.status)"></span>
                <span class="tree-node-name">{{ node.title }}</span>
                <div class="tree-node-actions">
                    <button class="action-btn" @click.stop="$emit('rename', node)">✏️</button>
                    <button class="action-btn danger" @click.stop="$emit('delete', node)">🗑️</button>
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

        const statusClass = (status) => {
            if (!status) return 'pending';
            if (status.startsWith('STALE')) return 'stale';
            if (status === 'completed') return 'dubbed';
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
                            🔄 刷新
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
                </div>

                <div class="detail-content">
                    <div v-if="selectedNode && selectedNode.type === 'chapter'">
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
                        <div class="empty-state-icon">📋</div>
                        <h3 class="empty-state-title">选择左侧节点查看详情</h3>
                    </div>
                </div>
            </div>
        </div>

        <input type="file" ref="chapterFileInput" accept=".txt" style="display:none" @change="onChapterFileSelected">
        <input type="file" ref="reimportFileInput" accept=".txt" style="display:none" @change="onReimportFileSelected">

        <div class="task-panel" v-if="tasks.length > 0">
            <div class="task-panel-header" @click="toggleTaskPanel">
                <div class="task-panel-title">
                    <span>任务队列</span>
                    <span class="badge badge-primary">{{ tasks.length }}</span>
                </div>
                <span class="task-panel-toggle" :class="{ expanded: taskPanelExpanded }">▼</span>
            </div>
            <div v-if="taskPanelExpanded" class="task-list">
                <div v-for="group in taskGroups" :key="group.id" class="task-group">
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
                        <div v-for="task in group.tasks" :key="task.id" class="task-item">
                            <div class="task-item-info">
                                <div class="task-item-title">{{ taskTitle(task) }}</div>
                                <div class="task-item-status">{{ taskStateLabel(task.state) }}</div>
                            </div>
                            <div class="task-item-actions">
                                <button v-if="task.state === 'running' || task.state === 'queued'" class="btn btn-secondary btn-sm" @click="cancelTask(task.id)">
                                    取消
                                </button>
                            </div>
                        </div>
                    </div>
                </div>
            </div>
        </div>
    `,
    setup(props) {
        const showConfirm = inject('showConfirm');
        const showToast = inject('showToast');

        const novel = ref(null);
        const tree = ref([]);
        const loading = ref(true);
        const selectedNode = ref(null);
        const selectedNodes = ref([]);
        const chapterStats = ref({});
        const tasks = ref([]);
        const taskPanelExpanded = ref(true);
        const expandedGroups = ref([]);
        const chapterFileInput = ref(null);
        const reimportFileInput = ref(null);
        const pendingChapterTitle = ref('');
        const treeContainer = ref(null);
        let treeSortable = null;

        // Task 数据结构（src/task_queue.py）没有 title/status 字段，
        // 真实字段是 type/state；这里派生一个人类可读的标题和状态文案
        const TASK_TYPE_LABEL = { parse: '解析', tts: '生成人声', mix: '混音导出', precompute_embedding: '预计算音色' };
        const TASK_STATE_LABEL = {
            queued: '排队中', running: '运行中', succeeded: '已完成',
            failed: '失败', cancelled: '已取消',
        };
        const taskTitle = (task) => `${TASK_TYPE_LABEL[task.type] || task.type} · ${task.chapter_id || ''}`;
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

        const statusClass = (status) => {
            if (!status) return 'pending';
            if (status.startsWith('STALE')) return 'stale';
            if (status === 'completed') return 'dubbed';
            if (status === 'UNKNOWN') return 'pending';
            return 'parsed';
        };

        const taskGroups = computed(() => {
            const groups = {};
            tasks.value.forEach(task => {
                const groupId = task.group_id || task.id;
                if (!groups[groupId]) {
                    groups[groupId] = {
                        id: groupId,
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
        };

        const addPart = async () => {
            const title = prompt('请输入部名称:');
            if (title) {
                try {
                    await API.createNode(props.novelId, { title, type: 'part' });
                    await loadTree();
                } catch (error) {
                    console.error('创建部失败:', error);
                }
            }
        };

        const addVolume = async () => {
            const title = prompt('请输入卷名称:');
            if (title) {
                try {
                    await API.createNode(props.novelId, { title, type: 'volume' });
                    await loadTree();
                } catch (error) {
                    console.error('创建卷失败:', error);
                }
            }
        };

        const addChapter = async () => {
            const title = prompt('请输入章节名称:');
            if (title) {
                try {
                    await API.createNode(props.novelId, { title, type: 'chapter' });
                    await loadTree();
                } catch (error) {
                    console.error('创建章节失败:', error);
                }
            }
        };

        const renameNode = async (node) => {
            const title = prompt('请输入新名称:', node.title);
            if (title && title !== node.title) {
                try {
                    await API.updateNode(props.novelId, node.id, { title });
                    await loadTree();
                } catch (error) {
                    console.error('重命名失败:', error);
                }
            }
        };

        const deleteNode = async (node) => {
            const confirmed = confirm(`确定要删除「${node.title}」吗？`);
            if (confirmed) {
                try {
                    await API.deleteNode(props.novelId, node.id, true);
                    await loadTree();
                    if (selectedNode.value?.id === node.id) {
                        selectedNode.value = null;
                    }
                } catch (error) {
                    console.error('删除失败:', error);
                }
            }
        };

        const addChildNode = async () => {
            const type = selectedNode.value.type === 'part' ? 'volume' : 'chapter';
            const title = prompt(`请输入${type === 'volume' ? '卷' : '章'}名称:`);
            if (title) {
                try {
                    await API.createNode(props.novelId, {
                        title,
                        type,
                        parent_id: selectedNode.value.id,
                    });
                    await loadTree();
                } catch (error) {
                    console.error('创建节点失败:', error);
                }
            }
        };

        // 章节上传：选中一个部/卷作为父节点时用——先建一个空的 chapter 节点，
        // 再把选中文件的内容 PUT 上去；新节点还没有 raw.txt，upload_raw 会直接
        // 写入，不会触发"重新导入"的覆盖确认
        const uploadChapter = () => {
            if (!selectedNode.value) return;
            pendingChapterTitle.value = prompt('请输入章节名称:');
            if (pendingChapterTitle.value) {
                chapterFileInput.value?.click();
            }
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
            try {
                const pre = await API.preflightTask({ type, novel_id: props.novelId, scope });
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
                await API.createTask({ type, novel_id: props.novelId, scope });
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

        const cancelGroup = async (groupId) => {
            const group = taskGroups.value.find(g => g.id === groupId);
            if (group) {
                const confirmed = confirm(`确定要取消该批次的 ${group.tasks.length} 个任务吗？`);
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
            const confirmed = confirm('确定要取消该任务吗？');
            if (confirmed) {
                try {
                    await API.deleteTask(taskId);
                    await loadTasks();
                } catch (error) {
                    console.error('取消任务失败:', error);
                }
            }
        };

        // task_update SSE 事件在 app.js 里转发成 window 上的 CustomEvent，
        // 收到就整体重新拉一次任务列表（任务量不大，简单可靠优先于精细 patch）
        const onTaskUpdate = () => { loadTasks(); };

        onMounted(async () => {
            await Promise.all([loadNovel(), loadTree(), loadTasks()]);
            window.addEventListener('task-update', onTaskUpdate);
        });

        onUnmounted(() => {
            window.removeEventListener('task-update', onTaskUpdate);
        });

        return {
            novel,
            tree,
            loading,
            selectedNode,
            selectedNodes,
            chapterStats,
            tasks,
            taskGroups,
            taskPanelExpanded,
            expandedGroups,
            chapterFileInput,
            reimportFileInput,
            treeContainer,
            onReorder,
            statusClass,
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
                        <div class="empty-state-icon">📝</div>
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
                        <div class="empty-state-icon">✏️</div>
                        <h3 class="empty-state-title">选择左侧分块进行编辑</h3>
                    </div>
                </div>
                <div v-if="selectedSegment" class="segment-edit-actions">
                    <button class="btn btn-secondary" @click="cancelEdit">取消</button>
                    <button class="btn btn-primary" @click="saveSegment">保存</button>
                </div>
            </div>
        </div>
    `,
    setup(props) {
        const showConfirm = inject('showConfirm');
        const showToast = inject('showToast');

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
        });

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
            const name = prompt('请输入角色名称:');
            if (name) {
                try {
                    const { role_id } = await API.createRole({ name });
                    editForm.speaker = role_id;
                    await loadRoles();
                } catch (error) {
                    console.error('创建角色失败:', error);
                }
            }
            showRoleDropdown.value = false;
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
            const confirmed = confirm(
                '会对整章重新跑一次增量 TTS（其余已合成且未改动的分块会命中缓存，不会重新生成）。确定继续吗？'
            );
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
                await API.updateSegment(props.novelId, props.chapterId, selectedSegment.value.seg_id, {
                    text: editForm.text,
                    speaker: editForm.speaker,
                    emotion: editForm.emotion,
                });
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
            const roleName = prompt('请输入要绑定的角色名称:');
            if (!roleName) return;
            const role = roles.value.find((r) => r.name === roleName);
            if (!role) {
                showToast?.(`没有找到名为「${roleName}」的角色`, 'error');
                return;
            }
            try {
                await API.batchUpdateSegments(props.novelId, props.chapterId, {
                    seg_ids: selectedSegments.value,
                    set: { speaker: role.id },
                });
                await loadSegments();
                selectedSegments.value = [];
            } catch (error) {
                console.error('批量绑定角色失败:', error);
                showToast?.(`批量绑定失败: ${error.message}`, 'error');
            }
        };

        const batchChangeTone = async () => {
            const emotion = prompt('请输入语气标签 (neutral/happy/angry/sad/serious/afraid/surprised/calm):');
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
            const confirmed = confirm('确定要清空选中分块的角色绑定吗？');
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
                alert(`选中的分块中有 ${unboundCount} 个未绑定角色，请先指派`);
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
            await Promise.all([loadSegments(), loadRoles()]);
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

app.component('roles-page', {
    template: `
        <div class="page-header">
            <h1 class="page-title">全局角色库</h1>
            <div class="page-actions">
                <button class="btn btn-primary" @click="showCreateDialog">
                    <span>+</span> 新增角色
                </button>
            </div>
        </div>
        
        <div class="content-section">
            <div class="flex gap-2 mb-4">
                <span
                    v-for="category in allCategories"
                    :key="category"
                    class="category-tag"
                    :class="{ active: selectedCategory === category }"
                    @click="filterByCategory(category)"
                >
                    {{ category }}
                </span>
                <span
                    class="category-tag"
                    :class="{ active: !selectedCategory }"
                    @click="filterByCategory(null)"
                >
                    全部
                </span>
                <button class="btn btn-secondary btn-sm" @click="showCategoryDialog = true">管理分类</button>
            </div>
        </div>

        <div v-if="showCategoryDialog" class="modal-overlay" @click.self="showCategoryDialog = false">
            <div class="modal">
                <div class="modal-header">
                    <h2 class="modal-title">管理角色分类</h2>
                    <button class="modal-close" @click="showCategoryDialog = false">&times;</button>
                </div>
                <div class="modal-body">
                    <div class="flex gap-2 mb-2">
                        <input type="text" class="form-input" v-model="newCategoryName" placeholder="新分类名称"
                               @keyup.enter="addCategory">
                        <button class="btn btn-secondary" @click="addCategory">添加</button>
                    </div>
                    <div v-for="category in categories" :key="category" class="flex gap-2 mb-2" style="align-items:center;">
                        <span style="flex:1;">{{ category }}</span>
                        <button class="btn btn-danger btn-sm" @click="removeCategory(category)">删除</button>
                    </div>
                    <p v-if="categories.length === 0" class="form-help">还没有自定义分类</p>
                </div>
                <div class="modal-footer">
                    <button class="btn btn-primary" @click="showCategoryDialog = false">完成</button>
                </div>
            </div>
        </div>
        
        <div v-if="loading" class="empty-state">
            <div class="loading-spinner"></div>
        </div>
        
        <div v-else-if="filteredRoles.length === 0" class="empty-state">
            <div class="empty-state-icon">👤</div>
            <h3 class="empty-state-title">暂无角色</h3>
            <p class="empty-state-description">点击上方按钮创建第一个角色</p>
        </div>
        
        <div v-else>
            <div v-for="role in filteredRoles" :key="role.id" class="role-card">
                <div class="role-card-header">
                    <div>
                        <div class="role-name">{{ role.name }}</div>
                        <div class="role-meta">{{ role.category || '未分类' }} · {{ role.gender || '未知' }}</div>
                    </div>
                    <div class="flex gap-2">
                        <button class="btn btn-secondary btn-sm" @click="editRole(role)">编辑</button>
                        <button 
                            class="btn btn-danger btn-sm" 
                            @click="deleteRole(role)"
                            :disabled="role.name === 'narrator'"
                        >
                            删除
                        </button>
                    </div>
                </div>
                
                <div v-if="role.has_reference" class="audio-player">
                    <audio :src="referenceUrl(role.id)" controls></audio>
                </div>

                <div class="role-stats">
                    <span class="embedding-status" :class="embeddingClass(role.embedding_status)">
                        {{ embeddingLabel(role.embedding_status) }}
                    </span>
                    <span class="ml-2">
                        被 {{ (role.novels || []).length }} 本小说、{{ role.segment_count || 0 }} 个分块引用
                    </span>
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
                            <select class="form-select" v-model="form.category">
                                <option value="">未分类</option>
                                <option v-for="category in categories" :key="category" :value="category">
                                    {{ category }}
                                </option>
                            </select>
                            <p class="form-help">没有想要的分类？先在上面"管理分类"里加一个</p>
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
    `,
    setup() {
        const showToast = inject('showToast');

        const roles = ref([]);
        const loading = ref(true);
        const selectedCategory = ref(null);
        const showDialog = ref(false);
        const editingRole = ref(null);
        const showCategoryDialog = ref(false);
        const categories = ref([]); // 管理分类里那份"正式登记"的分类列表
        const newCategoryName = ref('');
        const form = reactive({
            name: '',
            category: '',
            gender: '',
            speed: 1.0,
            notes: '',
            reference_audio: null,
        });

        // 筛选栏用的是"正式分类 + 角色实际在用但没被登记的分类"的并集，
        // 避免管理分类之前创建的角色因为分类没在列表里就消失
        const allCategories = computed(() => {
            const cats = new Set(categories.value);
            roles.value.forEach(role => {
                if (role.category) cats.add(role.category);
            });
            return Array.from(cats);
        });

        const filteredRoles = computed(() => {
            if (!selectedCategory.value) return roles.value;
            return roles.value.filter(role => role.category === selectedCategory.value);
        });

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

        const loadCategories = async () => {
            try {
                const data = await API.getRoleCategories();
                categories.value = data.categories || [];
            } catch (error) {
                console.error('加载分类失败:', error);
            }
        };

        const addCategory = async () => {
            const name = newCategoryName.value.trim();
            if (!name || categories.value.includes(name)) {
                newCategoryName.value = '';
                return;
            }
            try {
                const { categories: updated } = await API.updateRoleCategories({
                    categories: [...categories.value, name],
                });
                categories.value = updated;
                newCategoryName.value = '';
            } catch (error) {
                console.error('添加分类失败:', error);
                showToast?.(`添加失败: ${error.message}`, 'error');
            }
        };

        const removeCategory = async (name) => {
            try {
                const { categories: updated } = await API.updateRoleCategories({
                    categories: categories.value.filter((c) => c !== name),
                });
                categories.value = updated;
                if (selectedCategory.value === name) selectedCategory.value = null;
            } catch (error) {
                console.error('删除分类失败:', error);
                showToast?.(`删除失败: ${error.message}`, 'error');
            }
        };

        const filterByCategory = (category) => {
            selectedCategory.value = category;
        };

        const showCreateDialog = () => {
            editingRole.value = null;
            form.name = '';
            form.category = '';
            form.gender = '';
            form.speed = 1.0;
            form.notes = '';
            form.reference_audio = null;
            showDialog.value = true;
        };

        const editRole = (role) => {
            editingRole.value = role;
            form.name = role.name;
            form.category = role.category || '';
            form.gender = role.gender || '';
            form.speed = role.speed || 1.0;
            form.notes = role.description || '';
            form.reference_audio = null;
            showDialog.value = true;
        };

        const closeDialog = () => {
            showDialog.value = false;
            editingRole.value = null;
        };

        const handleFileUpload = (event) => {
            const file = event.target.files[0];
            if (file) {
                form.reference_audio = file;
            }
        };

        const saveRole = async () => {
            try {
                let roleId;
                if (editingRole.value) {
                    // RoleUpdate 只认 name/category/description/speed，没有 gender/notes 字段
                    await API.updateRole(editingRole.value.id, {
                        name: form.name,
                        category: form.category,
                        speed: form.speed,
                        description: form.notes,
                    });
                    roleId = editingRole.value.id;
                } else {
                    // RoleCreate 只认 name/gender/category/description，没有 speed 字段
                    // （语速是注册之后再通过 update 设的）
                    const { role_id } = await API.createRole({
                        name: form.name,
                        category: form.category,
                        gender: form.gender,
                        description: form.notes,
                    });
                    roleId = role_id;
                    if (form.speed) {
                        await API.updateRole(roleId, { speed: form.speed });
                    }
                }

                if (form.reference_audio) {
                    await API.uploadRoleReference(roleId, form.reference_audio);
                }

                closeDialog();
                await loadRoles();
            } catch (error) {
                console.error('保存角色失败:', error);
            }
        };

        const deleteRole = async (role) => {
            if (role.name === 'narrator') {
                alert('narrator 角色不允许删除');
                return;
            }

            const confirmed = confirm(`确定要删除角色「${role.name}」吗？\n\n该角色被 ${role.segment_count || 0} 个分块引用，删除后这些分块会变成未绑定。`);
            if (confirmed) {
                try {
                    await API.deleteRole(role.id, true);
                    await loadRoles();
                } catch (error) {
                    console.error('删除角色失败:', error);
                }
            }
        };

        onMounted(() => {
            loadRoles();
            loadCategories();
        });

        return {
            roles,
            loading,
            selectedCategory,
            categories,
            allCategories,
            showCategoryDialog,
            newCategoryName,
            filteredRoles,
            showDialog,
            editingRole,
            form,
            referenceUrl,
            embeddingLabel,
            embeddingClass,
            filterByCategory,
            addCategory,
            removeCategory,
            showCreateDialog,
            editRole,
            closeDialog,
            handleFileUpload,
            saveRole,
            deleteRole,
        };
    },
});

app.component('settings-page', {
    template: `
        <div class="page-header">
            <h1 class="page-title">系统配置</h1>
        </div>
        
        <div v-if="loading" class="empty-state">
            <div class="loading-spinner"></div>
        </div>
        
        <div v-else class="settings-form">
            <div class="settings-section">
                <h3 class="settings-section-title">TTS 设置</h3>
                <div class="form-group">
                    <label class="form-label">TTS 引擎选择</label>
                    <select class="form-select" v-model="form.tts_engine">
                        <option value="indextts">IndexTTS</option>
                        <option value="edge-tts">Edge TTS</option>
                    </select>
                    <p class="form-help">语速/音调是每个角色单独的参数（角色库页面设置），不是全局配置</p>
                </div>
            </div>

            <div class="settings-section">
                <h3 class="settings-section-title">文件设置</h3>
                <div class="form-group">
                    <label class="form-label">文件输出根目录</label>
                    <input type="text" class="form-input" v-model="form.library_root" placeholder="/path/to/library">
                    <p class="form-help">小说数据和生成的音频文件存储位置</p>
                </div>
            </div>
            
            <div class="settings-section">
                <h3 class="settings-section-title">性能设置</h3>
                <div class="form-group">
                    <label class="form-label">后台并发任务数</label>
                    <input type="number" class="form-input" v-model="form.cpu_workers" min="1" max="8">
                    <p class="form-help">仅影响混音等 CPU 任务；解析和语音合成受显存限制，恒为串行</p>
                </div>
            </div>
            
            <div class="settings-section">
                <h3 class="settings-section-title">音频设置</h3>
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
                <h3 class="settings-section-title">监控设置</h3>
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
    `,
    setup() {
        const loading = ref(true);
        const form = reactive({
            tts_engine: 'indextts',
            library_root: '',
            cpu_workers: 2,
            audio_format: 'mp3',
            mix_bitrate: '192k',
            monitor_interval: 1,
        });

        const originalForm = reactive({});

        // 后端 GET /config 返回的是 global_config.yaml 原始的嵌套结构
        // {server:{...}, tts:{...}, mixing:{...}}，不能直接 Object.assign
        // 到这个扁平 form 上——那样嵌套对象只会作为多余字段挂上去，
        // 扁平字段永远还是初始值。
        const fillFormFromConfig = (config) => {
            form.tts_engine = config.tts?.engine || form.tts_engine;
            form.library_root = config.server?.library_root ?? form.library_root;
            form.cpu_workers = config.server?.cpu_workers ?? form.cpu_workers;
            form.audio_format = config.mixing?.output_format || form.audio_format;
            form.mix_bitrate = config.mixing?.bitrate || form.mix_bitrate;
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
            } finally {
                loading.value = false;
            }
        };

        const resetForm = () => {
            Object.assign(form, originalForm);
        };

        const saveConfig = async () => {
            try {
                // 后端 PATCH /config 只认白名单里的点分路径键名，扁平字段要转一遍；
                // monitor_interval 这里是"秒"，后端存的是毫秒
                const payload = {
                    'tts.engine': form.tts_engine,
                    'server.library_root': form.library_root,
                    'server.cpu_workers': Number(form.cpu_workers),
                    'mixing.output_format': form.audio_format,
                    'mixing.bitrate': form.mix_bitrate,
                    'server.monitor_interval_ms': Math.round(Number(form.monitor_interval) * 1000),
                };
                const result = await API.updateConfig(payload);
                Object.assign(originalForm, form);
                if (result.rejected_keys && result.rejected_keys.length > 0) {
                    alert(`以下配置未能保存（不在白名单内）：${result.rejected_keys.join('、')}`);
                } else {
                    alert('配置已保存');
                }
            } catch (error) {
                console.error('保存配置失败:', error);
                alert('保存配置失败: ' + error.message);
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