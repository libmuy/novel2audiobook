// 全局共享：toast、弹窗服务、拖宽/拖高、hash 路由、主题应用。
// 所有页面/组件都从 window.N2A 取用，不用构建工具走 import，避免额外引入
// 打包步骤（项目约定：Vue3 全局构建 + 无构建工具，见 CLAUDE.md）。
(function () {
    const { reactive, ref, computed } = Vue;

    // ---------------------------------------------------------------------
    // Toast
    // ---------------------------------------------------------------------
    const toasts = reactive([]);
    let toastSeq = 0;
    function toast(kind, message) {
        const id = ++toastSeq;
        toasts.push({ id, kind, message });
        setTimeout(() => {
            const idx = toasts.findIndex((t) => t.id === id);
            if (idx >= 0) toasts.splice(idx, 1);
        }, 3000);
    }

    // ---------------------------------------------------------------------
    // 弹窗服务：openModal(config) 返回 Promise，confirm 时 resolve 整个
    // modal 状态对象（含用户在弹窗里编辑过的字段），cancel/backdrop 点击时
    // resolve(null)。弹窗内表单控件直接 v-model 到这个共享 reactive 对象，
    // 页面只管准备初始字段、消费 resolve 的结果，不用自己管表单状态。
    // ---------------------------------------------------------------------
    function _defaultModal() {
        return {
            open: false, type: '', width: 460, title: '',
            showTextField: false, textFieldLabel: '名称', textValue: '',
            showDescField: false, descValue: '',
            showLevelChecks: false, levelPart: false, levelVolume: false,
            showCategoryGender: false, category: '', gender: 'unknown', speed: 1.0, notes: '',
            showFileField: false, fileFieldLabel: '参考音频',
            showAssetFields: false, assetKind: 'ambience', duration: 5, description: '', seed: 0, negativePrompt: '', prompt: '',
            showTagsField: false, tagsDraft: [], tagInputValue: '',
            showEmotionSelect: false, emotionValue: 'neutral',
            showBgmSfxFields: false, bgmValue: null, sfxValue: null, bgmOptions: [], sfxOptions: [],
            showRolePicker: false, roleId: '',
            categoryOptions: [], roleOptions: [],
            showBody: false, body: '',
            showFooterButtons: true, confirmLabel: '确定', confirmBg: 'var(--accent)',
        };
    }
    const modal = reactive(_defaultModal());
    let _modalResolve = null;
    // 弹窗里选的文件（参考音频等）不能进 modal 这个 reactive 对象——confirmModal
    // 靠 JSON 序列化取快照跟 Vue 响应式代理脱钩，File 对象过不了 JSON 往返。
    // 单独存一份，跟弹窗生命周期同步清空。
    let _modalFile = null;
    function setModalFile(file) { _modalFile = file; }
    function getModalFile() { return _modalFile; }

    function openModal(config) {
        _modalFile = null;
        return new Promise((resolve) => {
            Object.assign(modal, _defaultModal(), config, { open: true });
            _modalResolve = resolve;
        });
    }
    function confirmModal() {
        const snapshot = JSON.parse(JSON.stringify(modal));
        modal.open = false;
        const resolve = _modalResolve;
        _modalResolve = null;
        if (resolve) resolve(snapshot);
    }
    function closeModal() {
        modal.open = false;
        const resolve = _modalResolve;
        _modalResolve = null;
        if (resolve) resolve(null);
    }
    function confirmDialog({ title, body, confirmLabel = '确定', danger = false }) {
        return openModal({
            type: 'confirm', title, width: 420, showBody: true, body,
            confirmLabel, confirmBg: danger ? 'var(--danger)' : 'var(--accent)',
        }).then((res) => !!res);
    }
    function promptDialog({ title, label, value = '', confirmLabel = '确定' }) {
        return openModal({
            type: 'prompt', title, width: 420, showTextField: true,
            textFieldLabel: label || '名称', textValue: value, confirmLabel,
        }).then((res) => (res ? res.textValue : null));
    }
    function addModalTag() {
        const v = (modal.tagInputValue || '').trim();
        if (!v) return;
        if (!modal.tagsDraft.includes(v)) modal.tagsDraft.push(v);
        modal.tagInputValue = '';
    }
    function removeModalTag(tag) {
        modal.tagsDraft = modal.tagsDraft.filter((t) => t !== tag);
    }

    // ---------------------------------------------------------------------
    // 拖宽 / 拖高面板：usePaneSize('n2a.tree.w', {min:220,max:640,def:320})
    // 横向（宽度）用于 >860px 桌面布局；纵向（高度）用于 <=860px 窄屏——两套
    // 独立持久化，互不影响（同一个面板换了断点不会跳一下）。
    // ---------------------------------------------------------------------
    function usePaneSize(storageKey, { min, max, def, axis = 'x' } = {}) {
        let stored = null;
        try { stored = parseInt(localStorage.getItem(storageKey), 10); } catch (e) { /* noop */ }
        const size = ref(Number.isFinite(stored) ? stored : def);
        let dragging = false;
        let startPos = 0;
        let startSize = 0;

        function clamp(v) { return Math.max(min, Math.min(max, v)); }

        function onMove(e) {
            if (!dragging) return;
            const point = e.touches ? e.touches[0] : e;
            const pos = axis === 'x' ? point.clientX : point.clientY;
            size.value = clamp(startSize + (pos - startPos));
        }
        function onUp() {
            if (!dragging) return;
            dragging = false;
            try { localStorage.setItem(storageKey, String(size.value)); } catch (e) { /* noop */ }
            window.removeEventListener('mousemove', onMove);
            window.removeEventListener('mouseup', onUp);
            window.removeEventListener('touchmove', onMove);
            window.removeEventListener('touchend', onUp);
        }
        function startResize(e) {
            dragging = true;
            const point = e.touches ? e.touches[0] : e;
            startPos = axis === 'x' ? point.clientX : point.clientY;
            startSize = size.value;
            window.addEventListener('mousemove', onMove);
            window.addEventListener('mouseup', onUp);
            window.addEventListener('touchmove', onMove, { passive: false });
            window.addEventListener('touchend', onUp);
        }
        return { size, startResize };
    }

    // ---------------------------------------------------------------------
    // hash 路由：#/novels | #/novels/:nid | #/novels/:nid/chapters/:cid |
    // #/roles | #/assets | #/settings。记忆最后一次路由，刷新/重开回到原页。
    // ---------------------------------------------------------------------
    const LAST_ROUTE_KEY = 'n2a.lastRoute';
    function parseHash(hash) {
        const path = (hash || '').replace(/^#/, '') || '/novels';
        const parts = path.split('/').filter(Boolean);
        if (parts[0] === 'novels' && parts[1] && parts[2] === 'chapters' && parts[3]) {
            return { name: 'workbench', params: { novelId: parts[1], chapterId: parts[3] } };
        }
        if (parts[0] === 'novels' && parts[1]) {
            return { name: 'novelDetail', params: { novelId: parts[1] } };
        }
        if (parts[0] === 'roles') return { name: 'roles', params: {} };
        if (parts[0] === 'assets') return { name: 'assets', params: {} };
        if (parts[0] === 'settings') return { name: 'settings', params: {} };
        return { name: 'novels', params: {} };
    }
    function useRouter() {
        let initial = window.location.hash;
        if (!initial) {
            try { initial = localStorage.getItem(LAST_ROUTE_KEY) || ''; } catch (e) { /* noop */ }
            if (initial) window.location.hash = initial;
        }
        const route = reactive(parseHash(window.location.hash));
        function sync() {
            const parsed = parseHash(window.location.hash);
            route.name = parsed.name;
            route.params = parsed.params;
            try { localStorage.setItem(LAST_ROUTE_KEY, window.location.hash || '#/novels'); } catch (e) { /* noop */ }
        }
        window.addEventListener('hashchange', sync);
        function navigate(path) { window.location.hash = path; }
        return { route, navigate };
    }

    // ---------------------------------------------------------------------
    // 主题：深色开关 + 主题色 + 圆角 + 字体，全部是每个浏览器自己的偏好
    // （不是后端配置），存 localStorage，跟 PATCH /api/config 的设置项分开。
    // ---------------------------------------------------------------------
    const UI_PREF_KEY = 'n2a.ui';
    function loadUiPrefs() {
        let saved = {};
        try { saved = JSON.parse(localStorage.getItem(UI_PREF_KEY) || '{}'); } catch (e) { /* noop */ }
        return Object.assign({ darkMode: false, accent: '#ec3013', radius: 8, font: 'Manrope' }, saved);
    }
    function saveUiPrefs(prefs) {
        try { localStorage.setItem(UI_PREF_KEY, JSON.stringify(prefs)); } catch (e) { /* noop */ }
    }
    function applyTheme(prefs) {
        const root = document.documentElement;
        root.setAttribute('data-theme', prefs.darkMode ? 'dark' : 'light');
        root.style.setProperty('--accent', prefs.accent);
        root.style.setProperty('--r', `${prefs.radius}px`);
        root.style.setProperty('--rLg', `${Math.round(prefs.radius * 1.5)}px`);
        const fontStacks = {
            Manrope: "Manrope,'Noto Sans SC','PingFang SC',system-ui,sans-serif",
            System: "system-ui,-apple-system,'PingFang SC','Microsoft YaHei',sans-serif",
            'Noto Sans SC': "'Noto Sans SC','PingFang SC',system-ui,sans-serif",
        };
        root.style.setProperty('--font', fontStacks[prefs.font] || fontStacks.Manrope);
    }

    const ACCENT_OPTIONS = [
        { color: '#ec3013', label: '朱红' },
        { color: '#4f5bd5', label: '靛蓝' },
        { color: '#0f9d8a', label: '青绿' },
        { color: '#e0851f', label: '琥珀' },
        { color: '#8b5cf6', label: '紫罗兰' },
    ];

    const EMOTIONS = [
        { v: 'neutral', l: '中性' }, { v: 'happy', l: '开心' }, { v: 'angry', l: '生气' }, { v: 'sad', l: '悲伤' },
        { v: 'serious', l: '严肃' }, { v: 'afraid', l: '害怕' }, { v: 'surprised', l: '惊讶' }, { v: 'calm', l: '平静' },
    ];

    // 树状态点颜色/中文文案映射：unparsed/parsed/voiced/stale 四态
    const STATE_LABEL = { unparsed: '未解析', parsed: '已解析', voiced: '已配音', stale: '需要重新处理' };

    // ---------------------------------------------------------------------
    // 任务存储：单一 SSE 连接（根组件建立）把 task_update 事件写进这里，
    // task-panel / 素材库 / 角色库页面都从这里读，不用各自开连接或各自轮询。
    // ---------------------------------------------------------------------
    const tasksById = reactive({});
    function seedTasks(list) { (list || []).forEach((t) => { tasksById[t.id] = t; }); }
    function applyTaskUpdate(task) { if (task && task.id) tasksById[task.id] = task; }
    const TASK_TYPE_LABEL = {
        parse: '批量解析', tts: '生成人声', mix: '混音导出',
        precompute_embedding: '预计算音色', asset_gen: '生成素材',
    };
    const TASK_STATE_LABEL = { queued: '排队中', running: '运行中', succeeded: '已完成', failed: '失败', cancelled: '已取消' };

    window.N2A = {
        toasts, toast,
        modal, openModal, confirmModal, closeModal, confirmDialog, promptDialog, addModalTag, removeModalTag,
        setModalFile, getModalFile,
        usePaneSize, useRouter,
        loadUiPrefs, saveUiPrefs, applyTheme,
        ACCENT_OPTIONS, EMOTIONS, STATE_LABEL,
        tasksById, seedTasks, applyTaskUpdate, TASK_TYPE_LABEL, TASK_STATE_LABEL,
    };
})();
