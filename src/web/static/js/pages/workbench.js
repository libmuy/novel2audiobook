// 配音工作台：分块列表（简单窗口化虚拟滚动，固定行高）+ 右侧编辑面板。
(function () {
    const { reactive, ref, computed, onMounted, onBeforeUnmount, nextTick } = Vue;
    const ROW_HEIGHT = 76;
    const OVERSCAN = 6;

    window.N2A_WorkbenchPage = {
        name: 'WorkbenchPage',
        props: { novelId: String, chapterId: String },
        emits: ['go-detail'],
        setup(props, { emit }) {
            const segments = ref([]); // {seg_id, speaker, text, emotion, sfx, bgm}
            const scriptSource = ref('final');
            const synthSet = ref(new Set()); // seg_id 集合：出现在 timeline 里即视为已合成
            const roles = ref([]);
            const assets = ref({ bgm: [], sfx: [] });
            const selectedIds = ref([]);
            const selectedId = ref(null);
            const editDraft = ref(null);
            const roleDropdownOpen = ref(false);
            const listEl = ref(null);
            const scrollTop = ref(0);
            const viewportH = ref(600);
            const wbWidth = N2A.usePaneSize('n2a.wb.w', { min: 280, max: 640, def: 400, axis: 'x' });

            async function loadRoles() {
                try { roles.value = await API.getRoles(); } catch (e) { /* noop */ }
            }
            async function loadAssets() {
                try { assets.value = await API.getAssets(); } catch (e) { /* noop */ }
            }
            async function loadTimeline() {
                try {
                    const tl = await API.getTimeline(props.novelId, props.chapterId);
                    synthSet.value = new Set((tl.items || []).map((it) => String(it.seg_id)));
                } catch (e) {
                    synthSet.value = new Set();
                }
            }
            async function loadSegments() {
                try {
                    const res = await API.getScript(props.novelId, props.chapterId);
                    segments.value = res.segments;
                    scriptSource.value = res.source;
                } catch (e) {
                    segments.value = [];
                    N2A.toast('error', e.message);
                }
            }
            async function loadAll() {
                await Promise.all([loadSegments(), loadTimeline(), loadRoles(), loadAssets()]);
            }
            onMounted(() => {
                loadAll();
                if (listEl.value) viewportH.value = listEl.value.clientHeight;
            });

            function onTaskUpdate(e) {
                const task = e.detail && e.detail.task;
                if (task && task.novel_id === props.novelId && task.chapter_id === props.chapterId
                    && task.type === 'tts' && ['succeeded', 'failed'].includes(task.state)) {
                    loadTimeline();
                    if (task.state === 'succeeded') N2A.toast('success', 'TTS 已完成');
                    if (task.state === 'failed') N2A.toast('error', `TTS 失败：${task.error || ''}`);
                }
            }
            window.addEventListener('n2a:task-update', onTaskUpdate);
            onBeforeUnmount(() => window.removeEventListener('n2a:task-update', onTaskUpdate));

            const roleById = computed(() => {
                const map = {};
                roles.value.forEach((r) => { map[r.id] = r; });
                return map;
            });
            // 分块引用着一个已经不存在的素材（生成脚本被删过、还没重新生成）时，
            // 下拉框要是没有这个选项会直接显示空白，容易被当成"没设"误保存掉。
            // 补一个"缺失"选项让当前值可见，不强迫用户先去处理素材库那边。
            function withMissingOption(list, current) {
                if (!current || list.includes(current)) return list;
                return [...list, current];
            }
            const bgmOptionsForDraft = computed(() => withMissingOption(assets.value.bgm, editDraft.value && editDraft.value.bgm));
            const sfxOptionsForDraft = computed(() => withMissingOption(assets.value.sfx, editDraft.value && editDraft.value.sfx));
            const stats = computed(() => {
                const total = segments.value.length;
                const voiced = segments.value.filter((s) => synthSet.value.has(String(s.seg_id))).length;
                const unbound = segments.value.filter((s) => !s.speaker).length;
                return { total, voiced, unvoiced: total - voiced, unbound };
            });

            const rows = computed(() => segments.value.map((sg) => {
                const unboundRow = !sg.speaker;
                const role = sg.speaker ? roleById.value[sg.speaker] : null;
                const selected = selectedId.value === sg.seg_id;
                return {
                    seg_id: sg.seg_id,
                    checked: selectedIds.value.includes(sg.seg_id),
                    selected, unbound: unboundRow,
                    synth: synthSet.value.has(String(sg.seg_id)),
                    speakerLabel: role ? role.name : '⚠ 未绑定',
                    preview: sg.text.length > 40 ? sg.text.slice(0, 40) + '...' : sg.text,
                    raw: sg,
                };
            }));

            // ---------------- 简单窗口化虚拟滚动 ----------------
            function onScroll(e) { scrollTop.value = e.target.scrollTop; }
            const visibleRange = computed(() => {
                const start = Math.max(0, Math.floor(scrollTop.value / ROW_HEIGHT) - OVERSCAN);
                const count = Math.ceil(viewportH.value / ROW_HEIGHT) + OVERSCAN * 2;
                const end = Math.min(rows.value.length, start + count);
                return { start, end };
            });
            const visibleRows = computed(() => rows.value.slice(visibleRange.value.start, visibleRange.value.end));
            const topSpacer = computed(() => visibleRange.value.start * ROW_HEIGHT);
            const bottomSpacer = computed(() => Math.max(0, (rows.value.length - visibleRange.value.end) * ROW_HEIGHT));
            // 长章节走虚拟滚动；短章节（<=60 块）直接全量渲染，简单可靠，也让
            // 只有几个分块的常见情况完全不用关心 spacer 计算。
            const useVirtual = computed(() => rows.value.length > 60);

            function selectSegment(row) {
                selectedId.value = row.seg_id;
                const sg = row.raw;
                editDraft.value = { seg_id: sg.seg_id, text: sg.text, speaker: sg.speaker, emotion: sg.emotion || 'neutral', sfx: sg.sfx || null, bgm: sg.bgm || null };
                roleDropdownOpen.value = false;
            }
            function toggleCheck(row) {
                const has = selectedIds.value.includes(row.seg_id);
                selectedIds.value = has ? selectedIds.value.filter((x) => x !== row.seg_id) : [...selectedIds.value, row.seg_id];
            }
            function selectAllUnbound() {
                selectedIds.value = segments.value.filter((s) => !s.speaker).map((s) => s.seg_id);
            }

            function pickRole(roleId) {
                if (editDraft.value) editDraft.value.speaker = roleId;
                roleDropdownOpen.value = false;
            }
            async function createRoleAndBind() {
                const res = await N2A.openModal({
                    type: 'roleCreate', width: 460, title: '新建角色并绑定',
                    showTextField: true, textFieldLabel: '角色名称', textValue: '',
                    showCategoryGender: true, category: '', gender: 'unknown', speed: 1.0, notes: '',
                    showTagsField: true, tagsDraft: [],
                    confirmLabel: '创建并绑定',
                });
                roleDropdownOpen.value = false;
                if (!res || !res.textValue || !res.textValue.trim()) return;
                try {
                    const created = await API.createRole({
                        name: res.textValue.trim(), gender: res.gender, category: res.category,
                        description: res.notes, tags: res.tagsDraft,
                    });
                    if (res.speed && res.speed !== 1.0) await API.updateRole(created.role_id, { speed: res.speed });
                    await loadRoles();
                    pickRole(created.role_id);
                    N2A.toast('success', '角色已创建');
                } catch (e) { N2A.toast('error', e.message); }
            }

            async function syncTimelineAssets() {
                // 混音器读的是 timeline.json 里 TTS 时打好的 sfx/bgm 快照，工作台
                // 编辑只改 script_final.json，两边会不一致，除非同步一次。还没跑过
                // TTS（没有 timeline）时接口 404，那只是意味着没有可同步的目标，
                // 不是错误，安静吞掉。
                try {
                    await API.refreshTimelineAssets(props.novelId, props.chapterId);
                } catch (e) { /* 无 timeline 时正常吞掉 */ }
            }
            async function saveSegment() {
                const d = editDraft.value;
                if (!d) return;
                const original = segments.value.find((s) => s.seg_id === d.seg_id) || {};
                // 只发真的改过的字段：sfx/bgm 没碰过也会被原样传回来，如果这个分块
                // 引用着一个已经不存在的素材（比如生成脚本被删过），后端会校验
                // "素材必须存在"而 400——用户只是想改改文字，不该被这个拦住。
                const patch = {};
                if (d.text !== original.text) patch.text = d.text;
                if (d.speaker !== (original.speaker ?? null)) patch.speaker = d.speaker;
                if (d.emotion !== (original.emotion || 'neutral')) patch.emotion = d.emotion;
                if (d.sfx !== (original.sfx ?? null)) patch.sfx = d.sfx;
                if (d.bgm !== (original.bgm ?? null)) patch.bgm = d.bgm;
                if (Object.keys(patch).length === 0) { cancelEdit(); return; }
                try {
                    await API.updateSegment(props.novelId, props.chapterId, d.seg_id, patch);
                    if ('sfx' in patch || 'bgm' in patch) await syncTimelineAssets();
                    const textChanged = 'text' in patch;
                    await loadSegments();
                    N2A.toast(textChanged ? 'warning' : 'success', textChanged ? '已保存。注意：文本改动会使原有语音失效。' : '已保存');
                } catch (e) { N2A.toast('error', e.message); }
            }
            function cancelEdit() { editDraft.value = null; selectedId.value = null; roleDropdownOpen.value = false; }

            const audioSrc = ref('');
            function playVoiceOnly() {
                const d = editDraft.value;
                if (!d || !synthSet.value.has(String(d.seg_id))) { N2A.toast('error', '该分块尚未生成人声'); return; }
                audioSrc.value = API.getSegmentAudioUrl(props.novelId, props.chapterId, d.seg_id);
            }
            function playMixPreview() { audioSrc.value = API.getOutputAudioUrl(props.novelId, props.chapterId); }

            async function regenerateSegment() {
                const ok = await N2A.confirmDialog({
                    title: '重新生成本块人声',
                    body: '会对整章重新跑一次增量 TTS（其余已合成且未改动的分块会命中缓存，不会重新生成）。确定继续吗？',
                });
                if (!ok) return;
                try {
                    await API.createTask({ type: 'tts', novel_id: props.novelId, scope: { chapter_ids: [props.chapterId] } });
                    N2A.toast('success', '已提交整章增量 TTS');
                } catch (e) { N2A.toast('error', e.message); }
            }

            // ---------------- 批量操作 ----------------
            async function openBatchBind() {
                const res = await N2A.openModal({
                    type: 'batchBind', width: 420, title: '批量绑定角色',
                    showRolePicker: true, roleId: roles.value[0] ? roles.value[0].id : '', roleOptions: roles.value,
                    confirmLabel: '绑定',
                });
                if (!res) return;
                try {
                    await API.batchUpdateSegments(props.novelId, props.chapterId, { seg_ids: selectedIds.value, set: { speaker: res.roleId } });
                    N2A.toast('success', '已批量绑定角色');
                    loadSegments();
                } catch (e) { N2A.toast('error', e.message); }
            }
            async function openBatchEmotion() {
                const res = await N2A.openModal({
                    type: 'batchEmotion', width: 420, title: '批量修改语气',
                    showEmotionSelect: true, emotionValue: 'neutral', confirmLabel: '修改',
                });
                if (!res) return;
                try {
                    await API.batchUpdateSegments(props.novelId, props.chapterId, { seg_ids: selectedIds.value, set: { emotion: res.emotionValue } });
                    N2A.toast('success', '已批量修改语气');
                    loadSegments();
                } catch (e) { N2A.toast('error', e.message); }
            }
            async function openBatchClear() {
                const ok = await N2A.confirmDialog({ title: '批量清空角色', body: '确定要清空选中分块的角色绑定吗？', confirmLabel: '清空', danger: true });
                if (!ok) return;
                try {
                    await API.batchUpdateSegments(props.novelId, props.chapterId, { seg_ids: selectedIds.value, set: { speaker: null } });
                    N2A.toast('success', '已清空角色绑定');
                    loadSegments();
                } catch (e) { N2A.toast('error', e.message); }
            }
            async function openBatchAssets() {
                const res = await N2A.openModal({
                    type: 'batchAssets', width: 420, title: '批量设置背景音/音效',
                    showBgmSfxFields: true, bgmOptions: assets.value.bgm, sfxOptions: assets.value.sfx,
                    confirmLabel: '应用',
                });
                if (!res) return;
                // JSON 往返会丢掉值为 undefined 的键（"不修改"），只有用户真正选过
                // （包括显式选"清空"变成 null）的字段才会出现在 res 里。
                const set = {};
                if ('bgmValue' in res) set.bgm = res.bgmValue;
                if ('sfxValue' in res) set.sfx = res.sfxValue;
                if (Object.keys(set).length === 0) { N2A.toast('warning', '没有选择要修改的字段'); return; }
                try {
                    await API.batchUpdateSegments(props.novelId, props.chapterId, { seg_ids: selectedIds.value, set });
                    await syncTimelineAssets();
                    N2A.toast('success', '已批量设置背景音/音效');
                    loadSegments();
                } catch (e) { N2A.toast('error', e.message); }
            }
            async function onBatchGenerateVoice() {
                const unboundCount = segments.value.filter((s) => selectedIds.value.includes(s.seg_id) && !s.speaker).length;
                if (unboundCount > 0) { N2A.toast('error', `选中的分块中有 ${unboundCount} 个未绑定角色，请先指派`); return; }
                try {
                    await API.createTask({ type: 'tts', novel_id: props.novelId, scope: { chapter_ids: [props.chapterId] } });
                    N2A.toast('success', '任务已提交（整章增量 TTS）');
                } catch (e) { N2A.toast('error', e.message); }
            }

            return {
                novelId: props.novelId, chapterId: props.chapterId,
                stats, rows, visibleRows, topSpacer, bottomSpacer, useVirtual, listEl, onScroll,
                selectedIds, selectedId, editDraft, roleDropdownOpen, roles, assets, wbWidth,
                bgmOptionsForDraft, sfxOptionsForDraft,
                scriptSource,
                selectSegment, toggleCheck, selectAllUnbound, pickRole, createRoleAndBind,
                saveSegment, cancelEdit, playVoiceOnly, playMixPreview, audioSrc, regenerateSegment,
                openBatchBind, openBatchEmotion, openBatchClear, openBatchAssets, onBatchGenerateVoice,
                emotions: N2A.EMOTIONS,
                back: () => emit('go-detail'),
            };
        },
        template: `
        <section class="workbench" data-screen-label="配音工作台">
            <div class="wb-tree" :style="{ width: wbWidth.size.value + 'px' }">
                <div class="wb-tree-header">
                    <button class="back-link" style="align-self:flex-start" @click="back">&larr; 返回小说详情</button>
                    <div class="wb-stats">
                        总分块 <b class="stat-value">{{ stats.total }}</b> &nbsp;|&nbsp;
                        已配音 <b class="stat-value">{{ stats.voiced }}</b> &nbsp;|&nbsp;
                        未配音 <b class="stat-value">{{ stats.unvoiced }}</b> &nbsp;|&nbsp;
                        未绑定角色 <b class="stat-value" :class="{ danger: stats.unbound > 0 }">{{ stats.unbound }}</b>
                    </div>
                    <div v-if="scriptSource === 'draft'" class="script-source-hint">这一章还没有定稿：编辑任意分块会自动把草稿转正。</div>
                    <button class="btn" style="align-self:flex-start" @click="selectAllUnbound">全选未绑定</button>
                </div>
                <div class="wb-segment-list" ref="listEl" @scroll="onScroll">
                    <template v-if="useVirtual">
                        <div :style="{ height: topSpacer + 'px' }"></div>
                        <div v-for="row in visibleRows" :key="row.seg_id" class="segment-card"
                             :class="{ unbound: row.unbound, selected: row.selected }" @click="selectSegment(row)">
                            <input type="checkbox" class="segment-checkbox" :checked="row.checked" @click.stop @change="toggleCheck(row)" />
                            <div class="segment-body">
                                <div class="segment-meta">
                                    <span class="segment-id">{{ row.seg_id }}</span>
                                    <span class="segment-dot" :class="{ synth: row.synth }"></span>
                                    <span class="segment-speaker" :class="{ 'unbound-label': row.unbound }">{{ row.speakerLabel }}</span>
                                </div>
                                <div class="segment-preview">{{ row.preview }}</div>
                            </div>
                        </div>
                        <div :style="{ height: bottomSpacer + 'px' }"></div>
                    </template>
                    <template v-else>
                        <div v-for="row in rows" :key="row.seg_id" class="segment-card"
                             :class="{ unbound: row.unbound, selected: row.selected }" @click="selectSegment(row)">
                            <input type="checkbox" class="segment-checkbox" :checked="row.checked" @click.stop @change="toggleCheck(row)" />
                            <div class="segment-body">
                                <div class="segment-meta">
                                    <span class="segment-id">{{ row.seg_id }}</span>
                                    <span class="segment-dot" :class="{ synth: row.synth }"></span>
                                    <span class="segment-speaker" :class="{ 'unbound-label': row.unbound }">{{ row.speakerLabel }}</span>
                                </div>
                                <div class="segment-preview">{{ row.preview }}</div>
                            </div>
                        </div>
                    </template>
                </div>
                <div v-if="selectedIds.length" class="wb-batch-bar">
                    <div class="wb-batch-count">已选择 {{ selectedIds.length }} 个分块</div>
                    <div class="wb-batch-actions">
                        <button class="btn-sm" @click="openBatchBind">绑定角色</button>
                        <button class="btn-sm" @click="openBatchEmotion">修改语气</button>
                        <button class="btn-sm" @click="openBatchClear">清空角色</button>
                        <button class="btn-sm" @click="openBatchAssets">设置背景音/音效</button>
                        <button class="btn-sm btn-sm-primary" @click="onBatchGenerateVoice">批量生成人声</button>
                    </div>
                </div>
            </div>
            <div class="n2a-resizer" @mousedown="wbWidth.startResize"></div>
            <div class="n2a-contentpane">
                <div v-if="editDraft" class="wb-editor">
                    <h3>编辑分块 {{ editDraft.seg_id }}</h3>
                    <div class="field">
                        <div class="field-label">原文文本</div>
                        <textarea class="textarea" style="min-height:100px" v-model="editDraft.text"></textarea>
                    </div>
                    <div class="role-dropdown-wrap">
                        <div class="field-label">角色绑定</div>
                        <button class="role-dropdown-btn" :style="{ color: editDraft.speaker ? 'var(--text)' : 'var(--danger)' }" @click="roleDropdownOpen = !roleDropdownOpen">
                            <span>{{ editDraft.speaker ? (roles.find(r => r.id === editDraft.speaker) || {}).name || editDraft.speaker : '未绑定 — 点击选择角色' }}</span>
                            <span>&#9662;</span>
                        </button>
                        <div v-if="roleDropdownOpen" class="role-dropdown-list">
                            <div v-for="r in roles" :key="r.id" class="role-dropdown-item" @click="pickRole(r.id)">
                                {{ r.name }}<template v-if="r.category">（{{ r.category }}）</template>
                            </div>
                            <div class="role-dropdown-create" @click="createRoleAndBind">+ 新建角色并绑定</div>
                        </div>
                    </div>
                    <div class="field">
                        <div class="field-label">语气标签</div>
                        <select class="select" v-model="editDraft.emotion">
                            <option v-for="em in emotions" :key="em.v" :value="em.v">{{ em.l }}</option>
                        </select>
                    </div>
                    <div class="field segment-fx">
                        <div style="flex:1">
                            <div class="field-label">背景音</div>
                            <select class="select segment-bgm-select" v-model="editDraft.bgm">
                                <option :value="null">（无）</option>
                                <option v-for="n in bgmOptionsForDraft" :key="n" :value="n">{{ n }}</option>
                            </select>
                        </div>
                        <div style="flex:1">
                            <div class="field-label">音效</div>
                            <select class="select segment-sfx-select" v-model="editDraft.sfx">
                                <option :value="null">（无）</option>
                                <option v-for="n in sfxOptionsForDraft" :key="n" :value="n">{{ n }}</option>
                            </select>
                        </div>
                    </div>
                    <div>
                        <div class="field-label">试听</div>
                        <div class="audio-row">
                            <button class="btn" @click="playVoiceOnly">仅人声</button>
                            <button class="btn" @click="playMixPreview">混音预览</button>
                        </div>
                        <audio v-if="audioSrc" controls :src="audioSrc" style="width:100%;height:32px"></audio>
                    </div>
                    <div>
                        <div class="field-label">操作</div>
                        <button class="btn segment-edit-actions" @click="regenerateSegment">重新生成本块人声</button>
                    </div>
                    <div class="wb-editor-footer">
                        <button class="btn" @click="cancelEdit">取消</button>
                        <button class="btn-primary btn" @click="saveSegment">保存</button>
                    </div>
                </div>
                <div v-else style="height:100%;display:flex;align-items:center;justify-content:center;color:var(--textFaint);font-size:13px">选择左侧分块进行编辑</div>
            </div>
        </section>
        `,
    };
})();
