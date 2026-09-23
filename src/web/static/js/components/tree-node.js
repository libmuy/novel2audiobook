// 小说树的单个节点，递归渲染。拖拽排序只在同一父节点的子列表内（不共享
// SortableJS group），避免拖出会产出后端不校验类型合法性的树（比如把章节
// 拖进部级）——同级重排完全够用，需要换层级时走「新增」到目标父节点。
//
// 每层子列表各自绑定一个 Sortable 实例，updated() 钩子里无条件销毁重建：
// 这是从旧实现里抄来的、真实踩过坑的写法（见 docs/plan/007），Vue 重渲染
// 有时会换掉 DOM 节点，不重挂 Sortable 会在第二次拖拽时失效。
(function () {
    window.N2A_TreeNode = {
        name: 'TreeNode',
        props: { node: Object, depth: { type: Number, default: 0 } },
        setup(props) {
            const ctx = Vue.inject('treeCtx');
            const childrenEl = Vue.ref(null);
            let sortable = null;

            const isChapter = Vue.computed(() => props.node.type === 'chapter');
            const collapsed = Vue.computed(() => !!ctx.collapsed[props.node.id]);
            const selected = Vue.computed(() => ctx.selectedNodeId.value === props.node.id);
            const checked = Vue.computed(() => {
                const ids = ctx.chapterIdsUnder(props.node);
                return ids.length > 0 && ids.every((id) => ctx.checkedIds.value.includes(id));
            });
            const stateClass = Vue.computed(() => `state-${(props.node.state || 'unparsed')}`);
            const addLabel = Vue.computed(() => {
                if (props.node.type === 'part') return ctx.levels.value.volume ? '+ 新增卷' : '+ 新增章节';
                return '+ 新增章节';
            });
            const addType = Vue.computed(() => {
                if (props.node.type === 'part') return ctx.levels.value.volume ? 'volume' : 'chapter';
                return 'chapter';
            });

            function rebindSortable() {
                if (sortable) { sortable.destroy(); sortable = null; }
                if (!childrenEl.value || isChapter.value) return;
                sortable = Sortable.create(childrenEl.value, {
                    animation: 150,
                    handle: '.tree-node',
                    onEnd(evt) {
                        if (evt.oldIndex === evt.newIndex) return;
                        const childId = (props.node.children || [])[evt.oldIndex] && (props.node.children || [])[evt.oldIndex].id;
                        if (childId) ctx.onReorder(childId, props.node.id, evt.newIndex);
                    },
                });
            }
            Vue.onMounted(rebindSortable);
            Vue.onUpdated(rebindSortable);
            Vue.onBeforeUnmount(() => { if (sortable) sortable.destroy(); });

            return {
                ctx, childrenEl, isChapter, collapsed, selected, checked, stateClass, addLabel, addType,
                indent: Vue.computed(() => 10 + props.depth * 18),
            };
        },
        template: `
        <div>
            <div class="tree-node" :class="{ selected }" :style="{ paddingLeft: indent + 'px' }"
                 :data-node-id="node.id" :data-node-type="node.type"
                 @click="ctx.onSelect(node)" @dblclick="ctx.onOpenWorkbench(node)">
                <button v-if="!isChapter" class="tree-toggle" @click.stop="ctx.onToggleCollapse(node)">
                    {{ collapsed ? '▸' : '▾' }}
                </button>
                <span v-else class="tree-toggle-spacer"></span>
                <input type="checkbox" class="tree-checkbox" :checked="checked"
                       @click.stop @change="ctx.onCheckToggle(node)" />
                <span class="tree-dot" :class="stateClass" v-if="isChapter"></span>
                <span class="tree-node-name">{{ node.title }}</span>
                <span class="tree-node-actions">
                    <button class="btn-text-muted" @click.stop="ctx.onRename(node)">改</button>
                    <button class="btn-danger-text" @click.stop="ctx.onDeleteNode(node)">删</button>
                </span>
            </div>
            <div v-show="!collapsed && !isChapter" ref="childrenEl">
                <tree-node v-for="child in (node.children || [])" :key="child.id" :node="child" :depth="depth + 1" />
            </div>
            <div v-if="!collapsed && !isChapter" class="tree-add-row" :style="{ paddingLeft: (indent + 18) + 'px', paddingTop: '5px', paddingBottom: '5px' }">
                <button @click="ctx.onAdd(addType, node.id)">{{ addLabel }}</button>
            </div>
        </div>
        `,
    };
})();
