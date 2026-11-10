(function siteAppearance() {
    'use strict';
    const element = document.getElementById('site-appearance-config');
    if (!element) return;
    const body = document.body;
    let config;
    try { config = JSON.parse(element.textContent); } catch (_) { return; }
    // The server clock also drives open tabs with an incorrect local OS clock.
    let serverEpoch = Date.parse(config.server_now);
    let monotonicEpoch = performance.now();
    let boundaryTimer;
    let refreshing = false;
    const reducedMotion = matchMedia('(prefers-reduced-motion: reduce)');
    function apply() {
        clearTimeout(boundaryTimer);
        const now = serverEpoch + performance.now() - monotonicEpoch;
        const start = config.starts_at ? Date.parse(config.starts_at) : null;
        const end = config.ends_at ? Date.parse(config.ends_at) : null;
        const active = config.configured_theme === 'pepero' && (start === null || now >= start) && (end === null || now < end);
        body.classList.toggle('event-theme', active);
        body.classList.toggle('event-pepero', active);
        body.classList.toggle('event-dark', active && config.color_mode === 'dark');
        body.classList.toggle('event-light', active && config.color_mode !== 'dark');
        body.classList.toggle('event-still', active && !config.motion_enabled);
        // Content, forms and submitted code stay in place: never reload at a boundary.
        window.dispatchEvent(new Event('siteappearancechange'));
        const next = [start, end].filter(time => time !== null && time > now).sort((a, b) => a - b)[0];
        if (next !== undefined) boundaryTimer = setTimeout(apply, Math.min(next - now + 10, 2147483647));
    }
    async function refresh() {
        if (config.preview || document.hidden || refreshing) return;
        refreshing = true;
        try {
            const response = await fetch('/api/site-appearance', {cache:'no-store', credentials:'same-origin'});
            if (!response.ok) return;
            const value = await response.json();
            if (!['default', 'pepero'].includes(value.configured_theme) || !Number.isFinite(Date.parse(value.server_now))) return;
            config = value;
            serverEpoch = Date.parse(value.server_now);
            monotonicEpoch = performance.now();
            apply();
        } catch (_) {
            // Keep the last schedule; its known end still restores the default offline.
        } finally { refreshing = false; }
    }
    apply();
    if (!config.preview) {
        setInterval(refresh, 60000);
        document.addEventListener('visibilitychange', () => { if (!document.hidden) { apply(); refresh(); } });
        window.addEventListener('pageshow', refresh);
    }
    let pointerFrame = null;
    window.addEventListener('pointermove', event => {
        if (!body.classList.contains('event-theme') || !config.motion_enabled || reducedMotion.matches || pointerFrame) return;
        pointerFrame = requestAnimationFrame(() => {
            body.style.setProperty('--event-pointer-x', ((event.clientX / Math.max(innerWidth,1) - .5) * 10).toFixed(2) + 'px');
            body.style.setProperty('--event-pointer-y', ((event.clientY / Math.max(innerHeight,1) - .5) * 10).toFixed(2) + 'px');
            pointerFrame = null;
        });
    }, {passive:true});
})();
