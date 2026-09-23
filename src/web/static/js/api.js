const API = {
    baseUrl: '/api',

    async request(url, options = {}) {
        try {
            // FormData 上传不能手动设 Content-Type：浏览器需要自己生成带正确
            // boundary 的 multipart/form-data 头，我们设了反而会覆盖掉它，
            // 后端会解析失败。
            const isFormData = options.body instanceof FormData;
            const headers = isFormData
                ? { ...options.headers }
                : { 'Content-Type': 'application/json', ...options.headers };

            const response = await fetch(this.baseUrl + url, { ...options, headers });

            if (!response.ok) {
                const error = await response.json().catch(() => ({ detail: '请求失败' }));
                throw new Error(error.detail || `HTTP ${response.status}`);
            }

            if (options.rawResponse) return response;
            if (response.status === 204) return null;
            return await response.json();
        } catch (error) {
            console.error('API请求失败:', error);
            throw error;
        }
    },

    // 小说
    async getNovels() { return this.request('/novels'); },
    async getNovel(novelId) { return this.request(`/novels/${novelId}`); },
    async createNovel(data) { return this.request('/novels', { method: 'POST', body: JSON.stringify(data) }); },
    async updateNovel(novelId, data) { return this.request(`/novels/${novelId}`, { method: 'PATCH', body: JSON.stringify(data) }); },
    async deleteNovel(novelId) { return this.request(`/novels/${novelId}`, { method: 'DELETE' }); },
    async getChapterStats(novelId) { return this.request(`/novels/${novelId}/chapter-stats`); },
    async getNovelTree(novelId) { return this.request(`/novels/${novelId}/tree`); },

    async createNode(novelId, data) { return this.request(`/novels/${novelId}/nodes`, { method: 'POST', body: JSON.stringify(data) }); },
    async updateNode(novelId, nodeId, data) { return this.request(`/novels/${novelId}/nodes/${nodeId}`, { method: 'PATCH', body: JSON.stringify(data) }); },
    async deleteNode(novelId, nodeId, confirm = false) { return this.request(`/novels/${novelId}/nodes/${nodeId}?confirm=${confirm}`, { method: 'DELETE' }); },
    async reorderNodes(novelId, data) { return this.request(`/novels/${novelId}/nodes/reorder`, { method: 'POST', body: JSON.stringify(data) }); },

    // 章节
    async getChapter(novelId, chapterId) { return this.request(`/novels/${novelId}/chapters/${chapterId}`); },
    async uploadRaw(novelId, chapterId, text, confirm = false) {
        const formData = new FormData();
        formData.append('file', new Blob([text], { type: 'text/plain' }), 'raw.txt');
        return this.request(`/novels/${novelId}/chapters/${chapterId}/raw?confirm=${confirm}`, { method: 'PUT', body: formData });
    },
    async uploadRawFile(novelId, chapterId, file, confirm = false) {
        const formData = new FormData();
        formData.append('file', file);
        return this.request(`/novels/${novelId}/chapters/${chapterId}/raw?confirm=${confirm}`, { method: 'PUT', body: formData });
    },

    // 剧本：X-Script-Source 头标注这份数据来自 final 还是 draft（还没定稿），
    // 工作台用它提示「本章还没有定稿，编辑会自动把草稿转正」。
    async getScript(novelId, chapterId) {
        const resp = await this.request(`/novels/${novelId}/chapters/${chapterId}/script`, { rawResponse: true });
        const segments = await resp.json();
        return { segments, source: resp.headers.get('X-Script-Source') || 'final' };
    },
    async putScript(novelId, chapterId, segments) {
        return this.request(`/novels/${novelId}/chapters/${chapterId}/script`, { method: 'PUT', body: JSON.stringify(segments) });
    },
    async getChapterAssets(novelId, chapterId) { return this.request(`/novels/${novelId}/chapters/${chapterId}/assets`); },
    async refreshTimelineAssets(novelId, chapterId) { return this.request(`/novels/${novelId}/chapters/${chapterId}/timeline/refresh-assets`, { method: 'POST' }); },
    async getTimeline(novelId, chapterId) { return this.request(`/novels/${novelId}/chapters/${chapterId}/timeline`); },

    async updateSegment(novelId, chapterId, segId, data) { return this.request(`/novels/${novelId}/chapters/${chapterId}/segments/${segId}`, { method: 'PATCH', body: JSON.stringify(data) }); },
    async batchUpdateSegments(novelId, chapterId, data) { return this.request(`/novels/${novelId}/chapters/${chapterId}/segments/batch`, { method: 'POST', body: JSON.stringify(data) }); },

    getSegmentAudioUrl(novelId, chapterId, segId) { return `/api/novels/${novelId}/chapters/${chapterId}/segments/${segId}/audio`; },
    getOutputAudioUrl(novelId, chapterId) { return `/api/novels/${novelId}/chapters/${chapterId}/output.mp3?t=${Date.now()}`; },

    // 角色
    async getRoles() { return this.request('/roles'); },
    getRoleReferenceUrl(roleId) { return `/api/roles/${roleId}/reference?t=${Date.now()}`; },
    async createRole(data) { return this.request('/roles', { method: 'POST', body: JSON.stringify(data) }); },
    async updateRole(roleId, data) { return this.request(`/roles/${roleId}`, { method: 'PATCH', body: JSON.stringify(data) }); },
    async deleteRole(roleId, confirm = false) { return this.request(`/roles/${roleId}?confirm=${confirm}`, { method: 'DELETE' }); },
    async uploadRoleReference(roleId, file) {
        const formData = new FormData();
        formData.append('file', file);
        return this.request(`/roles/${roleId}/reference`, { method: 'PUT', body: formData });
    },

    async getRoleCategoryTree() { return this.request('/role-category-tree'); },
    async putRoleCategoryTree(tree) { return this.request('/role-category-tree', { method: 'PUT', body: JSON.stringify({ tree }) }); },
    async getRoleTags() { return this.request('/role-tags'); },

    async getAssetCategoryTree() { return this.request('/asset-category-tree'); },
    async putAssetCategoryTree(tree) { return this.request('/asset-category-tree', { method: 'PUT', body: JSON.stringify({ tree }) }); },
    async getAssetTags() { return this.request('/asset-tags'); },

    // 任务
    async getTasks(params = {}) {
        const qs = new URLSearchParams(params).toString();
        return this.request(`/tasks${qs ? `?${qs}` : ''}`);
    },
    async getTask(taskId) { return this.request(`/tasks/${taskId}`); },
    async createTask(data) { return this.request('/tasks', { method: 'POST', body: JSON.stringify(data) }); },
    async deleteTask(taskId) { return this.request(`/tasks/${taskId}`, { method: 'DELETE' }); },
    async getTaskLog(taskId, offset = 0) { return this.request(`/tasks/${taskId}/log?offset=${offset}`); },
    async preflightTask(data) { return this.request('/tasks/preflight', { method: 'POST', body: JSON.stringify(data) }); },

    // 配置
    async getConfig() { return this.request('/config'); },
    async updateConfig(data) { return this.request('/config', { method: 'PATCH', body: JSON.stringify(data) }); },

    async getGpuOwner() { return this.request('/gpu/owner'); },
    async getMonitor() { return this.request('/monitor'); },

    // 素材
    async getAssets() { return this.request('/assets'); },
    async getAssetSpecs(kind) { return this.request(`/asset-specs${kind ? `?kind=${kind}` : ''}`); },
    getAssetAudioUrl(kind, name) { return `/api/asset-specs/${kind}/${name}/audio`; },
    async createAssetSpec(data) { return this.request('/asset-specs', { method: 'POST', body: JSON.stringify(data) }); },
    async updateAssetSpec(kind, name, data) { return this.request(`/asset-specs/${kind}/${name}`, { method: 'PATCH', body: JSON.stringify(data) }); },
    async deleteAssetSpec(kind, name, deleteFiles = false) { return this.request(`/asset-specs/${kind}/${name}?delete_files=${deleteFiles}`, { method: 'DELETE' }); },

    // SSE：后端发具名事件（resource / task_update），onmessage 收不到，必须
    // 逐个 addEventListener。指数退避重连（1s→2s→4s...封顶 30s），断开徽章
    // 由调用方按 onStatusChange('connected'|'disconnected') 驱动。
    createSSEConnection(onMessage, onStatusChange) {
        let es = null;
        let retryMs = 1000;
        let stopped = false;
        let retryTimer = null;

        const handle = (type) => (event) => {
            try {
                onMessage({ type, payload: JSON.parse(event.data) });
            } catch (error) {
                console.error('SSE消息解析失败:', error);
            }
        };

        const connect = () => {
            if (stopped) return;
            es = new EventSource(this.baseUrl + '/events');
            es.addEventListener('resource', handle('resource'));
            es.addEventListener('task_update', handle('task_update'));
            es.addEventListener('tree_update', handle('tree_update'));
            es.onopen = () => {
                retryMs = 1000;
                onStatusChange && onStatusChange('connected');
            };
            es.onerror = () => {
                onStatusChange && onStatusChange('disconnected');
                es.close();
                if (stopped) return;
                retryTimer = setTimeout(connect, retryMs);
                retryMs = Math.min(retryMs * 2, 30000);
            };
        };
        connect();

        return {
            close() {
                stopped = true;
                if (retryTimer) clearTimeout(retryTimer);
                if (es) es.close();
            },
        };
    },
};
