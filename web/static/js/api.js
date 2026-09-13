const API = {
    baseUrl: '/api',

    async request(url, options = {}) {
        try {
            const response = await fetch(this.baseUrl + url, {
                ...options,
                headers: {
                    'Content-Type': 'application/json',
                    ...options.headers,
                },
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

    async uploadRaw(novelId, nodeId, text, confirm = false) {
        return this.request(`/novels/${novelId}/nodes/${nodeId}/raw?confirm=${confirm}`, {
            method: 'PUT',
            body: JSON.stringify({ text }),
        });
    },

    // 分块相关
    async getSegments(novelId, chapterId) {
        return this.request(`/novels/${novelId}/chapters/${chapterId}/segments`);
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

    // 音频相关
    async getAudioUrl(novelId, chapterId, md5) {
        return `/api/novels/${novelId}/chapters/${chapterId}/audio/${md5}.wav`;
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

    async deleteRole(roleId) {
        return this.request(`/roles/${roleId}`, {
            method: 'DELETE',
        });
    },

    async uploadRoleReference(roleId, file) {
        const formData = new FormData();
        formData.append('file', file);
        return this.request(`/roles/${roleId}/reference`, {
            method: 'POST',
            body: formData,
            headers: {},
        });
    },

    // 角色分类
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

    // SSE连接
    createSSEConnection(onMessage, onError) {
        const eventSource = new EventSource(this.baseUrl + '/events');
        
        eventSource.onmessage = (event) => {
            try {
                const data = JSON.parse(event.data);
                onMessage(data);
            } catch (error) {
                console.error('SSE消息解析失败:', error);
            }
        };

        eventSource.onerror = (error) => {
            console.error('SSE连接错误:', error);
            onError(error);
        };

        return eventSource;
    }
};