// 设置页分两类，处理方式不同：
// 1. 外观（主题色/圆角/字体/深色）是浏览器本地偏好，存 localStorage，改动
//    立即生效，不走「保存配置」按钮。
// 2. 其余是后端 PATCH /api/config（写 config/*.yaml），需要点「保存配置」；
//    只提交真正改动过的键，数值空值前端拦截，被拒的键带原因 toast。
(function () {
    const { reactive, ref, computed, onMounted } = Vue;

    // 字段 -> 配置点分键；get/set 处理展示值和后端值之间的换算（秒<->毫秒等）
    const FIELDS = [
        { key: 'tts.engine', path: ['tts', 'engine'], type: 'str', default: 'Index-TTS-2.5' },
        { key: 'server.library_root', path: ['server', 'library_root'], type: 'str', default: 'data/library' },
        { key: 'server.cpu_workers', path: ['server', 'cpu_workers'], type: 'int', default: 2 },
        { key: 'mixing.output_format', path: ['mixing', 'output_format'], type: 'str', default: 'mp3' },
        { key: 'mixing.bitrate', path: ['mixing', 'bitrate'], type: 'str', default: '192k' },
        { key: 'mixing.voice_only', path: ['mixing', 'voice_only'], type: 'bool', default: true },
        { key: 'server.monitor_interval_ms', path: ['server', 'monitor_interval_ms'], type: 'int', default: 1000 },
        { key: 'mixing.ducking_threshold', path: ['mixing', 'ducking_threshold'], type: 'float', default: -20.0 },
        { key: 'mixing.ducking_gain_db', path: ['mixing', 'ducking_gain_db'], type: 'float', default: null },
        { key: 'mixing.ducking_fade_ms', path: ['mixing', 'ducking_fade_ms'], type: 'int', default: 300 },
        { key: 'mixing.ambience_gain_db', path: ['mixing', 'ambience_gain_db'], type: 'float', default: -18.0 },
        { key: 'mixing.sfx_limit_dbfs', path: ['mixing', 'sfx_limit_dbfs'], type: 'float', default: -3.0 },
        { key: 'tts.segment_gap_ms', path: ['tts', 'segment_gap_ms'], type: 'int', default: 200 },
    ];

    function getPath(obj, path) {
        return path.reduce((o, k) => (o == null ? o : o[k]), obj);
    }

    window.N2A_SettingsPage = {
        name: 'SettingsPage',
        setup() {
            const prefs = reactive(N2A.loadUiPrefs());
            function applyAndSave() {
                N2A.saveUiPrefs({ ...prefs });
                N2A.applyTheme(prefs);
            }
            function pickAccent(c) { prefs.accent = c; applyAndSave(); }
            Vue.watch(() => [prefs.radius, prefs.font, prefs.darkMode], applyAndSave);

            const remote = ref({}); // 后端原始配置快照（保存基线）
            const form = reactive({}); // 展示值
            const bitrateNum = computed({
                get: () => parseInt((form['mixing.bitrate'] || '192k').replace('k', ''), 10) || 192,
                set: (v) => { form['mixing.bitrate'] = `${v}k`; },
            });
            const monitorSec = computed({
                get: () => Math.round((form['server.monitor_interval_ms'] ?? 1000) / 1000 * 10) / 10,
                set: (v) => { form['server.monitor_interval_ms'] = Math.round(v * 1000); },
            });
            const mixWithAssets = computed({
                get: () => !form['mixing.voice_only'],
                set: (v) => { form['mixing.voice_only'] = !v; },
            });
            const showAdvanced = ref(false);

            function fillFromRemote(cfg) {
                remote.value = cfg;
                FIELDS.forEach((f) => {
                    const v = getPath(cfg, f.path);
                    form[f.key] = v ?? f.default; // ?? 不吞 0 / false
                });
            }
            async function load() {
                try { fillFromRemote(await API.getConfig()); } catch (e) { N2A.toast('error', e.message); }
            }
            onMounted(load);

            function isEmptyNumeric(v) { return v === '' || v === null || (typeof v === 'number' && Number.isNaN(v)); }

            async function save() {
                const patch = {};
                for (const f of FIELDS) {
                    const val = form[f.key];
                    // originalRaw 不套 ?? default：要能区分「后端本来就没设」和
                    // 「有值」，只有清空了一个原本有值的字段才算错误操作，从没
                    // 设置过、界面也留空的可选字段（如 ducking_gain_db）不该拦截保存。
                    const originalRaw = getPath(remote.value, f.path);
                    if ((f.type === 'int' || f.type === 'float') && isEmptyNumeric(val)) {
                        if (originalRaw == null) continue; // 本来就没设，留空是合法状态
                        N2A.toast('error', `${f.key} 不能为空`);
                        return;
                    }
                    const original = originalRaw ?? f.default;
                    if (val !== original) patch[f.key] = val;
                }
                if (Object.keys(patch).length === 0) { N2A.toast('warning', '没有改动'); return; }
                try {
                    const res = await API.updateConfig(patch);
                    if (res.rejected && res.rejected.length) {
                        res.rejected.forEach((r) => N2A.toast('error', `${r.key}（${r.reason}）`));
                    }
                    if (res.applied_keys && res.applied_keys.length) N2A.toast('success', '配置已保存');
                    await load();
                } catch (e) { N2A.toast('error', e.message); }
            }
            function reset() { fillFromRemote(remote.value); }

            return {
                prefs, pickAccent, accentOptions: N2A.ACCENT_OPTIONS,
                form, bitrateNum, monitorSec, mixWithAssets, showAdvanced,
                save, reset,
            };
        },
        template: `
        <section class="page-pad-narrow" data-screen-label="系统配置">
            <h2 style="margin:0 0 20px;font-size:26px">系统配置</h2>
            <div class="settings-section">
                <div class="settings-block">
                    <h4>外观设置</h4>
                    <div class="settings-divider"></div>
                    <div class="settings-field-group">
                        <div class="field">
                            <div class="field-label">主题色</div>
                            <div style="display:flex;gap:10px;flex-wrap:wrap">
                                <button v-for="opt in accentOptions" :key="opt.color" class="accent-swatch"
                                        :class="{ active: prefs.accent === opt.color }" :title="opt.label" @click="pickAccent(opt.color)">
                                    <span class="accent-swatch-fill" :style="{ background: opt.color }"></span>
                                </button>
                            </div>
                        </div>
                        <div class="field">
                            <div class="field-label">圆角大小 <b style="color:var(--text)">{{ prefs.radius }}px</b></div>
                            <input type="range" min="0" max="16" step="1" v-model.number="prefs.radius" style="width:100%;max-width:260px;accent-color:var(--accent)" />
                        </div>
                        <div class="field">
                            <div class="field-label">字体</div>
                            <select class="select" style="max-width:260px" v-model="prefs.font">
                                <option value="Manrope">Manrope</option>
                                <option value="Noto Sans SC">Noto Sans SC</option>
                                <option value="System">系统字体</option>
                            </select>
                        </div>
                        <div class="field">
                            <label style="display:flex;align-items:center;gap:8px;font-size:13px">
                                <input type="checkbox" v-model="prefs.darkMode" />深色模式
                            </label>
                        </div>
                        <div class="settings-hint">外观是本机浏览器偏好，改动立即生效，不需要保存。</div>
                    </div>
                </div>

                <div class="settings-block">
                    <h4>TTS 设置</h4>
                    <div class="settings-divider"></div>
                    <div class="field-label">TTS 引擎选择</div>
                    <select class="select" style="max-width:260px" v-model="form['tts.engine']">
                        <option value="Index-TTS-2.5">IndexTTS</option>
                        <option value="EdgeTTS" disabled>Edge TTS（未接入，暂不可用）</option>
                    </select>
                    <div class="settings-hint">语速/音调是每个角色单独的参数，不是全局配置。</div>
                </div>

                <div class="settings-block">
                    <h4>文件设置</h4>
                    <div class="settings-divider"></div>
                    <div class="field-label">文件输出根目录</div>
                    <input class="input" type="text" v-model="form['server.library_root']" />
                </div>

                <div class="settings-block">
                    <h4>性能设置</h4>
                    <div class="settings-divider"></div>
                    <div class="field-label">后台并发任务数</div>
                    <input class="input" type="number" min="1" max="16" style="width:100px" v-model.number="form['server.cpu_workers']" />
                    <div class="settings-hint">仅影响混音等 CPU 任务；解析和语音合成恒为串行。改动需要重启 ./run.sh webui 才会生效。</div>
                </div>

                <div class="settings-block">
                    <h4>音频设置</h4>
                    <div class="settings-divider"></div>
                    <div class="field-row">
                        <div class="field">
                            <div class="field-label">音频输出格式</div>
                            <select class="select" v-model="form['mixing.output_format']">
                                <option value="mp3">MP3</option>
                                <option value="wav">WAV</option>
                                <option value="flac">FLAC</option>
                            </select>
                        </div>
                        <div class="field">
                            <div class="field-label">混音码率</div>
                            <select class="select setting-bitrate" v-model.number="bitrateNum">
                                <option :value="128">128 kbps</option>
                                <option :value="192">192 kbps</option>
                                <option :value="256">256 kbps</option>
                                <option :value="320">320 kbps</option>
                            </select>
                        </div>
                    </div>
                    <div class="field setting-mix-with-assets" style="margin-top:14px">
                        <label style="display:flex;align-items:center;gap:8px;font-size:13px">
                            <input type="checkbox" v-model="mixWithAssets" />默认混音时叠加背景音/音效
                        </label>
                        <div class="settings-hint">当前 mixing.voice_only 默认 true（只出人声）；效果音流水线仍在完善中，批量混音时也可以单次用「带素材」覆盖。</div>
                    </div>
                    <button class="advanced-toggle" style="margin-top:10px" @click="showAdvanced = !showAdvanced">{{ showAdvanced ? '收起' : '展开' }}高级混音参数</button>
                    <div v-if="showAdvanced" class="settings-field-group" style="margin-top:12px">
                        <div class="field-row">
                            <div class="field setting-ducking-threshold">
                                <div class="field-label">闪避触发阈值（dBFS）</div>
                                <input class="input" type="number" step="0.5" v-model.number="form['mixing.ducking_threshold']" />
                            </div>
                            <div class="field setting-ducking-gain">
                                <div class="field-label">闪避衰减量（dB）</div>
                                <input class="input" type="number" step="0.5" v-model.number="form['mixing.ducking_gain_db']" />
                            </div>
                        </div>
                        <div class="field-row">
                            <div class="field">
                                <div class="field-label">闪避渐变时长（ms）</div>
                                <input class="input" type="number" step="10" v-model.number="form['mixing.ducking_fade_ms']" />
                            </div>
                            <div class="field setting-ambience-gain">
                                <div class="field-label">背景音基础电平（dB）</div>
                                <input class="input" type="number" step="0.5" v-model.number="form['mixing.ambience_gain_db']" />
                            </div>
                        </div>
                        <div class="field-row">
                            <div class="field setting-sfx-limit">
                                <div class="field-label">音效峰值限幅（dBFS）</div>
                                <input class="input" type="number" step="0.5" v-model.number="form['mixing.sfx_limit_dbfs']" />
                            </div>
                            <div class="field setting-segment-gap">
                                <div class="field-label">句间静音（ms）</div>
                                <input class="input" type="number" step="10" v-model.number="form['tts.segment_gap_ms']" />
                            </div>
                        </div>
                    </div>
                </div>

                <div class="settings-block">
                    <h4>监控设置</h4>
                    <div class="settings-divider"></div>
                    <div class="field-label">资源监控刷新间隔（秒）</div>
                    <input class="input" type="number" min="0.2" max="60" step="0.1" style="width:100px" v-model.number="monitorSec" />
                </div>

                <div class="settings-footer">
                    <span></span>
                    <div style="display:flex;gap:10px">
                        <button class="btn" @click="reset">重置</button>
                        <button class="btn-primary btn" @click="save">保存配置</button>
                    </div>
                </div>
            </div>
        </section>
        `,
    };
})();
