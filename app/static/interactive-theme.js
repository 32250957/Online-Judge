
(function activeVisitorHeartbeat() {
    const online = document.querySelector('[data-online-users]');
    if (!online) return;

    function formatNumber(value) {
        return Number(value || 0).toLocaleString('ko-KR');
    }

    async function sendHeartbeat() {
        try {
            const response = await fetch('/api/active-heartbeat', { cache: 'no-store' });
            if (!response.ok) return;
            const data = await response.json();
            if (typeof data.online_user_count !== 'undefined') {
                online.textContent = formatNumber(data.online_user_count);
            }
        } catch (error) {
            // 온라인 카운트는 표시 보조 기능이므로 실패해도 화면을 유지한다.
        }
    }

    sendHeartbeat();
    window.setInterval(sendHeartbeat, 30000);
})();

(function interactiveOjTheme() {
    const root = document.documentElement;
    let targetX = 50;
    let targetY = 28;
    let currentX = targetX;
    let currentY = targetY;
    let rafId = null;

    function animate() {
        currentX += (targetX - currentX) * 0.08;
        currentY += (targetY - currentY) * 0.08;
        root.style.setProperty('--mouse-x', currentX.toFixed(2) + '%');
        root.style.setProperty('--mouse-y', currentY.toFixed(2) + '%');
        rafId = requestAnimationFrame(animate);
    }

    function handlePointerMove(event) {
        const width = window.innerWidth || 1;
        const height = window.innerHeight || 1;
        targetX = Math.max(0, Math.min(100, (event.clientX / width) * 100));
        targetY = Math.max(0, Math.min(100, (event.clientY / height) * 100));
    }

    window.addEventListener('pointermove', handlePointerMove, { passive: true });
    rafId = requestAnimationFrame(animate);

    window.addEventListener('pagehide', function () {
        if (rafId) cancelAnimationFrame(rafId);
    });
})();

(function frontStatusPolling() {
    const queueCard = document.getElementById('front-queue-card');
    if (!queueCard) return;

    const normalCount = document.querySelector('[data-normal-count]');
    const rejudgeCount = document.querySelector('[data-rejudge-count]');
    const waitingCount = document.querySelector('[data-waiting-count]');
    const normalBar = document.querySelector('[data-normal-bar]');
    const rejudgeBar = document.querySelector('[data-rejudge-bar]');
    const waitingBar = document.querySelector('[data-waiting-bar]');
    const health = document.querySelector('[data-queue-health]');
    const contestTitle = document.querySelector('[data-active-contest-title]');

    function setWidth(el, value) {
        if (!el) return;
        const width = Math.max(0, Math.min(100, Number(value || 0)));
        el.style.width = width + '%';
    }

    async function refreshFrontStatus() {
        try {
            const response = await fetch('/api/front-status', { cache: 'no-store' });
            if (!response.ok) return;
            const data = await response.json();
            const queue = data.queue || {};
            const normal = queue.normal || {};
            const rejudge = queue.rejudge || {};
            const waiting = queue.waiting || {};

            if (normalCount) normalCount.textContent = `실행 ${normal.running || 0} · 대기 ${normal.queued || 0}`;
            if (rejudgeCount) rejudgeCount.textContent = `실행 ${rejudge.running || 0} · 대기 ${rejudge.queued || 0}`;
            if (waitingCount) waitingCount.textContent = `${waiting.count || 0}개`;
            setWidth(normalBar, normal.width);
            setWidth(rejudgeBar, rejudge.width);
            setWidth(waitingBar, waiting.width);
            if (health) health.textContent = queue.healthy ? '정상' : '확인 필요';
            if (contestTitle && data.contest) contestTitle.textContent = data.contest.title;
        } catch (error) {
            // 네트워크 오류가 있어도 화면 자체는 최초 로드 상태를 유지한다.
        }
    }

    refreshFrontStatus();
    window.setInterval(refreshFrontStatus, 10000);
})();

(function homeStatusPolling() {
    const root = document.querySelector('[data-home-status-counts]');
    if (!root) return;

    const judgeable = document.querySelector('[data-judgeable-problems]');
    const today = document.querySelector('[data-today-submissions]');
    const online = document.querySelector('[data-online-users]');
    const logBox = document.querySelector('[data-live-submission-log]');

    function formatNumber(value) {
        return Number(value || 0).toLocaleString('ko-KR');
    }

    function escapeHtml(value) {
        return String(value || '')
            .replaceAll('&', '&amp;')
            .replaceAll('<', '&lt;')
            .replaceAll('>', '&gt;')
            .replaceAll('"', '&quot;')
            .replaceAll("'", '&#039;');
    }

    function renderLogs(logs) {
        if (!logBox || !Array.isArray(logs)) return;
        if (logs.length === 0) {
            logBox.innerHTML = '<div class="home-row-card empty-row"><b>아직 제출이 없습니다</b></div>';
            return;
        }
        logBox.innerHTML = logs.map(function(log) {
            const profile = log.user_profile_url
                ? '<a class="username-link" href="' + escapeHtml(log.user_profile_url) + '">' + escapeHtml(log.username) + '</a>'
                : escapeHtml(log.username);
            return '<div class="home-row-card">' +
                '<b>' + profile + ' · <a class="home-row-main-link" href="/submissions/' + encodeURIComponent(log.id) + '">' + escapeHtml(log.problem_id) + ' · ' + escapeHtml(log.result) + '</a></b>' +
                '<small>' + escapeHtml(log.problem_title) + '</small>' +
                '</div>';
        }).join('');
    }

    async function refreshHomeStatus() {
        try {
            const response = await fetch('/api/home-status', { cache: 'no-store' });
            if (!response.ok) return;
            const data = await response.json();
            if (judgeable) judgeable.textContent = formatNumber(data.judgeable_problem_count);
            if (today) today.textContent = formatNumber(data.today_submission_count);
            if (online) online.textContent = formatNumber(data.online_user_count);
            renderLogs(data.recent_submissions || []);
        } catch (error) {
            // 최초 렌더링 값 유지
        }
    }

    refreshHomeStatus();
    window.setInterval(refreshHomeStatus, 8000);
})();

(function homeTabs() {
    const tabButtons = document.querySelectorAll('[data-home-tab]');
    if (!tabButtons.length) return;
    const panels = document.querySelectorAll('[data-home-panel]');

    tabButtons.forEach(function(button) {
        button.addEventListener('click', function() {
            const key = button.getAttribute('data-home-tab');
            tabButtons.forEach(function(item) { item.classList.toggle('active', item === button); });
            panels.forEach(function(panel) { panel.classList.toggle('active', panel.getAttribute('data-home-panel') === key); });
        });
    });
})();

(function recommendedProblemPreview() {
    const buttons = document.querySelectorAll('[data-recommend-problem]');
    if (!buttons.length) return;
    const meta = document.querySelector('[data-preview-meta]');
    const title = document.querySelector('[data-preview-title]');
    const tier = document.querySelector('[data-preview-tier]');
    const reason = document.querySelector('[data-preview-reason]');
    const solved = document.querySelector('[data-preview-solved]');
    const statement = document.querySelector('[data-preview-statement]');
    const link = document.querySelector('[data-preview-link]');

    function escapeText(value) {
        return String(value || '');
    }

    buttons.forEach(function(button) {
        button.addEventListener('click', function() {
            buttons.forEach(function(item) { item.classList.toggle('active', item === button); });
            const problemId = button.getAttribute('data-id') || '';
            const tag = button.getAttribute('data-tag') || '태그 없음';
            const tierName = button.getAttribute('data-tier-name') || 'Unrated';
            const tierClass = button.getAttribute('data-tier-class') || 'tier-unrated';
            if (meta) meta.textContent = '#' + problemId + ' · ' + tag;
            if (title) title.textContent = escapeText(button.getAttribute('data-title'));
            if (tier) tier.innerHTML = '<span class="tier-text ' + tierClass + '">' + escapeText(tierName) + '</span>';
            if (reason) reason.textContent = '추천 이유: ' + escapeText(button.getAttribute('data-match'));
            if (solved) solved.textContent = '해결 → ' + escapeText(button.getAttribute('data-solved')) + '명';
            if (statement) statement.textContent = escapeText(button.getAttribute('data-statement'));
            if (link) link.setAttribute('href', button.getAttribute('data-url') || '/problems');
        });
    });
})();
