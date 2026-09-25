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

    // ---------------------------------------------------------------------
    // 前端错误收集：window error / unhandledrejection / console.error 包装，
    // 批量 POST /api/frontend-logs，后端以 logger("frontend") 写进统一日志
    // （行首 FRONTEND）。去重合并（同消息计数、distinct≤50）、≥10 条或 2 秒
    // flush、每批 ≤20、msg≤500 字 stack≤1000 字；上报端点自身出错直接丢（防
    // 递归）；连续 5 次失败冷却 60 秒（熔断）。
    // ---------------------------------------------------------------------
    const FL_ENDPOINT = '/api/frontend-logs';
    const FL_MAX_DISTINCT = 50;
    const FL_FLUSH_COUNT = 10;
    const FL_FLUSH_MS = 2000;
    const FL_BATCH = 20;
    const FL_MSG_LIMIT = 500;
    const FL_STACK_LIMIT = 1000;
    const FL_CIRCUIT_FAILS = 5;
    const FL_CIRCUIT_COOLDOWN_MS = 60000;

    const _flEntries = new Map(); // message -> {message, stack, level, url, count}
    let _flTimer = null;
    let _flFails = 0;
    let _flCircuitUntil = 0;
    let _flInFlight = false; // 防递归：上报期间的 console.error 一律丢弃

    function _flTruncate(s, limit) {
        const str = String(s == null ? '' : s);
        return str.length > limit ? `${str.slice(0, limit)}…[截断]` : str;
    }

    function _flReport(level, message, stack, url) {
        if (_flInFlight || Date.now() < _flCircuitUntil) return;
        const msg = _flTruncate(message, FL_MSG_LIMIT);
        if (!msg) return;
        const existing = _flEntries.get(msg);
        if (existing) {
            existing.count += 1;
        } else {
            if (_flEntries.size >= FL_MAX_DISTINCT) return; // distinct 封顶，丢新的
            _flEntries.set(msg, {
                message: msg,
                stack: _flTruncate(stack, FL_STACK_LIMIT),
                level, url: url || '', count: 1,
            });
        }
        let total = 0;
        for (const e of _flEntries.values()) total += e.count;
        if (total >= FL_FLUSH_COUNT) _flFlush();
        else if (!_flTimer) _flTimer = setTimeout(_flFlush, FL_FLUSH_MS);
    }

    function _flFlush() {
        if (_flTimer) { clearTimeout(_flTimer); _flTimer = null; }
        if (!_flEntries.size || Date.now() < _flCircuitUntil) return;
        while (_flEntries.size) {
            const batch = [];
            for (const key of Array.from(_flEntries.keys())) {
                if (batch.length >= FL_BATCH) break;
                batch.push(_flEntries.get(key));
                _flEntries.delete(key);
            }
            _flSend(batch);
        }
    }

    function _flSend(batch) {
        _flInFlight = true;
        fetch(FL_ENDPOINT, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ entries: batch }),
            keepalive: true,
        }).then((res) => {
            if (res.ok) _flFails = 0;
            else _flOnFail();
        }).catch(() => _flOnFail())
            .finally(() => { _flInFlight = false; });
    }

    function _flOnFail() {
        // 失败的批次已丢（不回填、不上报自身错误——防递归）
        _flFails += 1;
        if (_flFails >= FL_CIRCUIT_FAILS) {
            _flCircuitUntil = Date.now() + FL_CIRCUIT_COOLDOWN_MS;
            _flFails = 0;
        }
    }

    window.addEventListener('error', (e) => {
        _flReport('error', e.message || String(e.error || 'Error'),
            (e.error && e.error.stack) || '',
            e.filename ? `${e.filename}:${e.lineno}` : '');
    });
    window.addEventListener('unhandledrejection', (e) => {
        const r = e.reason;
        _flReport('error', (r && r.message) || String(r), (r && r.stack) || '', '');
    });
    const _origConsoleError = console.error;
    console.error = function (...args) {
        const err = args.find((a) => a instanceof Error);
        _flReport('error', args.map((a) => String(a && a.message ? a.message : a)).join(' '),
            (err && err.stack) || '', '');
        _origConsoleError.apply(console, args); // 保留原行为
    };

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
