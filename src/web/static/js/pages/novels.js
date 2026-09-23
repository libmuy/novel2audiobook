(function () {
    const { reactive, ref, computed, onMounted } = Vue;

    window.N2A_NovelsPage = {
        name: 'NovelsPage',
        emits: ['open-novel'],
        setup(props, { emit }) {
            const novels = ref([]);
            const search = ref('');
            const loading = ref(true);

            async function load() {
                loading.value = true;
                try {
                    novels.value = await API.getNovels();
                } catch (e) {
                    N2A.toast('error', e.message);
                } finally {
                    loading.value = false;
                }
            }
            onMounted(load);

            const visibleNovels = computed(() => {
                const q = search.value.trim().toLowerCase();
                const list = q ? novels.value.filter((n) => n.title.toLowerCase().includes(q)) : novels.value;
                return list.map((n) => {
                    const counts = n.counts || { total: 0, parsed: 0, voiced: 0, stale: 0 };
                    const total = counts.total || 0;
                    const voicedPct = total ? Math.round((counts.voiced / total) * 100) : 0;
                    const parsedPct = total ? Math.round((counts.parsed / total) * 100) : 0;
                    return { ...n, counts, voicedPct, parsedPct };
                });
            });

            async function openCreate() {
                const res = await N2A.openModal({
                    type: 'novelCreate', width: 460, title: '新增小说',
                    showTextField: true, textFieldLabel: '小说名称', textValue: '',
                    showDescField: true, descValue: '',
                    showLevelChecks: true, levelPart: false, levelVolume: false,
                    confirmLabel: '创建',
                });
                if (!res) return;
                if (!res.textValue || !res.textValue.trim()) { N2A.toast('error', '请输入小说名称'); return; }
                try {
                    await API.createNovel({
                        title: res.textValue.trim(), description: res.descValue || '',
                        levels: { part: res.levelPart, volume: res.levelVolume },
                    });
                    N2A.toast('success', '小说已创建');
                    load();
                } catch (e) { N2A.toast('error', e.message); }
            }
            async function openEdit(novel) {
                const res = await N2A.openModal({
                    type: 'novelEdit', width: 460, title: '编辑小说',
                    showTextField: true, textFieldLabel: '小说名称', textValue: novel.title,
                    showDescField: true, descValue: novel.description,
                    confirmLabel: '保存',
                });
                if (!res) return;
                try {
                    await API.updateNovel(novel.novel_id, { title: res.textValue, description: res.descValue });
                    N2A.toast('success', '已保存');
                    load();
                } catch (e) { N2A.toast('error', e.message); }
            }
            async function openDelete(novel) {
                const ok = await N2A.confirmDialog({
                    title: '删除小说',
                    body: `确定要删除小说「${novel.title}」吗？此操作不可恢复。该小说包含 ${novel.chapter_count} 个章节。`,
                    confirmLabel: '删除', danger: true,
                });
                if (!ok) return;
                try {
                    await API.deleteNovel(novel.novel_id);
                    N2A.toast('success', '小说已删除');
                    load();
                } catch (e) { N2A.toast('error', e.message); }
            }

            return { search, visibleNovels, loading, openCreate, openEdit, openDelete, open: (n) => emit('open-novel', n.novel_id) };
        },
        template: `
        <section class="page-pad" data-screen-label="小说列表">
            <div class="page-head">
                <h2>小说列表</h2>
                <button class="btn-primary btn" @click="openCreate">+ 新增小说</button>
            </div>
            <input class="novel-search" type="text" placeholder="搜索小说名称..." v-model="search" />
            <div class="novel-grid">
                <div v-for="novel in visibleNovels" :key="novel.novel_id" class="novel-card" @click="open(novel)">
                    <div class="novel-card-top">
                        <div class="novel-card-title-row">
                            <h3>{{ novel.title }}</h3>
                            <span v-if="novel.levels && novel.levels.part" class="badge">部</span>
                            <span v-if="novel.levels && novel.levels.volume" class="badge">卷</span>
                        </div>
                        <div class="novel-card-actions">
                            <button @click.stop="openEdit(novel)">编辑</button>
                            <button @click.stop="openDelete(novel)">删除</button>
                        </div>
                    </div>
                    <p class="novel-card-desc">{{ novel.description }}</p>
                    <div>
                        <div class="novel-progress-track">
                            <div class="novel-progress-voiced" :style="{ width: novel.voicedPct + '%' }"></div>
                            <div class="novel-progress-parsed" :style="{ width: novel.parsedPct + '%' }"></div>
                        </div>
                        <div class="novel-progress-label">已配音 {{ novel.counts.voiced }} &nbsp;|&nbsp; 已解析 {{ novel.counts.parsed }} &nbsp;|&nbsp; 共 {{ novel.counts.total }}</div>
                    </div>
                </div>
            </div>
            <div v-if="!loading && visibleNovels.length === 0" class="empty-state">
                <div class="empty-state-title">暂无小说</div>
                <div class="empty-state-sub">点击上方按钮创建第一本小说</div>
            </div>
        </section>
        `,
    };
})();
