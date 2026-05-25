// Audio mixer — registry + popover for per-channel volume control (slopsmith#87).
//
// Plugins (or core) register a fader spec via window.slopsmith.audio.registerFader(spec).
// Each spec is the source of truth for its own value: the popover only calls
// getValue() to render and setValue() to commit. Persistence is the plugin's
// responsibility — the registry doesn't store values.
//
// Spec shape:
//   { id, label, min, max, step, defaultValue, getValue, setValue }
(function () {
'use strict';

if (!window.slopsmith) {
    console.warn('[mixer] window.slopsmith missing — audio-mixer.js loaded too early');
    return;
}

const _faders = new Map();
let _popoverEl = null;
let _btnEl = null;
let _open = false;
let _openTimer = null;

function _audioEl() { return document.getElementById('audio'); }

function _clampSongVolume(v) {
    const n = Number(v);
    if (!Number.isFinite(n)) return 80;
    return Math.min(100, Math.max(0, n));
}

// In-memory fallback so volume changes survive the session even when
// localStorage is blocked (private mode / sandboxed contexts).
// Initialized from the persisted value so the fallback starts correct.
let _songVolumeMemory = (() => {
    try {
        const stored = parseFloat(localStorage.getItem('volume'));
        return Number.isFinite(stored) ? Math.min(100, Math.max(0, stored)) : 80;
    } catch (e) { return 80; }
})();

function _readSongVolume() {
    try {
        const stored = parseFloat(localStorage.getItem('volume'));
        return Number.isFinite(stored) ? _clampSongVolume(stored) : _songVolumeMemory;
    } catch (e) {
        return _songVolumeMemory;
    }
}

function _applySongVolume(v) {
    const normalized = _clampSongVolume(v == null ? _readSongVolume() : v);
    _songVolumeMemory = normalized;
    const a = _audioEl();
    if (a) a.volume = normalized / 100;
    const linear = normalized / 100;
    // Multi-stem sloppak: the stems plugin mutes the core <audio> element and
    // routes every stem through its own master GainNode, so a.volume above is
    // dead. Drive that master instead when the stems plugin has published its
    // hook (it clears the hook on teardown for PSARC / stem-less songs).
    const stemsSetMaster = window.slopsmith?.stems?.setMasterVolume;
    if (typeof stemsSetMaster === 'function') {
        // A synchronous throw or a rejected Promise from the stems plugin hook
        // must not abort _applySongVolume before it returns / persists. The
        // try/catch covers a sync throw; the .catch() covers an async rejection.
        try {
            // `void` marks the floating Promise as intentionally discarded,
            // consistent with the other ignored async calls in this module.
            void Promise.resolve(stemsSetMaster(linear))
                .catch(function () { /* stems hook unavailable */ });
        } catch (_) { /* stems hook unavailable */ }
    }
    // Desktop + JUCE: song audio is mixed in the native engine; HTML5 volume is ignored.
    if (window._juceMode) {
        const setGain = window.slopsmithDesktop?.audio?.setGain;
        if (typeof setGain === 'function') {
            // Same dual guard as the stems hook above: the try/catch covers a
            // synchronous throw from setGain, the .catch() covers a rejected IPC.
            try {
                return Promise.resolve(setGain('backing', linear))
                    .catch(function () { /* IPC unavailable */ })
                    .then(function () { return normalized; });
            } catch (_) { /* IPC unavailable */ }
        }
    }
    return Promise.resolve(normalized);
}

function _writeSongVolume(v) {
    const normalized = _clampSongVolume(v);
    void _applySongVolume(normalized);
    try {
        localStorage.setItem('volume', String(normalized));
    } catch (e) {
        // Ignore storage failures (for example in private mode or sandboxed contexts).
    }
}

function registerFader(spec) {
    if (!spec || typeof spec.id !== 'string' || !spec.id) {
        console.warn('[mixer] registerFader: spec.id required');
        return;
    }
    if (typeof spec.getValue !== 'function' || typeof spec.setValue !== 'function') {
        console.warn('[mixer] registerFader: spec.getValue and spec.setValue required', spec.id);
        return;
    }
    let min = Number.isFinite(spec.min) ? spec.min : 0;
    let max = Number.isFinite(spec.max) ? spec.max : 1;
    if (max <= min) {
        console.warn('[mixer] registerFader: max must be > min; correcting', spec.id);
        max = min + 1;
    }
    let step = Number.isFinite(spec.step) ? spec.step : (max - min) / 100;
    if (step <= 0) step = (max - min) / 100;
    const dv = Number.isFinite(spec.defaultValue) ? spec.defaultValue : min;
    const normalized = {
        id: spec.id,
        label: spec.label || spec.id,
        unit: typeof spec.unit === 'string' ? spec.unit : '',
        min,
        max,
        step,
        defaultValue: Math.min(max, Math.max(min, dv)),
        getValue: spec.getValue,
        setValue: spec.setValue,
    };
    if (_faders.has(spec.id)) {
        console.warn('[mixer] registerFader: overwriting existing fader', spec.id);
    }
    _faders.set(spec.id, normalized);
    if (_open) _renderPopover();
}

function unregisterFader(id) {
    _faders.delete(id);
    if (_open) _renderPopover();
}

function getFaders() {
    return Array.from(_faders.values(), function (spec) {
        return Object.freeze({
            id: spec.id,
            label: spec.label,
            unit: spec.unit,
            min: spec.min,
            max: spec.max,
            step: spec.step,
            defaultValue: spec.defaultValue,
            getValue: spec.getValue,
            setValue: spec.setValue,
        });
    });
}

function _formatValue(v, unit) {
    const s = v === Math.round(v) ? v.toFixed(0) : v.toFixed(2);
    return unit ? s + unit : s;
}

function _clampToSpec(v, spec) {
    return Math.min(spec.max, Math.max(spec.min, v));
}

function _strip(spec) {
    let cur = spec.defaultValue;
    try {
        const got = Number(spec.getValue());
        if (Number.isFinite(got)) cur = got;
    } catch (e) {
        console.error('[mixer] getValue threw', spec.id, e);
    }
    // Clamp the initial value to [min,max] so the slider and display agree
    // even when getValue() returns an out-of-range value.
    cur = _clampToSpec(cur, spec);

    const wrap = document.createElement('div');
    wrap.className = 'mixer-strip';

    const labelEl = document.createElement('span');
    labelEl.className = 'mixer-strip-label';
    labelEl.title = spec.label;
    labelEl.textContent = spec.label;

    const slider = document.createElement('input');
    slider.type = 'range';
    slider.className = 'mixer-strip-fader accent-accent';
    slider.min = String(spec.min);
    slider.max = String(spec.max);
    slider.step = String(spec.step);
    slider.value = String(cur);
    slider.setAttribute('aria-label', spec.label + ' volume');
    window.handleSliderInput?.(slider); //initialize the slider's background fill based on the initial value

    const valueEl = document.createElement('span');
    valueEl.className = 'mixer-strip-value';
    valueEl.textContent = _formatValue(cur, spec.unit);

    slider.addEventListener('keydown', (e) => {
        if (e.key === 'ArrowLeft' || e.key === 'ArrowRight' ||
            e.key === 'ArrowUp' || e.key === 'ArrowDown') {
            e.stopPropagation();
            return;
        }
        if (e.code === 'Space' || e.key === ' ') {
            e.preventDefault();
            e.stopPropagation();
        }
    });

    slider.addEventListener('input', () => {
        const parsed = parseFloat(slider.value);
        const requested = Number.isFinite(parsed) ? parsed : cur;
        let actual = requested;
        try {
            spec.setValue(requested);
        } catch (e) {
            console.error('[mixer] setValue threw', spec.id, e);
        }
        // Re-read the actual committed value so the display tracks what
        // the implementation really applied (e.g. internal clamping/rounding).
        try {
            const got = Number(spec.getValue());
            if (Number.isFinite(got)) actual = got;
        } catch (e) {
            console.error('[mixer] getValue threw', spec.id, e);
        }
        actual = _clampToSpec(actual, spec);
        cur = actual;
        slider.value = String(actual);
        window.handleSliderInput?.(slider); //update the slider's background fill on input
        valueEl.textContent = _formatValue(actual, spec.unit);
    });

    wrap.appendChild(labelEl);
    wrap.appendChild(slider);
    wrap.appendChild(valueEl);
    return wrap;
}

function _renderPopover() {
    if (!_popoverEl) return;
    _popoverEl.innerHTML = '';
    const row = document.createElement('div');
    row.className = 'mixer-row';
    if (_faders.size === 0) {
        const empty = document.createElement('span');
        empty.className = 'text-xs text-gray-500';
        empty.textContent = 'No audio sources';
        row.appendChild(empty);
    } else {
        for (const spec of _faders.values()) {
            row.appendChild(_strip(spec));
        }
    }
    _popoverEl.appendChild(row);
}

function _onDocKeydown(e) {
    if (e.key === 'Escape') {
        e.preventDefault();
        e.stopPropagation();
        closeMixer(true);
    }
}

function openMixer() {
    if (!_popoverEl) _init();
    if (!_popoverEl || _open) return;
    _renderPopover();
    _popoverEl.classList.remove('hidden');
    if (_btnEl) _btnEl.setAttribute('aria-expanded', 'true');
    _open = true;
    _openTimer = setTimeout(() => {
        _openTimer = null;
        if (_open) {
            document.addEventListener('click', _onDocClick, true);
            document.addEventListener('keydown', _onDocKeydown, true);
        }
    }, 0);
}

function closeMixer(restoreFocus) {
    if (!_popoverEl) return;
    if (_openTimer !== null) {
        clearTimeout(_openTimer);
        _openTimer = null;
    }
    _popoverEl.classList.add('hidden');
    if (_btnEl) _btnEl.setAttribute('aria-expanded', 'false');
    _open = false;
    document.removeEventListener('click', _onDocClick, true);
    document.removeEventListener('keydown', _onDocKeydown, true);
    // Restore focus to the toggle button when the popover was dismissed via
    // keyboard (Escape) so keyboard users don't lose their place.
    if (restoreFocus && _btnEl) _btnEl.focus();
}

function toggleMixer() { if (_open) closeMixer(); else openMixer(); }

function _onDocClick(e) {
    if (!_popoverEl) return;
    if (_popoverEl.contains(e.target)) return;
    if (_btnEl && _btnEl.contains(e.target)) return;
    closeMixer();
}

function _registerSongFader() {
    registerFader({
        id: 'song',
        label: 'Song',
        unit: '%',
        min: 0, max: 100, step: 1,
        defaultValue: _readSongVolume(),
        getValue: _readSongVolume,
        setValue: _writeSongVolume,
    });
}

function _onScreenChanged(e) {
    const screenId = e && e.detail ? e.detail.id : undefined;
    if (screenId !== 'player') closeMixer(false);
}

let _initialized = false;
function _init() {
    if (_initialized) return;
    _initialized = true;
    _btnEl = document.getElementById('btn-mixer');
    _popoverEl = document.getElementById('mixer-popover');
    _registerSongFader();
    if (window.slopsmith && window.slopsmith.on) {
        window.slopsmith.on('screen:changed', _onScreenChanged);
    }
    window.dispatchEvent(new Event('slopsmith:audio:ready'));
}

window.slopsmith.audio = Object.assign(window.slopsmith.audio || {}, {
    registerFader, unregisterFader, getFaders,
    openMixer, closeMixer, toggleMixer,
    applySongVolume: _applySongVolume,
    readSongVolume: _readSongVolume,
});

if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', _init);
} else {
    _init();
}
})();
