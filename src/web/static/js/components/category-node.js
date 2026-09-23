// 角色库/素材库共用的分类树节点，递归渲染。比小说树简单：没有勾选框/状态点/
// 拖拽，只有选中筛选 + 每级新建子分类 + 改名/删除。
(function () {
    window.N2A_CategoryNode = {
        name: 'CategoryNode',
        props: { node: Object, depth: { type: Number, default: 0 } },
        setup(props) {
            const ctx = Vue.inject('catCtx');
            const collapsed = Vue.computed(() => !!ctx.collapsed[props.node.id]);
            const selected = Vue.computed(() => ctx.filterPath.value === ctx.pathOf(props.node));
            return {
                ctx, collapsed, selected,
                indent: Vue.computed(() => 10 + props.depth * 18),
            };
        },
        template: `
        <div :data-category-path="ctx.pathOf(node)">
            <div class="tree-node category-row" :class="{ selected }" :style="{ paddingLeft: indent + 'px' }">
                <button class="tree-toggle" @click.stop="ctx.toggleCollapse(node)">
                    {{ collapsed ? '▸' : '▾' }}
                </button>
                <span class="tree-node-name" @click="ctx.select(node)">{{ node.title }}</span>
                <span class="tree-node-actions">
                    <button class="btn-text-muted" @click.stop="ctx.rename(node)">改</button>
                    <button class="btn-danger-text" @click.stop="ctx.remove(node)">删</button>
                </span>
            </div>
            <template v-if="!collapsed">
                <category-node v-for="child in (node.children || [])" :key="child.id" :node="child" :depth="depth + 1" />
                <div class="tree-add-row" :style="{ paddingLeft: (indent + 18) + 'px', paddingTop: '5px', paddingBottom: '5px' }">
                    <button @click="ctx.addChild(node)">+ 新建子分类</button>
                </div>
            </template>
        </div>
        `,
    };
})();
