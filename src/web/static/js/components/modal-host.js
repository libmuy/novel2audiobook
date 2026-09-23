// 通用弹窗宿主：字段按 N2A.modal 的 showXxx 开关渲染，双向绑定直接落在共享
// reactive 对象上（见 core.js 的 openModal 注释）。挂一次在根组件里，各页面
// 只管 N2A.openModal({...}).then(result => ...)。
(function () {
    window.N2A_ModalHost = {
        name: 'ModalHost',
        setup() {
            const m = N2A.modal;
            function onFileChange(e) {
                const file = e.target.files && e.target.files[0];
                if (file) N2A.setModalFile(file);
            }
            function onTagKeydown(e) {
                if (e.key === 'Enter') { e.preventDefault(); N2A.addModalTag(); }
            }
            // 分类树上早被删掉/改名前的旧引用：categoryOptions 里已经没有这个
            // path 了，下拉框要是干脆不显示会看起来像"没分类"，容易被误当空值
            // 保存回去。补一个"未登记"的兜底选项，保留原值可见。
            const categoryOptionsResolved = Vue.computed(() => {
                const opts = m.categoryOptions || [];
                if (m.category && !opts.some((o) => o.path === m.category)) {
                    return [...opts, { id: '__missing__', path: m.category, label: `${m.category}（未登记）` }];
                }
                return opts;
            });
            // 必填的名称类字段为空时，禁用确定——不用等提交了才在 toast 里骂用户，
            // 跟旧的原生 prompt() 一样"空值直接不让点"的体感。回车提交要走同一个
            // 判断，不然按钮 disabled 但回车照样能提交，视觉状态说谎。
            const confirmDisabled = Vue.computed(() => m.showTextField && !(m.textValue || '').trim());
            function onEnterConfirm() { if (!confirmDisabled.value) N2A.confirmModal(); }
            return {
                m, close: N2A.closeModal, confirm: N2A.confirmModal, onEnterConfirm,
                onFileChange, onTagKeydown, removeTag: N2A.removeModalTag,
                emotions: N2A.EMOTIONS, categoryOptionsResolved, confirmDisabled,
                stop(e) { e.stopPropagation(); },
            };
        },
        template: `
        <div v-if="m.open" class="modal-overlay" @click="close" @keydown.esc="close">
            <div class="modal" :style="{ maxWidth: m.width + 'px' }" @click="stop">
                <div class="modal-header">
                    <h3>{{ m.title }}</h3>
                    <button class="modal-close" @click="close">&times;</button>
                </div>
                <div class="modal-body">
                    <div v-if="m.showTextField" class="field">
                        <div class="field-label">{{ m.textFieldLabel }}</div>
                        <input class="input" :class="{ 'asset-name-input': m.showAssetFields, 'role-name-input': m.showCategoryGender }"
                               type="text" v-model="m.textValue" @keydown.enter="onEnterConfirm" autofocus />
                    </div>
                    <div v-if="m.showDescField" class="field">
                        <div class="field-label">简介</div>
                        <textarea class="textarea" v-model="m.descValue"></textarea>
                    </div>
                    <div v-if="m.showLevelChecks" class="field-row">
                        <label style="display:flex;align-items:center;gap:6px;font-size:13px">
                            <input type="checkbox" v-model="m.levelPart" />启用部层级
                        </label>
                        <label style="display:flex;align-items:center;gap:6px;font-size:13px">
                            <input type="checkbox" v-model="m.levelVolume" />启用卷层级
                        </label>
                    </div>
                    <template v-if="m.showCategoryGender">
                        <div class="field-row modal-row">
                            <div class="field" style="flex:1">
                                <div class="field-label">分类</div>
                                <select class="select role-category-select" v-model="m.category">
                                    <option value="">未分类</option>
                                    <option v-for="c in categoryOptionsResolved" :key="c.id" :value="c.path">{{ c.label }}</option>
                                </select>
                            </div>
                            <div class="field" style="flex:1">
                                <div class="field-label">性别</div>
                                <select class="select" v-model="m.gender">
                                    <option value="male">男</option>
                                    <option value="female">女</option>
                                    <option value="unknown">未知</option>
                                </select>
                            </div>
                        </div>
                        <div class="field-row modal-row">
                            <div class="field" style="flex:1">
                                <div class="field-label">语速</div>
                                <input class="input" type="number" step="0.05" v-model.number="m.speed" />
                            </div>
                            <div class="field" style="flex:1" v-if="m.showFileField">
                                <div class="field-label">{{ m.fileFieldLabel }}</div>
                                <label class="upload-label" style="width:100%;text-align:center;border-style:dashed">
                                    选择文件
                                    <input type="file" accept="audio/*" style="display:none" @change="onFileChange" />
                                </label>
                            </div>
                        </div>
                        <div class="field">
                            <div class="field-label">备注</div>
                            <textarea class="textarea" v-model="m.notes"></textarea>
                        </div>
                    </template>
                    <template v-if="m.showAssetFields">
                        <div class="field-row modal-row">
                            <div class="field" style="flex:1">
                                <div class="field-label">类型</div>
                                <select class="select" v-model="m.assetKind">
                                    <option value="ambience">背景音</option>
                                    <option value="sfx">音效</option>
                                </select>
                            </div>
                            <div class="field" style="flex:1">
                                <div class="field-label">分类</div>
                                <select class="select asset-category-select" v-model="m.category">
                                    <option value="">未分类</option>
                                    <option v-for="c in categoryOptionsResolved" :key="c.id" :value="c.path">{{ c.label }}</option>
                                </select>
                            </div>
                        </div>
                        <div class="field">
                            <div class="field-label">提示词</div>
                            <textarea class="textarea asset-prompt-input" v-model="m.prompt"></textarea>
                        </div>
                        <div class="field">
                            <div class="field-label">负向提示词（可选）</div>
                            <input class="input" type="text" v-model="m.negativePrompt" />
                        </div>
                        <div class="field-row modal-row">
                            <div class="field" style="flex:1">
                                <div class="field-label">时长（秒）</div>
                                <input class="input" type="number" step="0.5" v-model.number="m.duration" />
                            </div>
                            <div class="field" style="flex:1">
                                <div class="field-label">随机种子</div>
                                <input class="input" type="number" v-model.number="m.seed" />
                            </div>
                        </div>
                        <div class="field">
                            <div class="field-label">描述</div>
                            <textarea class="textarea" v-model="m.description"></textarea>
                        </div>
                    </template>
                    <div v-if="m.showTagsField" class="field">
                        <div class="field-label">标签</div>
                        <div class="tag-chip-row" style="margin-bottom:8px">
                            <span v-for="tag in m.tagsDraft" :key="tag" class="tag-input-chip">
                                {{ tag }}
                                <button class="tag-chip-remove" @click="removeTag(tag)">&times;</button>
                            </span>
                        </div>
                        <input class="input" :class="{ 'asset-tag-input': m.showAssetFields, 'role-tag-input': m.showCategoryGender }"
                               type="text" placeholder="输入标签后按回车"
                               v-model="m.tagInputValue" @keydown="onTagKeydown" />
                    </div>
                    <div v-if="m.showBgmSfxFields" class="field segment-fx">
                        <div style="flex:1">
                            <div class="field-label">背景音</div>
                            <select class="select batch-bgm-select" v-model="m.bgmValue">
                                <option :value="undefined">（不修改）</option>
                                <option :value="null">（清空）</option>
                                <option v-for="n in m.bgmOptions" :key="n" :value="n">{{ n }}</option>
                            </select>
                        </div>
                        <div style="flex:1">
                            <div class="field-label">音效</div>
                            <select class="select batch-sfx-select" v-model="m.sfxValue">
                                <option :value="undefined">（不修改）</option>
                                <option :value="null">（清空）</option>
                                <option v-for="n in m.sfxOptions" :key="n" :value="n">{{ n }}</option>
                            </select>
                        </div>
                    </div>
                    <div v-if="m.showEmotionSelect" class="field">
                        <select class="select" v-model="m.emotionValue">
                            <option v-for="em in emotions" :key="em.v" :value="em.v">{{ em.l }}</option>
                        </select>
                    </div>
                    <div v-if="m.showRolePicker" class="field">
                        <select class="select" v-model="m.roleId">
                            <option v-for="r in m.roleOptions" :key="r.id" :value="r.id">{{ r.name }}</option>
                        </select>
                    </div>
                    <p v-if="m.showBody" style="margin:0;font-size:13px;line-height:1.6;color:var(--textMuted);white-space:pre-line">{{ m.body }}</p>
                </div>
                <div class="modal-footer" v-if="m.showFooterButtons">
                    <button class="btn" @click="close">取消</button>
                    <button class="btn-primary btn" :disabled="confirmDisabled" :style="{ background: m.confirmBg }" @click="confirm">{{ m.confirmLabel }}</button>
                </div>
            </div>
        </div>
        `,
    };
})();
