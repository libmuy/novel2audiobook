const API = {
    baseUrl: '/api',

    async request(url, options = {}) {
        try {
            // FormData 上传不能手动设 Content-Type：浏览器需要自己生成带正确
            // boundary 的 multipart/form-data 头，我们设了反而会覆盖掉它，
            // 后端会解析失败。之前这里传 headers:{} 想清空默认头，但对象展开
            // 顺序是先默认后覆盖，空对象根本盖不掉前面已经设好的 Content-Type。
            const isFormData = options.body instanceof FormData;
            const headers = isFormData
                ? { ...options.headers }
                : { 'Content-Type': 'application/json', ...options.headers };

            const response = await fetch(this.baseUrl + url, {
                ...options,
                headers,
            });

            if (!response.ok) {
                const error = await response.json().catch(() => ({ detail: '请求失败' }));
                throw new Error(error.detail || `HTTP ${response.status}`);
            }

            return await response.json();
        } catch (error) {
            console.error('API请求失败:', error);
            throw error;
        }
    },

    // 小说相关
    async getNovels() {
        return this.request('/novels');
    },

    async getNovel(novelId) {
        return this.request(`/novels/${novelId}`);
    },

    async createNovel(data) {
        return this.request('/novels', {
            method: 'POST',
            body: JSON.stringify(data),
        });
    },

    async updateNovel(novelId, data) {
        return this.request(`/novels/${novelId}`, {
            method: 'PATCH',
            body: JSON.stringify(data),
        });
    },

    async deleteNovel(novelId, confirm = false) {
        return this.request(`/novels/${novelId}?confirm=${confirm}`, {
            method: 'DELETE',
        });
    },

    // 树形结构
    async getNovelTree(novelId) {
        return this.request(`/novels/${novelId}/tree`);
    },

    async createNode(novelId, data) {
        return this.request(`/novels/${novelId}/nodes`, {
            method: 'POST',
            body: JSON.stringify(data),
        });
    },

    async updateNode(novelId, nodeId, data) {
        return this.request(`/novels/${novelId}/nodes/${nodeId}`, {
            method: 'PATCH',
            body: JSON.stringify(data),
        });
    },

    async deleteNode(novelId, nodeId, confirm = false) {
        return this.request(`/novels/${novelId}/nodes/${nodeId}?confirm=${confirm}`, {
            method: 'DELETE',
        });
    },

    async reorderNodes(novelId, data) {
        return this.request(`/novels/${novelId}/nodes/reorder`, {
            method: 'POST',
            body: JSON.stringify(data),
        });
    },

    // 章节相关
    async getChapter(novelId, chapterId) {
        return this.request(`/novels/${novelId}/chapters/${chapterId}`);
    },

    async uploadRaw(novelId, chapterId, text, confirm = false) {
        // 后端是 multipart 文件上传，不是 JSON；headers 传空对象让浏览器自己
        // 生成带 boundary 的 multipart/form-data，不能用 request() 默认的
        // application/json 头（那样会导致后端解析不出 file 字段）
        const formData = new FormData();
        formData.append('file', new Blob([text], { type: 'text/plain' }), 'raw.txt');
        return this.request(`/novels/${novelId}/chapters/${chapterId}/raw?confirm=${confirm}`, {
            method: 'PUT',
            body: formData,
        });
    },

    // 分块相关：分块列表其实是剧本本身（script_final.json / 尚未定稿则回落 draft）
    async getSegments(novelId, chapterId) {
        return this.request(`/novels/${novelId}/chapters/${chapterId}/script`);
    },

    async getTimeline(novelId, chapterId) {
        return this.request(`/novels/${novelId}/chapters/${chapterId}/timeline`);
    },

    async updateSegment(novelId, chapterId, segId, data) {
        return this.request(`/novels/${novelId}/chapters/${chapterId}/segments/${segId}`, {
            method: 'PATCH',
            body: JSON.stringify(data),
        });
    },

    async batchUpdateSegments(novelId, chapterId, data) {
        return this.request(`/novels/${novelId}/chapters/${chapterId}/segments/batch`, {
            method: 'POST',
            body: JSON.stringify(data),
        });
    },

    // 音频相关：分块自己不存 md5（那是后端按 speaker+text+emotion 算出来的
    // audio_cache 文件名），按 seg_id 让后端去查更可靠
    getSegmentAudioUrl(novelId, chapterId, segId) {
        return `/api/novels/${novelId}/chapters/${chapterId}/segments/${segId}/audio`;
    },

    getOutputAudioUrl(novelId, chapterId) {
        return `/api/novels/${novelId}/chapters/${chapterId}/output.mp3?t=${Date.now()}`;
    },

    // 角色相关
    async getRoles() {
        return this.request('/roles');
    },

    async createRole(data) {
        return this.request('/roles', {
            method: 'POST',
            body: JSON.stringify(data),
        });
    },

    async updateRole(roleId, data) {
        return this.request(`/roles/${roleId}`, {
            method: 'PATCH',
            body: JSON.stringify(data),
        });
    },

    async deleteRole(roleId, confirm = false) {
        return this.request(`/roles/${roleId}?confirm=${confirm}`, {
            method: 'DELETE',
        });
    },

    async uploadRoleReference(roleId, file) {
        const formData = new FormData();
        formData.append('file', file);
        return this.request(`/roles/${roleId}/reference`, {
            method: 'PUT', // 后端路由是 PUT，不是 POST
            body: formData,
        });
    },

    // 角色分类管理（自定义分类 CRUD）：后端目前没有 /role-categories 路由，
    // 角色库页面现在是从已有角色列表里 derive 出筛选用的分类集合，属于
    // 🟡 后续迭代，这两个函数不删是为了以后接的时候不用重新设计接口形状，
    // 但当前没有任何地方调用它们
    async getRoleCategories() {
        return this.request('/role-categories');
    },

    async updateRoleCategories(data) {
        return this.request('/role-categories', {
            method: 'PUT',
            body: JSON.stringify(data),
        });
    },

    // 任务相关
    async getTasks() {
        return this.request('/tasks');
    },

    async createTask(data) {
        return this.request('/tasks', {
            method: 'POST',
            body: JSON.stringify(data),
        });
    },

    async deleteTask(taskId) {
        return this.request(`/tasks/${taskId}`, {
            method: 'DELETE',
        });
    },

    async getTaskLog(taskId, offset = 0) {
        return this.request(`/tasks/${taskId}/log?offset=${offset}`);
    },

    async preflightTask(data) {
        return this.request('/tasks/preflight', {
            method: 'POST',
            body: JSON.stringify(data),
        });
    },

    // 配置相关
    async getConfig() {
        return this.request('/config');
    },

    async updateConfig(data) {
        return this.request('/config', {
            method: 'PATCH',
            body: JSON.stringify(data),
        });
    },

    // GPU相关
    async getGpuOwner() {
        return this.request('/gpu/owner');
    },

    // 监控相关
    async getMonitor() {
        return this.request('/monitor');
    },

    // SSE连接：后端发的是带 event: 字段的具名事件（resource/task_update/
    // tree_update），浏览器的 onmessage 只对没有 event: 字段的默认事件触发，
    // 必须逐个用 addEventListener 注册，onmessage 在这里永远不会被调用。
    createSSEConnection(onMessage, onError) {
        const eventSource = new EventSource(this.baseUrl + '/events');

        const handle = (type) => (event) => {
            try {
                const payload = JSON.parse(event.data);
                onMessage({ type, payload });
            } catch (error) {
                console.error('SSE消息解析失败:', error);
            }
        };

        eventSource.addEventListener('resource', handle('resource'));
        eventSource.addEventListener('task_update', handle('task_update'));
        eventSource.addEventListener('tree_update', handle('tree_update'));

        eventSource.onerror = (error) => {
            console.error('SSE连接错误:', error);
            onError(error);
        };

        return eventSource;
    }
};