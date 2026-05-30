function confirmDanger(form, phrase, message) {
    const guide = `${message || '위험 작업입니다.'}\n\n계속하려면 아래 문구를 정확히 입력하세요.\n${phrase}`;
    const value = window.prompt(guide, '');
    if (value !== phrase) {
        alert('확인 문구가 일치하지 않아 작업을 취소했습니다.');
        return false;
    }
    let input = form.querySelector("input[name='confirm_text']");
    if (!input) {
        input = document.createElement('input');
        input.type = 'hidden';
        input.name = 'confirm_text';
        form.appendChild(input);
    }
    input.value = phrase;
    return true;
}
