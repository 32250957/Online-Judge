(function () {
    'use strict';
    const form = document.getElementById('appearance-form');
    if (!form) return;
    const theme = form.elements.theme;
    const color = form.elements.color_mode;
    const noEnd = form.elements.no_end;
    const end = form.elements.ends_at;
    const motion = form.elements.motion_enabled;
    const options = document.getElementById('appearance-options');
    function update() {
        options.disabled = theme.value === 'default';
        end.disabled = noEnd.checked;
        end.required = !noEnd.checked;
        const params = new URLSearchParams({theme: theme.value, color_mode: color.value, motion_enabled: String(motion.checked)});
        const url = '/admin/appearance/preview?' + params;
        const iframe = document.getElementById('appearance-preview');
        if (iframe.getAttribute('src') !== url) iframe.src = url;
        document.getElementById('appearance-preview-link').href = url;
    }
    form.addEventListener('change', update);
    update();
})();
