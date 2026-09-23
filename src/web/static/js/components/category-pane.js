// 角色库/素材库共用的分类树逻辑（组合式函数，不是组件）。整树替换语义：
// 每次增删改都发一次 PUT，用服务端返回的树（带补全的 id）刷新本地状态——
// 不在前端自己生成 id，跟 category_tree.assign_missing_ids 的分工对齐。
(function () {
    function buildPathMap(tree) {
        const map = {};
        (function walk(nodes, prefix) {
            nodes.forEach((n) => {
                const path = prefix ? `${prefix}/${n.title}` : n.title;
                map[n.id] = path;
                walk(n.children || [], path);
            });
        })(tree, '');
        return map;
    }

    function flattenOptions(tree) {
        const out = [];
        (function walk(nodes, prefix, depth) {
            nodes.forEach((n) => {
                const path = prefix ? `${prefix}/${n.title}` : n.title;
                out.push({ id: n.id, path, label: '  '.repeat(depth) + n.title });
                walk(n.children || [], path, depth + 1);
            });
        })(tree, '', 0);
        return out;
    }

    function cloneTree(tree) { return JSON.parse(JSON.stringify(tree)); }
    function findInTree(nodes, id) {
        for (const n of nodes) {
            if (n.id === id) return n;
            const found = findInTree(n.children || [], id);
            if (found) return found;
        }
        return null;
    }
    function removeFromTree(nodes, id) {
        const idx = nodes.findIndex((n) => n.id === id);
        if (idx >= 0) { nodes.splice(idx, 1); return true; }
        for (const n of nodes) {
            if (n.children && removeFromTree(n.children, id)) return true;
        }
        return false;
    }

    function useCategoryTree({ getTree, putTree, itemNoun }) {
        const { reactive, ref, computed } = Vue;
        const tree = ref([]);
        const collapsed = reactive({});
        const filterPath = ref(null);

        async function refresh() {
            const res = await getTree();
            tree.value = res.tree || [];
        }
        const pathMap = computed(() => buildPathMap(tree.value));
        function pathOf(node) { return pathMap.value[node.id] || node.title; }
        const flatOptions = computed(() => flattenOptions(tree.value));

        function toggleCollapse(node) { collapsed[node.id] = !collapsed[node.id]; }
        function select(node) { filterPath.value = pathOf(node); }
        function selectAll() { filterPath.value = null; }

        async function addChild(parentNode) {
            const name = await N2A.promptDialog({ title: '新建分类', label: '分类名称', confirmLabel: '创建' });
            if (!name) return;
            const next = cloneTree(tree.value);
            const newNode = { title: name, children: [] };
            if (parentNode) {
                const p = findInTree(next, parentNode.id);
                if (!p) return;
                p.children = p.children || [];
                p.children.push(newNode);
            } else {
                next.push(newNode);
            }
            try {
                const res = await putTree(next);
                tree.value = res.tree;
                N2A.toast('success', '分类已创建');
            } catch (e) { N2A.toast('error', e.message); }
        }
        async function rename(node) {
            const name = await N2A.promptDialog({ title: '重命名分类', label: '分类名称', value: node.title, confirmLabel: '保存' });
            if (!name) return;
            const next = cloneTree(tree.value);
            const target = findInTree(next, node.id);
            if (!target) return;
            target.title = name;
            try {
                const res = await putTree(next);
                tree.value = res.tree;
                N2A.toast('success', '已重命名');
            } catch (e) { N2A.toast('error', e.message); }
        }
        async function remove(node) {
            const ok = await N2A.confirmDialog({
                title: '删除分类',
                body: `确定要删除分类「${node.title}」吗？其下的子分类也会一并删除，使用该分类的${itemNoun || '记录'}会变为未分类。`,
                confirmLabel: '删除', danger: true,
            });
            if (!ok) return;
            const next = cloneTree(tree.value);
            removeFromTree(next, node.id);
            try {
                const res = await putTree(next);
                if (filterPath.value === pathOf(node)) filterPath.value = null;
                tree.value = res.tree;
                N2A.toast('success', '分类已删除');
            } catch (e) { N2A.toast('error', e.message); }
        }

        return { tree, collapsed, filterPath, pathOf, flatOptions, refresh, toggleCollapse, select, selectAll, addChild, rename, remove };
    }

    window.N2A_useCategoryTree = useCategoryTree;
})();
