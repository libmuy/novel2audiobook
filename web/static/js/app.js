const { createApp, ref, reactive, computed, watch, onMounted, onUnmounted, nextTick } = Vue;

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

        const formatMemory = (mb) => {
            if (!mb) return '0MB';
            if (mb >= 1024) {
                return `${(mb / 1024).toFixed(1)}G`;
            }
            return `${Math.round(mb)}MB`;
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
                            Object.assign(resource, data.payload);
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
                    Object.assign(resource, monitorData);
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
                :key="novel.id" 
                class="card"
                @click="openNovel(novel.id)"
                style="cursor: pointer;"
            >
                <div class="card-header">
                    <h3 class="card-title">{{ novel.name }}</h3>
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
                        <input type="text" class="form-input" v-model="form.name" placeholder="请输入小说名称">
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
                    <button class="btn btn-primary" @click="saveNovel" :disabled="!form.name.trim()">
                        {{ editingNovel ? '保存' : '创建' }}
                    </button>
                </div>
            </div>
        </div>
    `,
    setup() {
        const novels = ref([]);
        const loading = ref(true);
        const searchQuery = ref('');
        const showDialog = ref(false);
        const editingNovel = ref(null);
        const form = reactive({
            name: '',
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
                novel.name.toLowerCase().includes(query)
            );
        });

        const loadNovels = async () => {
            loading.value = true;
            try {
                novels.value = await API.getNovels();
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
            form.name = '';
            form.description = '';
            form.levels.part = false;
            form.levels.volume = false;
            showDialog.value = true;
        };

        const editNovel = (novel) => {
            editingNovel.value = novel;
            form.name = novel.name;
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
                    await API.updateNovel(editingNovel.value.id, form);
                } else {
                    await API.createNovel(form);
                }
                closeDialog();
                await loadNovels();
            } catch (error) {
                console.error('保存小说失败:', error);
            }
        };

        const deleteNovel = async (novel) => {
            const confirmed = await window.app.__vue_app__._instance.proxy.showConfirm({
                title: '删除小说',
                message: `确定要删除小说「${novel.name}」吗？此操作不可恢复。`,
                warning: `该小说包含 ${(novel.chapter_count || 0)} 个章节，删除后所有章节数据将被移入回收站。`,
                confirmText: '删除',
                confirmClass: 'btn-danger',
            });

            if (confirmed) {
                try {
                    await API.deleteNovel(novel.id, true);
                    await loadNovels();
                } catch (error) {
                    console.error('删除小说失败:', error);
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

app.component('novel-detail-page', {
    props: {
        novelId: String,
    },
    template: `
        <div class="two-pane-layout">
            <div class="tree-pane">
                <div class="tree-header">
                    <h2 class="tree-title">{{ novel?.name || '加载中...' }}</h2>
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
                            <div 
                                v-for="node in tree" 
                                :key="node.id"
                                class="tree-node"
                                :class="{ selected: selectedNode?.id === node.id }"
                                @click="selectNode(node)"
                            >
                                <span class="tree-checkbox" @click.stop="toggleSelect(node)">
                                    {{ selectedNodes.includes(node.id) ? '☑️' : '☐' }}
                                </span>
                                <span class="status-dot" :class="node.status"></span>
                                <span class="tree-node-name">{{ node.name }}</span>
                                <div class="tree-node-actions">
                                    <button class="action-btn" @click.stop="renameNode(node)">✏️</button>
                                    <button class="action-btn danger" @click.stop="deleteNode(node)">🗑️</button>
                                </div>
                            </div>
                        </div>
                    </div>
                </div>
            </div>
            
            <div class="detail-pane">
                <div class="detail-header">
                    <div v-if="selectedNode">
                        <h3 class="detail-title">{{ selectedNode.name }}</h3>
                        <p class="detail-meta">{{ selectedNode.type === 'chapter' ? '章节' : selectedNode.type === 'part' ? '部' : '卷' }}</p>
                    </div>
                    <div v-else>
                        <h3 class="detail-title">选择节点查看详情</h3>
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
                        
                        <div class="content-section">
                            <h4 class="section-title">批量任务</h4>
                            <div class="flex gap-2">
                                <button class="btn btn-secondary" @click="batchParse">批量解析</button>
                                <button class="btn btn-secondary" @click="batchTTS">批量生成人声</button>
                                <button class="btn btn-secondary" @click="batchMix">批量混音导出</button>
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
                                <div class="task-item-title">{{ task.title }}</div>
                                <div class="task-item-status">{{ task.status }}</div>
                            </div>
                            <div class="task-item-actions">
                                <button v-if="task.status === 'running'" class="btn btn-secondary btn-sm" @click="cancelTask(task.id)">
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
        const novel = ref(null);
        const tree = ref([]);
        const loading = ref(true);
        const selectedNode = ref(null);
        const selectedNodes = ref([]);
        const chapterStats = ref({});
        const tasks = ref([]);
        const taskPanelExpanded = ref(true);
        const expandedGroups = ref([]);

        const taskGroups = computed(() => {
            const groups = {};
            tasks.value.forEach(task => {
                const groupId = task.group_id || task.id;
                if (!groups[groupId]) {
                    groups[groupId] = {
                        id: groupId,
                        title: task.group_title || task.title,
                        tasks: [],
                        completed: 0,
                        total: 0,
                    };
                }
                groups[groupId].tasks.push(task);
                groups[groupId].total++;
                if (task.status === 'completed') {
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
                tree.value = await API.getNovelTree(props.novelId);
            } catch (error) {
                console.error('加载树形结构失败:', error);
            } finally {
                loading.value = false;
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
                const segments = await API.getSegments(props.novelId, chapterId);
                const total = segments.length;
                const dubbed = segments.filter(s => s.status === 'synthesized').length;
                const unbound = segments.filter(s => !s.speaker).length;
                
                chapterStats.value = {
                    total_segments: total,
                    dubbed_segments: dubbed,
                    undubbed_segments: total - dubbed,
                    unbound_segments: unbound,
                };
            } catch (error) {
                console.error('加载章节统计失败:', error);
            }
        };

        const addPart = async () => {
            const name = prompt('请输入部名称:');
            if (name) {
                try {
                    await API.createNode(props.novelId, { name, type: 'part' });
                    await loadTree();
                } catch (error) {
                    console.error('创建部失败:', error);
                }
            }
        };

        const addVolume = async () => {
            const name = prompt('请输入卷名称:');
            if (name) {
                try {
                    await API.createNode(props.novelId, { name, type: 'volume' });
                    await loadTree();
                } catch (error) {
                    console.error('创建卷失败:', error);
                }
            }
        };

        const addChapter = async () => {
            const name = prompt('请输入章节名称:');
            if (name) {
                try {
                    await API.createNode(props.novelId, { name, type: 'chapter' });
                    await loadTree();
                } catch (error) {
                    console.error('创建章节失败:', error);
                }
            }
        };

        const renameNode = async (node) => {
            const name = prompt('请输入新名称:', node.name);
            if (name && name !== node.name) {
                try {
                    await API.updateNode(props.novelId, node.id, { name });
                    await loadTree();
                } catch (error) {
                    console.error('重命名失败:', error);
                }
            }
        };

        const deleteNode = async (node) => {
            const confirmed = confirm(`确定要删除「${node.name}」吗？`);
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
            const name = prompt(`请输入${type === 'volume' ? '卷' : '章'}名称:`);
            if (name) {
                try {
                    await API.createNode(props.novelId, { 
                        name, 
                        type,
                        parent_id: selectedNode.value.id,
                    });
                    await loadTree();
                } catch (error) {
                    console.error('创建节点失败:', error);
                }
            }
        };

        const uploadChapter = () => {
            // TODO: 实现章节上传
            alert('章节上传功能待实现');
        };

        const openWorkbench = () => {
            window.location.hash = `#/novels/${props.novelId}/chapters/${selectedNode.value.id}`;
        };

        const reimportChapter = async () => {
            const confirmed = confirm('重新导入会清空本章的剧本、时间线和成品 MP3。确定继续吗？');
            if (confirmed) {
                // TODO: 实现重新导入
                alert('重新导入功能待实现');
            }
        };

        const batchParse = async () => {
            // TODO: 实现批量解析
            alert('批量解析功能待实现');
        };

        const batchTTS = async () => {
            // TODO: 实现批量TTS
            alert('批量TTS功能待实现');
        };

        const batchMix = async () => {
            // TODO: 实现批量混音
            alert('批量混音功能待实现');
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

        const cancelGroup = async (groupId) => {
            const group = taskGroups.value.find(g => g.id === groupId);
            if (group) {
                const confirmed = confirm(`确定要取消该批次的 ${group.tasks.length} 个任务吗？`);
                if (confirmed) {
                    for (const task of group.tasks) {
                        if (task.status === 'running' || task.status === 'pending') {
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

        onMounted(async () => {
            await Promise.all([loadNovel(), loadTree(), loadTasks()]);
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
                <div class="segment-list-content" ref="segmentList">
                    <div v-if="loading" class="empty-state">
                        <div class="loading-spinner"></div>
                    </div>
                    <div v-else-if="segments.length === 0" class="empty-state">
                        <div class="empty-state-icon">📝</div>
                        <h3 class="empty-state-title">暂无分块</h3>
                        <p class="empty-state-description">请先解析该章节</p>
                    </div>
                    <div v-else>
                        <div 
                            v-for="segment in segments" 
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
                        {{ selectedSegment ? `编辑分块 ${selectedSegment.seg_id}` : '选择分块进行编辑' }}
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
                                    {{ editForm.speaker || '选择角色' }}
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
                            <select class="form-select" v-model="editForm.tone">
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
                                    :disabled="!selectedSegment.md5"
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
        const segments = ref([]);
        const loading = ref(true);
        const selectedSegment = ref(null);
        const selectedSegments = ref([]);
        const roles = ref([]);
        const showRoleDropdown = ref(false);
        const audioUrl = ref('');
        const segmentList = ref(null);

        const editForm = reactive({
            text: '',
            speaker: '',
            tone: 'neutral',
        });

        const stats = computed(() => {
            const total = segments.value.length;
            const dubbed = segments.value.filter(s => s.status === 'synthesized').length;
            const unbound = segments.value.filter(s => !s.speaker).length;
            return {
                total,
                dubbed,
                undubbed: total - dubbed,
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
            editForm.tone = segment.tone || 'neutral';
            audioUrl.value = '';
        };

        const selectAllUnbound = () => {
            selectedSegments.value = segments.value
                .filter(s => !s.speaker)
                .map(s => s.seg_id);
        };

        const selectRole = (role) => {
            editForm.speaker = role.name;
            showRoleDropdown.value = false;
        };

        const createAndBindRole = async () => {
            const name = prompt('请输入角色名称:');
            if (name) {
                try {
                    const role = await API.createRole({ name });
                    editForm.speaker = role.name;
                    await loadRoles();
                } catch (error) {
                    console.error('创建角色失败:', error);
                }
            }
            showRoleDropdown.value = false;
        };

        const previewVoice = () => {
            if (selectedSegment.value?.md5) {
                audioUrl.value = API.getAudioUrl(props.novelId, props.chapterId, selectedSegment.value.md5);
            }
        };

        const previewMixed = async () => {
            // TODO: 实现混音预览
            alert('混音预览功能待实现');
        };

        const regenerateVoice = async () => {
            const confirmed = confirm('确定要重新生成该分块的人声吗？');
            if (confirmed) {
                // TODO: 实现重新生成
                alert('重新生成功能待实现');
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
                    tone: editForm.tone,
                });
                await loadSegments();
                selectedSegment.value = null;
            } catch (error) {
                console.error('保存分块失败:', error);
            }
        };

        const openNovelDetail = () => {
            window.location.hash = `#/novels/${props.novelId}`;
        };

        const batchBindRole = async () => {
            const roleName = prompt('请输入要绑定的角色名称:');
            if (roleName) {
                try {
                    await API.batchUpdateSegments(props.novelId, props.chapterId, {
                        segment_ids: selectedSegments.value,
                        updates: { speaker: roleName },
                    });
                    await loadSegments();
                    selectedSegments.value = [];
                } catch (error) {
                    console.error('批量绑定角色失败:', error);
                }
            }
        };

        const batchChangeTone = async () => {
            const tone = prompt('请输入语气标签 (neutral/happy/angry/sad/serious/afraid/surprised/calm):');
            if (tone) {
                try {
                    await API.batchUpdateSegments(props.novelId, props.chapterId, {
                        segment_ids: selectedSegments.value,
                        updates: { tone },
                    });
                    await loadSegments();
                    selectedSegments.value = [];
                } catch (error) {
                    console.error('批量修改语气失败:', error);
                }
            }
        };

        const batchClearRole = async () => {
            const confirmed = confirm('确定要清空选中分块的角色绑定吗？');
            if (confirmed) {
                try {
                    await API.batchUpdateSegments(props.novelId, props.chapterId, {
                        segment_ids: selectedSegments.value,
                        updates: { speaker: null },
                    });
                    await loadSegments();
                    selectedSegments.value = [];
                } catch (error) {
                    console.error('批量清空角色失败:', error);
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

            // TODO: 实现批量生成TTS
            alert('批量生成TTS功能待实现');
        };

        onMounted(async () => {
            await Promise.all([loadSegments(), loadRoles()]);
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
            stats,
            segmentList,
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
                    v-for="category in categories" 
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
                
                <div v-if="role.reference_audio" class="audio-player">
                    <audio :src="role.reference_audio" controls></audio>
                </div>
                
                <div class="role-stats">
                    <span class="embedding-status" :class="role.embedding_valid ? 'valid' : 'invalid'">
                        {{ role.embedding_valid ? '✓ Embedding 有效' : '✗ Embedding 无效' }}
                    </span>
                    <span class="ml-2">
                        被 {{ role.novel_count || 0 }} 本小说、{{ role.segment_count || 0 }} 个分块引用
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
                            <input type="text" class="form-input" v-model="form.category" placeholder="请输入分类">
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
        const roles = ref([]);
        const loading = ref(true);
        const selectedCategory = ref(null);
        const showDialog = ref(false);
        const editingRole = ref(null);
        const form = reactive({
            name: '',
            category: '',
            gender: '',
            speed: 1.0,
            notes: '',
            reference_audio: null,
        });

        const categories = computed(() => {
            const cats = new Set();
            roles.value.forEach(role => {
                if (role.category) {
                    cats.add(role.category);
                }
            });
            return Array.from(cats);
        });

        const filteredRoles = computed(() => {
            if (!selectedCategory.value) return roles.value;
            return roles.value.filter(role => role.category === selectedCategory.value);
        });

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
            form.notes = role.notes || '';
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
                    await API.updateRole(editingRole.value.id, {
                        name: form.name,
                        category: form.category,
                        gender: form.gender,
                        speed: form.speed,
                        notes: form.notes,
                    });
                    roleId = editingRole.value.id;
                } else {
                    const role = await API.createRole({
                        name: form.name,
                        category: form.category,
                        gender: form.gender,
                        speed: form.speed,
                        notes: form.notes,
                    });
                    roleId = role.id;
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
                    await API.deleteRole(role.id);
                    await loadRoles();
                } catch (error) {
                    console.error('删除角色失败:', error);
                }
            }
        };

        onMounted(() => {
            loadRoles();
        });

        return {
            roles,
            loading,
            selectedCategory,
            categories,
            filteredRoles,
            showDialog,
            editingRole,
            form,
            filterByCategory,
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
                </div>
                <div class="form-row">
                    <div class="form-group">
                        <label class="form-label">全局默认语速</label>
                        <input type="number" class="form-input" v-model="form.tts_speed" step="0.1" min="0.5" max="2.0">
                    </div>
                    <div class="form-group">
                        <label class="form-label">全局默认音调</label>
                        <input type="number" class="form-input" v-model="form.tts_pitch" step="1" min="-12" max="12">
                    </div>
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
            tts_speed: 1.0,
            tts_pitch: 0,
            library_root: '',
            cpu_workers: 2,
            audio_format: 'mp3',
            mix_bitrate: '192k',
            monitor_interval: 1,
        });

        const originalForm = reactive({});

        const loadConfig = async () => {
            loading.value = true;
            try {
                const config = await API.getConfig();
                Object.assign(form, config);
                Object.assign(originalForm, config);
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
                await API.updateConfig(form);
                Object.assign(originalForm, form);
                alert('配置已保存');
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