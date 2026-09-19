// 嵌套分类树的通用逻辑（角色库、音效库共用）。
//
// 只导出一个普通工厂函数 window.CategoryTree.useCategoryTree——不在这里调用
// app.component：Vue 应用实例是在 app.js 里创建的，`category-tree-pane` 组件
// 因此仍在 app.js 里注册。本文件必须在 app.js 之前加载（见 index.html）。
//
// 树的存取由调用方给的 load()/save(tree) 决定（角色库走 /role-category-tree，
// 音效库走 /asset-category-tree），本文件不认识任何具体接口。
(function () {
    const { ref, reactive, computed } = Vue;

    // load:          () => Promise<{tree: []}>
    // save:          (tree) => Promise<{tree: []}>，服务端校验失败会抛错（重名/含 "/"/超深度 → 400）
    // showToast:     (message, type) => void
    // showConfirm:   与根组件 provide 的 showConfirm 同签名
    // deleteWarning: 删除确认框里的补充说明（各页对「已引用该分类的条目」的处理相同，
    //                但措辞里的名词不同，所以由调用方给）
    function useCategoryTree({ load, save, showToast, showConfirm, deleteWarning }) {
        const tree = ref([]);
        const collapsed = reactive({});
        const selectedPath = ref(null);

        // 把嵌套树摊平成带缩进深度和路径的行；折叠的节点不展开子级
        const flatRows = computed(() => {
            const out = [];
            const walk = (nodes, depth, prefix) => {
                for (const n of nodes) {
                    const path = prefix ? `${prefix}/${n.title}` : n.title;
                    const hasChildren = !!(n.children && n.children.length);
                    out.push({ id: n.id, title: n.title, path, depth, hasChildren });
                    if (hasChildren && !collapsed[n.id]) walk(n.children, depth + 1, path);
                }
            };
            walk(tree.value, 0, '');
            return out;
        });

        // 全部路径（不受折叠影响）：条目表单的分类下拉用
        const allPaths = computed(() => {
            const out = [];
            const walk = (nodes, depth, prefix) => {
                for (const n of nodes) {
                    const path = prefix ? `${prefix}/${n.title}` : n.title;
                    out.push({ value: path, label: '　'.repeat(depth) + n.title });
                    walk(n.children || [], depth + 1, path);
                }
            };
            walk(tree.value, 0, '');
            return out;
        });

        // 下拉选项 = 树里的全部路径 + 条目身上还挂着、但已经不在树里的分类名
        // （旧数据 / 树节点被删过）。后者也必须出现，否则编辑这种条目会把它的
        // 分类悄悄清空。extraValues 由调用方从自己的条目列表里收集。
        const pathOptions = (extraValues) => {
            const known = new Set(allPaths.value.map((p) => p.value));
            const extra = [];
            for (const v of extraValues || []) {
                if (v && !known.has(v) && !extra.includes(v)) extra.push(v);
            }
            return [
                ...allPaths.value,
                ...extra.map((c) => ({ value: c, label: `${c}（未登记）` })),
            ];
        };

        // 条目的 category 是否落在当前选中的分类下（本身或子路径）；没选分类 = 全部
        const matchesPath = (category) => {
            if (selectedPath.value === null) return true;
            const c = category || '';
            return c === selectedPath.value || c.startsWith(selectedPath.value + '/');
        };

        const toggleCollapse = (id) => { collapsed[id] = !collapsed[id]; };

        const reload = async () => {
            try {
                tree.value = (await load()).tree || [];
            } catch (error) {
                console.error('加载分类树失败:', error);
            }
        };

        // 任何树变更都是"改一份克隆、整树保存、用响应覆盖本地"，服务端负责校验并补 id
        const commitTree = async (mutate) => {
            const next = JSON.parse(JSON.stringify(tree.value));
            mutate(next);
            try {
                tree.value = (await save(next)).tree;
                return true;
            } catch (error) {
                showToast?.(`保存分类失败: ${error.message}`, 'error');
                return false;
            }
        };

        const findNode = (nodes, id) => {
            for (const n of nodes) {
                if (n.id === id) return n;
                const hit = findNode(n.children || [], id);
                if (hit) return hit;
            }
            return null;
        };

        const isUnderSelected = (path) =>
            !!selectedPath.value && (selectedPath.value === path || selectedPath.value.startsWith(path + '/'));

        // mode: 'create'（row 是父节点，null = 顶层）| 'rename'（row 是被改名的节点）
        // 返回是否成功；失败时（重名等）已经 toast 过了，对话框留着让用户改
        const saveNode = async (mode, row, title) => {
            const name = (title || '').trim();
            if (!name) return false;
            const ok = await commitTree((t) => {
                if (mode === 'rename') {
                    findNode(t, row.id).title = name;
                } else if (row) {
                    const p = findNode(t, row.id);
                    (p.children = p.children || []).push({ title: name, children: [] });
                } else {
                    t.push({ title: name, children: [] });
                }
            });
            // 改名后路径变了：选中的是被改名节点（或其子孙）就重置回"全部"，
            // 避免筛选停在一个已经不存在的路径上
            if (ok && mode === 'rename' && isUnderSelected(row.path)) selectedPath.value = null;
            return ok;
        };

        const removeNode = async (row) => {
            const confirmed = await showConfirm({
                title: '删除分类',
                message: `确定删除分类「${row.path}」及其所有子分类吗？`,
                warning: deleteWarning,
                confirmText: '删除',
                confirmClass: 'btn-danger',
            });
            if (!confirmed) return;
            const ok = await commitTree((t) => {
                const remove = (nodes) => {
                    const i = nodes.findIndex((n) => n.id === row.id);
                    if (i >= 0) { nodes.splice(i, 1); return true; }
                    return nodes.some((n) => remove(n.children || []));
                };
                remove(t);
            });
            if (ok && isUnderSelected(row.path)) selectedPath.value = null;
        };

        return {
            tree, collapsed, selectedPath, flatRows, allPaths,
            pathOptions, matchesPath, toggleCollapse, reload, saveNode, removeNode,
        };
    }

    window.CategoryTree = { useCategoryTree };
})();
