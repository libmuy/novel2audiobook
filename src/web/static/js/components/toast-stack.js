(function () {
    window.N2A_ToastStack = {
        name: 'ToastStack',
        setup() {
            return { toasts: N2A.toasts };
        },
        template: `
            <div class="toast-stack">
                <div v-for="t in toasts" :key="t.id" :class="['toast', t.kind]">{{ t.message }}</div>
            </div>
        `,
    };
})();
