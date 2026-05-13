document.addEventListener("copy", function (event) {
    const selection = window.getSelection();
    if (!selection || selection.rangeCount === 0) return;
    const container = document.createElement("div");
    for (let i = 0; i < selection.rangeCount; i += 1) {
        container.appendChild(selection.getRangeAt(i).cloneContents());
    }
    if (!container.querySelector("mjx-container")) return;
    container.querySelectorAll("mjx-container").forEach(function (mathNode) {
        let replacement = "";
        const assistive = mathNode.querySelector("mjx-assistive-mml") || mathNode.querySelector("math");
        if (assistive) {
            replacement = assistive.textContent || "";
        }
        if (!replacement) {
            replacement = mathNode.getAttribute("aria-label") || mathNode.textContent || "";
        }
        const span = document.createElement("span");
        span.textContent = replacement.trim();
        mathNode.replaceWith(span);
    });
    const text = container.innerText.replace(/\n{3,}/g, "\n\n");
    if (text.trim()) {
        event.clipboardData.setData("text/plain", text);
        event.preventDefault();
    }
});
