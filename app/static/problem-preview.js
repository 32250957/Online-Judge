(function () {
    function escapeHtml(value) {
        return (value || '').replace(/[&<>'"]/g, ch => ({'&':'&amp;','<':'&lt;','>':'&gt;',"'":'&#39;','"':'&quot;'}[ch]));
    }
    function render(textareaName, targetId) {
        const source = document.querySelector(`textarea[name="${textareaName}"]`);
        const target = document.getElementById(targetId);
        if (!source || !target) return;
        target.innerHTML = escapeHtml(source.value).replace(/\n/g, '<br>');
        if (window.MathJax && window.MathJax.typesetPromise) {
            window.MathJax.typesetPromise([target]).catch(() => {});
        }
    }
    function bind(textareaName, targetId) {
        const source = document.querySelector(`textarea[name="${textareaName}"]`);
        if (!source) return;
        const update = () => render(textareaName, targetId);
        source.addEventListener('input', update);
        update();
    }
    window.addEventListener('DOMContentLoaded', () => {
        bind('description', 'description-preview');
        bind('input_description', 'input-description-preview');
        bind('output_description', 'output-description-preview');
    });
})();
