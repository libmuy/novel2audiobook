(function () {
    const { reactive, ref, computed, onMounted, onBeforeUnmount, provide } = Vue;

    window.N2A_RolesPage = {
        name: 'RolesPage',
        setup() {
            const cat = window.N2A_useCategoryTree({
                getTree: API.getRoleCategoryTree.bind(API),
                putTree: API.putRoleCategoryTree.bind(API),
                itemNoun: '角色',
            });
            provide('catCtx', {
                collapsed: cat.collapsed, filterPath: cat.filterPath, pathOf: cat.pathOf,
                toggleCollapse: cat.toggleCollapse, select: cat.select, addChild: cat.addChild,
                rename: cat.rename, remove: cat.remove,
            });

            const roles = ref([]);
            const tagFilters = ref([]);
            const tags = ref([]);
            const precomputing = reactive({}); // roleId -> taskId

            async function loadRoles() { roles.value = await API.getRoles(); }
            async function loadTags() { tags.value = (await API.getRoleTags()).tags || []; }
            async function loadAll() { await Promise.all([cat.refresh(), loadRoles(), loadTags()]); }
            onMounted(loadAll);

            function onTaskUpdate(e) {
                const task = e.detail && e.detail.task;
                if (!task || task.type !== 'precompute_embedding') return;
                const rid = task.params && task.params.role_id;
                if (!rid) return;
                if (['succeeded', 'failed', 'cancelled'].includes(task.state)) {
                    delete precomputing[rid];
                    if (task.state === 'succeeded') N2A.toast('success', `「${rid}」的音色已预计算完成`);
                    if (task.state === 'failed') N2A.toast('error', `预计算失败：${task.error || ''}`);
                    loadRoles();
                } else {
                    precomputing[rid] = task.id;
                }
            }
            window.addEventListener('n2a:task-update', onTaskUpdate);
            onBeforeUnmount(() => window.removeEventListener('n2a:task-update', onTaskUpdate));

            const visibleRoles = computed(() => {
                const filterIds = cat.filterPath.value;
                return roles.value
                    .filter((r) => !filterIds || r.category === filterIds || (r.category || '').startsWith(filterIds + '/'))
                    .filter((r) => tagFilters.value.length === 0 || tagFilters.value.some((t) => (r.tags || []).includes(t)));
            });
            function toggleTagFilter(t) {
                tagFilters.value = tagFilters.value.includes(t) ? tagFilters.value.filter((x) => x !== t) : [...tagFilters.value, t];
            }

            const embMap = {
                valid: { label: '✓ Embedding 有效', cls: 'valid' },
                stale: { label: '✗ Embedding 已过期', cls: 'stale' },
                none: { label: '○ 尚未预计算', cls: 'none' },
            };
            function embInfo(role) {
                const st = role.embedding_status || {};
                if (!st.exists) return embMap.none;
                return st.valid ? embMap.valid : embMap.stale;
            }

            async function openCreate() {
                const res = await N2A.openModal({
                    type: 'roleCreate', width: 460, title: '新增角色',
                    showTextField: true, textFieldLabel: '角色名称', textValue: '',
                    showCategoryGender: true, category: cat.filterPath.value || '', gender: 'unknown', speed: 1.0, notes: '',
                    categoryOptions: cat.flatOptions.value,
                    showTagsField: true, tagsDraft: [],
                    confirmLabel: '创建',
                });
                if (!res || !res.textValue || !res.textValue.trim()) return;
                try {
                    const created = await API.createRole({
                        name: res.textValue.trim(), gender: res.gender, category: res.category,
                        description: res.notes, tags: res.tagsDraft,
                    });
                    if (res.speed && res.speed !== 1.0) await API.updateRole(created.role_id, { speed: res.speed });
                    N2A.toast('success', '角色已创建');
                    loadAll();
                } catch (e) { N2A.toast('error', e.message); }
            }
            async function openEdit(role) {
                const res = await N2A.openModal({
                    type: 'roleEdit', width: 460, title: '编辑角色',
                    showTextField: true, textFieldLabel: '角色名称', textValue: role.name,
                    showCategoryGender: true, category: role.category, gender: role.gender, speed: role.speed || 1.0, notes: role.description,
                    categoryOptions: cat.flatOptions.value, showFileField: true, fileFieldLabel: '替换参考音频',
                    showTagsField: true, tagsDraft: [...(role.tags || [])],
                    confirmLabel: '保存',
                });
                if (!res) return;
                try {
                    await API.updateRole(role.id, {
                        name: res.textValue, gender: res.gender, category: res.category,
                        description: res.notes, speed: res.speed, tags: res.tagsDraft,
                    });
                    const file = N2A.getModalFile();
                    if (file) await API.uploadRoleReference(role.id, file);
                    N2A.toast('success', '已保存');
                    loadAll();
                } catch (e) { N2A.toast('error', e.message); }
            }
            async function openDelete(role) {
                if (role.id === 'narrator') { N2A.toast('error', 'narrator 角色不可删除'); return; }
                try {
                    const preview = await API.deleteRole(role.id, false);
                    const ok = await N2A.confirmDialog({
                        title: '删除角色',
                        body: `该角色被 ${preview.reference_count} 个分块引用，删除后这些分块会变成未绑定。确定删除吗？`,
                        confirmLabel: '删除', danger: true,
                    });
                    if (!ok) return;
                    await API.deleteRole(role.id, true);
                    N2A.toast('success', '角色已删除');
                    loadAll();
                } catch (e) { N2A.toast('error', e.message); }
            }
            async function precompute(role) {
                const ok = await N2A.confirmDialog({
                    title: '预计算音色',
                    body: '会占用 GPU（如需要会先暂停 LLM 服务），完成后 embedding 标记为有效。确定继续吗？',
                });
                if (!ok) return;
                try {
                    const res = await API.createTask({ type: 'precompute_embedding', params: { role_id: role.id } });
                    const created = res.tasks[0];
                    // 环境没就绪时任务失败得比这个 await 还快，而且 SSE 是另一条
                    // 早就建立好的长连接，跟这次 POST 的响应谁先到没有保证——
                    // SSE 的 task_update（连带 onTaskUpdate 里的 delete
                    // precomputing[role.id] + 它自己的结果 toast）完全可能先于
                    // 这个 await 返回。只信 POST 响应体里的快照不够，要跟
                    // N2A.tasksById 里 SSE 可能已经写入的更新状态取最新的那个，
                    // 否则会把"已经终态"又掰回"还在算"，按钮卡死在"预计算中…"。
                    // 终态的结果 toast 统一交给 onTaskUpdate 出（不管它是刚好
                    // 先到还是稍后才到，都只会出现一次），这里不重复上一份。
                    const latestState = (N2A.tasksById[created.id] || created).state;
                    if (!['succeeded', 'failed', 'cancelled'].includes(latestState)) {
                        precomputing[role.id] = created.id;
                        N2A.toast('success', '已提交预计算任务');
                    }
                } catch (e) { N2A.toast('error', e.message); }
            }

            return {
                tree: cat.tree, filterPath: cat.filterPath, selectAll: cat.selectAll, addRoot: () => cat.addChild(null),
                visibleRoles, tags, tagFilters, toggleTagFilter, embInfo, precomputing,
                openCreate, openEdit, openDelete, precompute,
                getReferenceUrl: (id) => API.getRoleReferenceUrl(id),
            };
        },
        components: { CategoryNode: window.N2A_CategoryNode },
        template: `
        <section data-screen-label="全局角色库" style="flex:1;display:flex;flex-direction:column;min-height:0">
            <div class="page-pad" style="padding:20px 24px 16px;border-bottom:1px solid var(--border)"><h2 style="margin:0;font-size:26px">全局角色库</h2></div>
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
                        <h3 style="margin:0;font-size:16px">{{ filterPath || '全部角色' }}</h3>
                        <button class="btn-primary btn" @click="openCreate">+ 新增角色</button>
                    </div>
                    <div v-if="tags.length" class="tag-filter-row">
                        <span class="tag-filter-label">标签筛选</span>
                        <button v-for="t in tags" :key="t.name" class="tag-filter-chip" :class="{ active: tagFilters.includes(t.name) }" @click="toggleTagFilter(t.name)">{{ t.name }}</button>
                    </div>
                    <div class="card-grid">
                        <div v-for="role in visibleRoles" :key="role.id" class="role-card">
                            <div class="role-card-name">{{ role.name }}</div>
                            <div class="role-card-sub">{{ role.category || '未分类' }} &middot; {{ { male: '男', female: '女', unknown: '未知' }[role.gender] }}</div>
                            <div class="tag-chip-row" v-if="(role.tags || []).length">
                                <span v-for="tag in role.tags" :key="tag" class="tag-chip">{{ tag }}</span>
                            </div>
                            <audio v-if="role.has_reference" controls :src="getReferenceUrl(role.id)" style="width:100%;height:30px"></audio>
                            <div v-else class="no-reference-note">未上传参考音频</div>
                            <div class="role-stats">
                                <span :class="['embedding-badge', embInfo(role).cls]">{{ embInfo(role).label }}</span>
                                <span style="color:var(--textMuted)">被 {{ role.novels.length }} 本小说、{{ role.segment_count }} 个分块引用</span>
                            </div>
                            <button v-if="!(role.embedding_status && role.embedding_status.valid)"
                                    class="btn precompute-embedding-btn" :disabled="!role.has_reference || !!precomputing[role.id]"
                                    :title="!role.has_reference ? '没有参考音频，无法预计算' : ''"
                                    @click="precompute(role)">
                                {{ precomputing[role.id] ? '预计算中…' : '预计算音色' }}
                            </button>
                            <div class="card-footer">
                                <button class="btn-text" @click="openEdit(role)">编辑</button>
                                <button class="btn-danger-text" :disabled="role.id === 'narrator'" @click="openDelete(role)">删除</button>
                            </div>
                        </div>
                    </div>
                    <div v-if="!visibleRoles.length" class="empty-state">
                        <div class="empty-state-title">暂无角色</div>
                    </div>
                </div>
            </div>
        </section>
        `,
    };
})();
