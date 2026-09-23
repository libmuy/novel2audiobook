(function () {
    const { reactive, ref, computed, onMounted, onBeforeUnmount } = Vue;

    function formatGB(bytes) {
        if (bytes == null) return '0.0';
        return (bytes / (1024 ** 3)).toFixed(1);
    }

    const App = {
        name: 'App',
        setup() {
            const { route, navigate } = N2A.useRouter();
            const prefs = reactive(N2A.loadUiPrefs());

            onMounted(() => N2A.applyTheme(prefs));
            function toggleDarkMode() {
                prefs.darkMode = !prefs.darkMode;
                N2A.saveUiPrefs({ ...prefs });
                N2A.applyTheme(prefs);
            }

            const resource = reactive({ cpu_percent: 0, memory: { used_bytes: 0, total_bytes: 0 }, gpu: null, gpu_owner: 'idle' });
            const connected = ref(true);

            const OWNER_LABEL = { idle: 'GPU 空闲', llm: 'LLM 占用', tts: 'TTS 占用' };
            const OWNER_DOT = { idle: 'var(--neutralDot)', llm: 'var(--text)', tts: 'var(--accent)' };

            let sse = null;
            onMounted(() => {
                API.getGpuOwner().then((r) => { resource.gpu_owner = r.owner || 'idle'; }).catch(() => {});
                sse = API.createSSEConnection(
                    ({ type, payload }) => {
                        if (type === 'resource') Object.assign(resource, payload);
                        if (type === 'task_update' && payload.task) {
                            N2A.applyTaskUpdate(payload.task);
                            window.dispatchEvent(new CustomEvent('n2a:task-update', { detail: { task: payload.task } }));
                        }
                    },
                    (status) => { connected.value = status === 'connected'; },
                );
            });
            onBeforeUnmount(() => { if (sse) sse.close(); });

            const pages = {
                novels: 'novels', novelDetail: 'novelDetail', workbench: 'workbench',
                roles: 'roles', assets: 'assets', settings: 'settings',
            };
            const isNovelsFamily = computed(() => ['novels', 'novelDetail', 'workbench'].includes(route.name));

            return {
                route, navigate, prefs, toggleDarkMode, resource, connected,
                ownerLabel: computed(() => OWNER_LABEL[resource.gpu_owner] || 'GPU 空闲'),
                ownerDot: computed(() => OWNER_DOT[resource.gpu_owner] || OWNER_DOT.idle),
                memUsed: computed(() => formatGB(resource.memory && resource.memory.used_bytes)),
                memTotal: computed(() => formatGB(resource.memory && resource.memory.total_bytes)),
                gpuBusy: computed(() => (resource.gpu ? resource.gpu.busy_percent : 0)),
                vramUsed: computed(() => formatGB(resource.gpu && resource.gpu.vram_used_bytes)),
                vramTotal: computed(() => formatGB(resource.gpu && resource.gpu.vram_total_bytes)),
                cpuPercent: computed(() => Math.round(resource.cpu_percent || 0)),
                isNovelsFamily,
            };
        },
        components: {
            NovelsPage: window.N2A_NovelsPage,
            NovelDetailPage: window.N2A_NovelDetailPage,
            WorkbenchPage: window.N2A_WorkbenchPage,
            RolesPage: window.N2A_RolesPage,
            AssetsPage: window.N2A_AssetsPage,
            SettingsPage: window.N2A_SettingsPage,
            ModalHost: window.N2A_ModalHost,
            ToastStack: window.N2A_ToastStack,
        },
        template: `
        <header class="n2a-header">
            <div class="n2a-header-left">
                <div class="n2a-brand">novel2audiobook</div>
                <nav class="n2a-nav">
                    <button :class="{ active: isNovelsFamily }" @click="navigate('/novels')">小说</button>
                    <button :class="{ active: route.name === 'roles' }" @click="navigate('/roles')">角色库</button>
                    <button :class="{ active: route.name === 'assets' }" @click="navigate('/assets')">音效库</button>
                    <button :class="{ active: route.name === 'settings' }" @click="navigate('/settings')">设置</button>
                </nav>
            </div>
            <div class="n2a-header-right">
                <div v-if="!connected" class="sse-badge">实时连接已断开</div>
                <div class="resource-bar">
                    <span>CPU <b>{{ cpuPercent }}%</b></span>
                    <span>内存 <b>{{ memUsed }}/{{ memTotal }}G</b></span>
                    <span>GPU <b>{{ gpuBusy }}%</b></span>
                    <span>显存 <b>{{ vramUsed }}/{{ vramTotal }}G</b></span>
                    <span class="resource-owner">
                        <span class="resource-owner-dot" :style="{ background: ownerDot }"></span>
                        <span class="resource-owner-label">{{ ownerLabel }}</span>
                    </span>
                </div>
                <button class="theme-toggle" @click="toggleDarkMode">{{ prefs.darkMode ? '浅色模式' : '深色模式' }}</button>
            </div>
        </header>
        <main class="n2a-main">
            <novels-page v-if="route.name === 'novels'" @open-novel="(id) => navigate('/novels/' + id)" />
            <novel-detail-page v-else-if="route.name === 'novelDetail'" :novel-id="route.params.novelId"
                @go-novels="navigate('/novels')"
                @open-workbench="(cid) => navigate('/novels/' + route.params.novelId + '/chapters/' + cid)" />
            <workbench-page v-else-if="route.name === 'workbench'" :novel-id="route.params.novelId" :chapter-id="route.params.chapterId"
                @go-detail="navigate('/novels/' + route.params.novelId)" />
            <roles-page v-else-if="route.name === 'roles'" />
            <assets-page v-else-if="route.name === 'assets'" />
            <settings-page v-else-if="route.name === 'settings'" />
        </main>
        <modal-host></modal-host>
        <toast-stack></toast-stack>
        `,
    };

    const app = Vue.createApp(App);
    app.component('tree-node', window.N2A_TreeNode);
    app.component('category-node', window.N2A_CategoryNode);
    app.mount('#app');
})();
