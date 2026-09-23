// 章节详情页底部的任务队列面板：本书任务 + 全局任务两组，分组折叠、进度条、
// 取消、按需展开日志（增量续拉，SSE task_update 到达时若日志正在展开就跟着
// 刷新，收起就停止请求）。任务数据来自 N2A.tasksById（根组件的单一 SSE
// 连接写入），这里只在挂载时补一次 GET /tasks 做初始快照。
(function () {
    const { reactive, computed, onMounted, onBeforeUnmount, watch } = Vue;

    window.N2A_TaskPanel = {
        name: 'TaskPanel',
        props: { novelId: { type: String, default: null }, titleFor: { type: Function, default: null } },
        setup(props) {
            const panelOpen = Vue.ref(true);
            const groupExpanded = reactive({});
            const logs = reactive({}); // taskId -> { open, text, nextOffset, timer }

            async function refresh() {
                try {
                    const list = await API.getTasks({ limit: 200 });
                    N2A.seedTasks(list);
                } catch (e) { /* 静默：面板不是关键路径 */ }
            }
            onMounted(refresh);

            function itemName(task) {
                if (task.chapter_id) return (props.titleFor && props.titleFor(task.chapter_id)) || task.chapter_id;
                if (task.type === 'precompute_embedding') return (task.params && task.params.role_id) || '角色';
                if (task.type === 'asset_gen') return '素材库';
                return task.id;
            }

            const groups = computed(() => {
                const all = Object.values(N2A.tasksById).filter((t) => t.novel_id === props.novelId || t.novel_id == null);
                const byGroup = {};
                all.forEach((t) => {
                    const key = t.group_id || t.id;
                    if (!byGroup[key]) byGroup[key] = { id: key, novelId: t.novel_id, type: t.type, tasks: [], createdAt: t.created_at };
                    byGroup[key].tasks.push(t);
                    if (t.created_at && (!byGroup[key].createdAt || t.created_at > byGroup[key].createdAt)) byGroup[key].createdAt = t.created_at;
                });
                const list = Object.values(byGroup).map((g) => {
                    const done = g.tasks.filter((t) => t.state === 'succeeded' || t.state === 'failed' || t.state === 'cancelled').length;
                    if (!(g.id in groupExpanded)) groupExpanded[g.id] = g.tasks.some((t) => t.state === 'running' || t.state === 'queued');
                    return {
                        id: g.id,
                        novelId: g.novelId,
                        summary: `${N2A.TASK_TYPE_LABEL[g.type] || g.type} · ${done}/${g.tasks.length} 完成`,
                        createdAt: g.createdAt,
                        children: g.tasks.map((t) => {
                            const cancelling = t.state === 'running' && t.cancel_requested;
                            return {
                                id: t.id, name: itemName(t), state: t.state,
                                stateLabel: cancelling ? '取消中…' : (N2A.TASK_STATE_LABEL[t.state] || t.state),
                                progress: t.progress && t.progress.total ? Math.round((t.progress.done / t.progress.total) * 100) : (t.state === 'succeeded' ? 100 : 0),
                                canCancel: t.state === 'running' || t.state === 'queued',
                                cancelling,
                                error: t.error,
                            };
                        }),
                    };
                }).sort((a, b) => (b.createdAt || '').localeCompare(a.createdAt || ''));
                return list;
            });
            const thisNovelGroups = computed(() => groups.value.filter((g) => g.novelId === props.novelId && props.novelId != null));
            const globalGroups = computed(() => groups.value.filter((g) => g.novelId == null));

            function toggleGroup(g) { groupExpanded[g.id] = !groupExpanded[g.id]; }

            async function cancelTask(taskId) {
                const task = N2A.tasksById[taskId];
                const running = task && task.state === 'running';
                const ok = await N2A.confirmDialog({
                    title: '取消任务',
                    body: running
                        ? '会立即终止正在跑的推理进程（GPU 任务换手后 llama-server 需要一点时间恢复），已完成的部分不会保留。确定取消吗？'
                        : '任务还在排队，取消后不会开始执行。确定取消吗？',
                    confirmLabel: '取消任务', danger: true,
                });
                if (!ok) return;
                try {
                    await API.deleteTask(taskId);
                    N2A.toast('warning', '任务已取消');
                } catch (e) { N2A.toast('error', e.message); }
            }

            function isTerminal(state) { return state === 'succeeded' || state === 'failed' || state === 'cancelled'; }

            async function pullLog(taskId) {
                const entry = logs[taskId];
                if (!entry || !entry.open) return;
                try {
                    const res = await API.getTaskLog(taskId, entry.nextOffset || 0);
                    entry.text += res.text || '';
                    entry.nextOffset = res.next_offset;
                } catch (e) { /* 日志拉取失败不打断面板 */ }
            }
            function toggleLog(taskId) {
                const entry = logs[taskId] || (logs[taskId] = { open: false, text: '', nextOffset: 0, timer: null });
                entry.open = !entry.open;
                if (entry.open) {
                    pullLog(taskId);
                    const task = N2A.tasksById[taskId];
                    if (task && !isTerminal(task.state)) {
                        entry.timer = setInterval(() => {
                            const t = N2A.tasksById[taskId];
                            pullLog(taskId);
                            if (t && isTerminal(t.state)) { clearInterval(entry.timer); entry.timer = null; }
                        }, 1500);
                    }
                } else if (entry.timer) {
                    clearInterval(entry.timer);
                    entry.timer = null;
                }
            }
            onBeforeUnmount(() => { Object.values(logs).forEach((e) => e.timer && clearInterval(e.timer)); });

            return {
                panelOpen, thisNovelGroups, globalGroups, groupExpanded, logs,
                toggleGroup, cancelTask, toggleLog,
                totalCount: computed(() => groups.value.length),
            };
        },
        template: `
        <div class="task-panel">
            <div class="task-panel-header" @click="panelOpen = !panelOpen">
                <span class="task-panel-header-icon">{{ panelOpen ? '▾' : '▸' }}</span>
                <span class="task-panel-header-title">任务队列 ({{ totalCount }})</span>
            </div>
            <div class="task-panel-body" v-if="panelOpen">
                <template v-if="thisNovelGroups.length">
                    <div class="task-section-title">本书任务</div>
                    <div v-for="g in thisNovelGroups" :key="g.id" class="task-group" :data-group-id="g.id">
                        <div class="task-group-header" @click="toggleGroup(g)">
                            <span class="task-group-summary">{{ g.summary }}</span>
                            <span class="task-group-expand">{{ groupExpanded[g.id] ? '▾' : '▸' }}</span>
                        </div>
                        <div class="task-group-body" v-if="groupExpanded[g.id]">
                            <div v-for="t in g.children" :key="t.id" :data-task-id="t.id">
                                <div class="task-item">
                                    <span class="task-item-name">{{ t.name }}</span>
                                    <span class="task-item-status" :style="{ color: t.state === 'failed' ? 'var(--danger)' : (t.state === 'succeeded' ? 'var(--success)' : 'var(--accent)') }">{{ t.stateLabel }}</span>
                                    <div class="task-progress-track"><div class="task-progress-fill" :style="{ width: t.progress + '%' }"></div></div>
                                    <button v-if="t.canCancel" class="task-cancel-btn" :disabled="t.cancelling" @click="cancelTask(t.id)">{{ t.cancelling ? '取消中…' : '取消' }}</button>
                                    <button class="task-log-btn" @click="toggleLog(t.id)">日志</button>
                                </div>
                                <div v-if="t.error" style="font-size:11px;color:var(--danger);padding-left:2px">{{ t.error }}</div>
                                <div v-if="logs[t.id] && logs[t.id].open" class="task-log">{{ logs[t.id].text || '（暂无日志）' }}</div>
                            </div>
                        </div>
                    </div>
                </template>
                <template v-if="globalGroups.length">
                    <div class="task-section-title">全局任务</div>
                    <div v-for="g in globalGroups" :key="g.id" class="task-group task-group-global" :data-group-id="g.id">
                        <div class="task-group-header" @click="toggleGroup(g)">
                            <span class="task-group-summary">{{ g.summary }}</span>
                            <span class="task-group-expand">{{ groupExpanded[g.id] ? '▾' : '▸' }}</span>
                        </div>
                        <div class="task-group-body" v-if="groupExpanded[g.id]">
                            <div v-for="t in g.children" :key="t.id" :data-task-id="t.id">
                                <div class="task-item">
                                    <span class="task-item-name">{{ t.name }}</span>
                                    <span class="task-item-status" :style="{ color: t.state === 'failed' ? 'var(--danger)' : (t.state === 'succeeded' ? 'var(--success)' : 'var(--accent)') }">{{ t.stateLabel }}</span>
                                    <div class="task-progress-track"><div class="task-progress-fill" :style="{ width: t.progress + '%' }"></div></div>
                                    <button v-if="t.canCancel" class="task-cancel-btn" :disabled="t.cancelling" @click="cancelTask(t.id)">{{ t.cancelling ? '取消中…' : '取消' }}</button>
                                    <button class="task-log-btn" @click="toggleLog(t.id)">日志</button>
                                </div>
                                <div v-if="t.error" style="font-size:11px;color:var(--danger);padding-left:2px">{{ t.error }}</div>
                                <div v-if="logs[t.id] && logs[t.id].open" class="task-log">{{ logs[t.id].text || '（暂无日志）' }}</div>
                            </div>
                        </div>
                    </div>
                </template>
                <div v-if="!thisNovelGroups.length && !globalGroups.length" class="task-empty">暂无任务</div>
            </div>
        </div>
        `,
    };
})();
